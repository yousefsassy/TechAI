import argparse,json,time,logging,asyncio
from pathlib import Path
from urllib.parse import urljoin,urlsplit
from scraping.public_pages import allowed_url, get_rules, PacedSession, SourceUnavailable, USER_AGENT
from scrapling.spiders import Spider
from sqlalchemy import select, text, update
from backend.config import settings
from backend.database import Session,init_db
from backend.models import Product,Offer,ShoppingConversation,now
from backend.services.shopping_terms import term_matches
from scraping.normalize import plain
import re
from scraping import mytek,tunisianet,spacenet,scoop
from scraping.parser import parse_product

def save(record):
    with Session.begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        o = db.scalar(select(Offer).where(Offer.product_url == record.url))
        if o is None:
            o = db.scalar(select(Offer).where(Offer.supplier_name == record.supplier,
                                             Offer.supplier_product_id == record.source_id))
        if o is not None and o.supplier_name != record.supplier:
            raise ValueError('conflicting_offer_supplier')
        if o is not None and record.observed_at < max(o.scraped_at, o.updated_at):
            return 'superseded'
        p = o.product if o else db.scalar(select(Product).where(Product.signature == record.signature))
        if p is None:
            p=Product(signature=record.signature,name=record.name,category=record.category,model=record.model,
                raw_specifications=record.raw,**record.specifications); db.add(p); db.flush()
        inserted=o is None
        if o is None:
            o=Offer(product_id=p.id,supplier_name=record.supplier,supplier_product_id=record.source_id,product_url=record.url,
                price_tnd=record.price,stock_status=record.stock_status); db.add(o)
        o.product_url=record.url; o.product_id=p.id; o.price_tnd=record.price; o.stock_status=record.stock_status
        o.available_quantity=record.available_quantity; o.image_url=record.image
        o.scraped_at = record.observed_at
        # A failed check can update the outcome without making scraped_at fresh.
        o.updated_at = record.observed_at
        p.raw_specifications=record.raw; p.name=record.name
        p.model = record.model
        for key, value in record.specifications.items():
            if key in ('brand', 'processor', 'ram_gb', 'storage_gb', 'storage_type', 'screen_size', 'operating_system'):
                setattr(p, key, value)
        if record.category not in ('accessories', 'discovered') or p.category == 'accessories':
            p.category = record.category
    return 'inserted' if inserted else 'updated'

def demote(url, observed_at):
    with Session.begin() as db:
        db.execute(update(Offer)
                   .where(Offer.product_url == url, Offer.scraped_at <= observed_at,
                          Offer.updated_at <= observed_at)
                   .values(stock_status='unknown', updated_at=observed_at))

def recent_product_topics():
    """Read only structured search nouns locally to choose existing retailer menu links."""
    with Session() as db:
        rows = db.scalars(select(ShoppingConversation.criteria)
                          .order_by(ShoppingConversation.updated_at.desc()).limit(100))
        topics = []
        for criteria in rows:
            nouns = criteria.get('type_terms') or criteria.get('title_terms')
            if nouns:
                topics.append(list(dict.fromkeys([*nouns, *criteria.get('search_terms', [])])))
        return topics


def category_links(page, topics, allowed, limit=4):
    links = {}
    for anchor in page.css('a[href]'):
        url = urljoin(page.url, anchor.attrib.get('href', ''))
        parts = urlsplit(url)
        if parts.query or parts.fragment or not re.fullmatch(r'/[0-9]+-[a-z0-9-]+/?', parts.path):
            continue
        if url != page.url and allowed(url):
            links[url] = plain(parts.path.replace('-', ' ') + ' ' + anchor.get_all_text())
    selected = []
    for terms in topics:
        ranked = [(sum(term_matches(t, label) for t in terms), url) for url, label in links.items()]
        if ranked:
            score, best = max(ranked, key=lambda item: (item[0], -len(item[1])))
            if score and best not in selected:
                selected.append(best)
                if len(selected) == limit:
                    break
    return selected


class CatalogSpider(Spider):
    name='techstock-catalog'
    concurrent_requests=1
    concurrent_requests_per_domain=1
    max_blocked_retries=0
    # PacedSession checks the one shared robots snapshot on every GET.
    robots_txt_obey=False
    logging_level=logging.INFO
    def __init__(self,source,category,rules,limit,start_url=None):
        self.source=source; self.category=category; self.rules=rules; self.limit=limit
        self.download_delay=0; self.start_urls=[start_url or source.CATEGORIES[category]]
        self.allowed_domains={urlsplit(source.ROOT).hostname}
        self.main_limit = limit
        self.topics = recent_product_topics() if category == 'accessories' else []
        self.seen=set(); self.category_pages=0; self.halted=False
        self.report=dict(retailer=source.__name__.split('.')[-1],category=category,pages_visited=0,products_discovered=0,
            products_extracted=0,inserted=0,updated=0,superseded=0,errors=[],skipped_by_robots=0)
        super().__init__()
    def configure_sessions(self,manager): manager.add('default',PacedSession(self.source.ROOT))
    def allowed(self,url):
        return allowed_url(self.source.ROOT, url) and self.rules.can_fetch(url,USER_AGENT)
    async def is_blocked(self,response):
        blocked=await super().is_blocked(response)
        if blocked:
            self.halted=True; self.report['errors'].append('blocked_http_'+str(response.status)); self.pause()
        return blocked
    async def on_error(self,request,error):
        self.report['errors'].append(type(error).__name__+': '+str(error)[:100])
        if isinstance(error, SourceUnavailable) and str(error) in {
            'http_401', 'http_403', 'http_429', 'http_503', 'retailer_paused', 'retailer_challenge'
        }:
            self.halted = True
            self.pause()
    async def parse(self,response):
        self.report['pages_visited']+=1; self.category_pages+=1
        if self.halted or response.status!=200: return
        if self.category_pages == 1 and self.topics:
            topics = category_links(response, self.topics, self.allowed, limit=4)
            self.main_limit = max(1, self.limit - 5 * len(topics))
            for url in topics:
                yield self.source_request(response, url, self.parse_topic)
        selector='a.product-thumbnail::attr(href), a.product-item-link::attr(href), h2.product-title a::attr(href)'
        links=response.css(selector).getall()
        if not links and settings.scraping_render_js and self.allowed(response.url):
            try:
                from scraping.render import render
                response=await asyncio.to_thread(render,response.url,next(iter(self.allowed_domains)),response)
                links=response.css(selector).getall()
            except Exception as exc: self.report['errors'].append('browser_render: '+str(exc)[:100])
        for link in links:
            url=urljoin(response.url,link)
            if url in self.seen or len(self.seen)>=self.main_limit: continue
            if not self.allowed(url): self.report['skipped_by_robots']+=1; continue
            self.seen.add(url); self.report['products_discovered']+=1
            yield self.source_request(response,url,self.parse_product)
        if len(self.seen)<self.main_limit and self.category_pages<settings.scraping_max_pages:
            link=response.css('a.next::attr(href), a.action.next::attr(href), link[rel="next"]::attr(href)').get()
            if link:
                url=urljoin(response.url,link)
                if self.allowed(url): yield self.source_request(response,url,self.parse)
                else: self.report['skipped_by_robots']+=1
    async def parse_topic(self, response):
        # One public listing page and a small product sample per requested topic.
        self.report['pages_visited'] += 1
        if self.halted or response.status != 200:
            return
        count = 0
        for link in response.css('a.product-thumbnail::attr(href), h2.product-title a::attr(href)').getall():
            url = urljoin(response.url, link)
            if len(self.seen) >= self.limit:
                break
            if url in self.seen or not self.allowed(url):
                continue
            self.seen.add(url)
            self.report['products_discovered'] += 1
            yield self.source_request(response, url, self.parse_product)
            count += 1
            if count >= 5:
                break

    def source_request(self,response,url,callback):
        # Browser-rendered selectors are not Spider responses; use an explicit request.
        from scrapling.spiders import Request
        return Request(url,callback=callback)
    async def parse_product(self,response):
        self.report['pages_visited']+=1
        if self.halted:
            return
        observed_at = response.meta.get('observed_at', now())
        if response.status in (404, 410):
            await asyncio.to_thread(demote, response.url, observed_at)
        if response.status != 200:
            return
        try:
            try: record=parse_product(response,response.url,self.category,self.report['retailer'],observed_at=observed_at)
            except ValueError as exc:
                if str(exc)!='structured_product_metadata_missing' or not settings.scraping_render_js or not self.allowed(response.url): raise
                from scraping.render import render
                rendered=await asyncio.to_thread(render,response.url,next(iter(self.allowed_domains)),response)
                record=parse_product(rendered,response.url,self.category,self.report['retailer'],observed_at=observed_at)
            kind=await asyncio.to_thread(save, record); self.report[kind]+=1; self.report['products_extracted']+=1
            yield dict(url=record.url,price=str(record.price))
        except Exception as exc:
            self.report['errors'].append(str(exc)[:160]); self.logger.warning('Extraction failed: %s',exc)

def refresh(retailer='spacenet',category=None,limit=34,brand=None,listing=None):
    init_db(); reports=[]
    if retailer=='tunisianet':
        reports=[dict(retailer=retailer,category=category or 'all',products_extracted=0,errors=['Collection disabled: terms Article 9 prohibits catalog reproduction; obtain retailer permission.'])]
    else:
        source={'mytek':mytek,'spacenet':spacenet,'scoop':scoop}[retailer]
        rules=get_rules(source.ROOT)
        if category and category not in source.CATEGORIES:
            raise ValueError('category_not_supported_by_retailer')
        for cat in ([category] if category else [c for c in source.CATEGORIES if c not in ('earphones','keyboard')]):
            if listing and listing != 'gaming': raise ValueError('unsupported_listing')
            if brand and listing: raise ValueError('ambiguous_listing')
            start_url=(getattr(source,'GAMING_CATEGORIES',{}).get(cat) if listing else None) or getattr(source,'BRAND_CATEGORIES',{}).get((cat,(brand or '').lower()),source.CATEGORIES[cat])
            if brand and start_url == source.CATEGORIES[cat]: raise ValueError('brand_category_not_supported_by_retailer')
            if listing and start_url == source.CATEGORIES[cat]: raise ValueError('listing_not_supported_by_retailer')
            if not rules.can_fetch(start_url,USER_AGENT):
                reports.append(dict(retailer=retailer,category=cat,errors=['robots_disallow'])); continue
            spider=CatalogSpider(source,cat,rules,limit,start_url=start_url); start=time.perf_counter()
            result=spider.start()
            spider.report['robots_disallowed_count']=result.stats.robots_disallowed_count
            spider.report['failed_requests_count']=result.stats.failed_requests_count
            if result.stats.robots_disallowed_count and not spider.report['pages_visited']:
                spider.report['errors'].append('robots_check_failed_or_disallowed; no requests made')
            spider.report['execution_seconds']=time.perf_counter()-start
            reports.append(spider.report)
            print(json.dumps(spider.report),flush=True)
            if spider.halted: break
    Path('data').mkdir(exist_ok=True)
    Path('data/scrape-'+retailer+'.json').write_text(json.dumps(reports,indent=2),encoding='utf-8')
    Path('data/scrape-'+retailer+'-'+now().strftime('%Y%m%dT%H%M%S')+'.json').write_text(json.dumps(reports,indent=2),encoding='utf-8')
    return reports

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--retailer',choices=['spacenet','scoop','mytek','tunisianet'],default='spacenet')
    p.add_argument('--category',choices=sorted(set(spacenet.CATEGORIES) | set(scoop.CATEGORIES) | set(mytek.CATEGORIES))); p.add_argument('--limit',type=int,default=34)
    a=p.parse_args()
    if not 1<=a.limit<=200: p.error('limit must be between 1 and 200 per category')
    print(json.dumps(refresh(a.retailer,a.category,a.limit),indent=2))
