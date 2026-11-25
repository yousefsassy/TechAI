"""Discover product pages on the retailer's own public navigation and sitemaps."""
import asyncio
from functools import partial
import logging
import re
import time
from datetime import timedelta
from urllib.parse import unquote, urljoin, urlsplit

from langsmith import traceable
from lxml import etree
from backend.config import settings
from backend.database import Session
from backend.models import DiscoveryIndex, now
from backend.services.catalog import safe_url, graphics_from_details, graphics_tier
from backend.services import shopping_metrics as metrics
from backend.services.shopping_criteria import Criteria
from backend.services.shopping_terms import term_matches, type_matches
from scraping import spacenet, scoop
from scraping.normalize import money, plain, stock
from scraping.parser import parse_product
from scraping.public_pages import (RULES, USER_AGENT, SourceUnavailable, allowed_url, cache_lifetime,
                                   get_page, get_page_shared, get_rules)
from scraping.refresh_catalog import demote, save

log = logging.getLogger(__name__)
SOURCES = {'spacenet': spacenet, 'scoop': scoop}
_RULES = RULES
_MENU_ROUTES = {}
_ROUTE_TYPES = {'mouse': 'souris optique'}
_LISTING_SELECTOR = 'article.product-miniature, .product-item, .product-miniature'
_PARSE_REJECTIONS = {
    'ambiguous_offers', 'ambiguous_product_metadata', 'invalid_price', 'malformed_offer',
    'malformed_price', 'missing_product_name', 'structured_product_metadata_missing',
    'unexpected_currency', 'unsafe_source_url', 'wrong_product_category',
}


def local_url(root, value):
    # Some retailer sitemaps publish their own links with the legacy HTTP scheme.
    url = urljoin(root + '/', value.strip())
    parts = urlsplit(url)
    if parts.scheme == 'http':
        url = parts._replace(scheme='https').geturl()
    if urlsplit(url).netloc != urlsplit(root).netloc or not safe_url(url):
        return None
    return url


def words_from_url(url):
    return plain(unquote(urlsplit(url).path.rsplit('/', 1)[-1]).replace('-', ' '))


def read_index(url, fetch):
    with Session() as db:
        saved = db.get(DiscoveryIndex, url)
        if saved and saved.collected_at >= now() - timedelta(hours=24):
            return saved.is_index, saved.entries
    page = fetch(url)
    if page is None:
        raise ValueError('sitemap_unavailable')
    if len(page.body) > 52_428_800:
        raise SourceUnavailable('sitemap_too_large')
    xml = etree.fromstring(page.body, etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))
    kind = etree.QName(xml).localname
    if kind not in ('sitemapindex', 'urlset'):
        raise SourceUnavailable('invalid_sitemap')
    entries = []
    bytes_saved = 0
    for node in xml.iterfind('./*/{*}loc'):
        if not node.text or len(node.text) > 2048:
            continue
        entry = node.text.strip()
        bytes_saved += len(entry.encode('utf-8')) + 5
        if len(entries) == 50000 or bytes_saved > 4_194_304:
            break
        entries.append(entry)
    index = kind == 'sitemapindex'
    with Session.begin() as db:
        db.merge(DiscoveryIndex(url=url, is_index=index, entries=entries, collected_at=now()))
    return index, entries


def source_search(retailer, criteria, known_urls, limit=4, seconds=95, on_verified=None, cancel=None):
    source = SOURCES[retailer]
    root = source.ROOT
    deadline = time.monotonic() + seconds
    report = dict(retailer=retailer, checked=0, saved=0, status='no_match', urls=[],
                  rejected={}, stop_reason=None)
    types = criteria.get('type_terms') or criteria.get('title_terms', [])
    brand = criteria.get('brand', '')
    models = criteria.get('title_terms', [])
    eligible = Criteria(criteria)
    checked = set()
    max_checks = min(7, limit + 3)

    def active():
        if cancel is not None and cancel.is_set():
            raise SourceUnavailable('cancelled')
        if time.monotonic() >= deadline:
            raise SourceUnavailable('time_limit')

    def rejected(reason):
        report['rejected'][reason] = report['rejected'].get(reason, 0) + 1

    def transport(url, *args, **kwargs):
        if cancel is not None:
            kwargs.setdefault('cancel', cancel)
        return get_page(url, *args, **kwargs)

    def rules_snapshot():
        return get_rules(root, deadline, fetch=transport, cache=_RULES)

    def permitted(url, rules):
        return allowed_url(root, url) and local_url(root, url) == url and rules.can_fetch(url, USER_AGENT)

    def fetch(url):
        active()
        rules = rules_snapshot()
        if not permitted(url, rules):
            raise SourceUnavailable('robots_disallow')
        def request(interest):
            kwargs = {'cancel': interest} if interest is not None else {}
            return transport(url, float(rules.crawl_delay(USER_AGENT) or 0), deadline, **kwargs)
        return get_page_shared((url, id(rules)), request, deadline, cancel)

    def fetch_listing(url):
        page = fetch(url)
        if page.status == 200:
            return page
        if page.status in (301, 302, 303, 307, 308, 404, 410):
            rejected('listing_http_' + str(page.status))
            return None
        raise SourceUnavailable(f'http_{page.status}')

    def relevant(title):
        return (not types or type_matches(types, title)) and all(term_matches(t, plain(title)) for t in models)

    def collect(url):
        if url in checked or len(checked) >= max_checks or not permitted(url, rules_snapshot()):
            return
        checked.add(url)
        requested = now()
        page = fetch(url)
        observed_at = getattr(page, 'meta', {}).get('observed_at', requested)
        if page.status in (404, 410):
            demote(url, observed_at)
            rejected('pdp_removed')
            return
        if page.status in (301, 302, 303, 307, 308):
            rejected('pdp_redirect')
            return
        if page.status != 200:
            raise SourceUnavailable(f'http_{page.status}')
        report['checked'] += 1
        try:
            with metrics.stage('parse'):
                record = parse_product(page, url, criteria.get('category') or 'discovered', retailer,
                                       observed_at=observed_at)
        except (ValueError, KeyError, TypeError) as exc:
            reason = str(exc)
            rejected(reason if reason in _PARSE_REJECTIONS else 'invalid_product_metadata')
            metrics.increment('pdp_rejected')
            return
        specs = record.specifications
        if types and not type_matches(types, record.name):
            rejected('wrong_product')
            metrics.increment('pdp_rejected')
            return
        active()
        with metrics.stage('commit'):
            saved = save(record)
        if saved == 'superseded':
            rejected('superseded_observation')
            return
        if (record.stock_status != 'in_stock' or not eligible.price_matches(record.price)
                or not eligible.matches_product(name=record.name, category=record.category,
                        brand=specs.get('brand'), model=record.model, raw=record.raw, ram=specs.get('ram_gb'),
                        storage=specs.get('storage_gb'), graphics=graphics_from_details(record.name, record.raw))):
            rejected('ineligible_offer')
            metrics.increment('pdp_rejected')
            return
        report['saved'] += 1
        report['urls'].append(record.url)
        metrics.increment('pdp_accepted')
        if on_verified:
            on_verified(record.url)

    def candidate_score(label):
        text = plain(label)
        features = [*criteria.get('search_terms', []), *criteria.get('preferred_terms', [])]
        if criteria.get('preference'):
            features.append(criteria['preference'])
        gpu = graphics_tier(label) if criteria.get('gpu_priority') else 0
        return (max(gpu, 0) + 4 * bool(brand and term_matches(brand, text))
                + 12 * sum(term_matches(t, text) for t in set(features)))

    def menu_routes(rules):
        cached = _MENU_ROUTES.get(root)
        if cached and cached[0] > time.monotonic():
            routes = cached[1]
        else:
            home = fetch_listing(root + '/')
            if home is None:
                return []
            routes = {}
            for a in home.css('a[href]'):
                url = local_url(root, a.attrib['href'])
                if url and re.fullmatch(r'/\d+-[\w-]+/?', urlsplit(url).path):
                    routes[url] = words_from_url(url) + ' ' + a.get_all_text()[:120]
                if len(routes) >= 2000:
                    break
            headers = getattr(home, 'headers', {})
            ttl = cache_lifetime(headers, 3600)
            if ttl:
                _MENU_ROUTES[root] = (time.monotonic() + ttl, routes)
        return [url for url, label in sorted(routes.items(), key=lambda pair: -candidate_score(pair[1]))
                if types and type_matches(types, label) and permitted(url, rules)]

    def listing_choices(listing, rules):
        choices = {}
        for card in listing.css(_LISTING_SELECTOR):
            link = card.css('h2 a, h3 a, a.product-item-link, a.product-thumbnail')
            if not link:
                continue
            url = local_url(root, link[0].attrib.get('href', ''))
            if not url or not permitted(url, rules):
                continue
            title = link[0].get_all_text().strip() or words_from_url(url)
            if not relevant(title):
                continue
            card_text = card.get_all_text()
            label = plain(title + ' ' + card_text)
            price_hint = card.css('[itemprop="price"]::attr(content), .price::text').get()
            try:
                listing_price = money(price_hint)
            except ValueError:
                listing_price = None
            over_budget = listing_price is not None and not eligible.price_matches(listing_price)
            choices[url] = (over_budget, candidate_score(label) + 8 * (stock(card_text) == 'in_stock'))
        return sorted(choices, key=lambda url: (choices[url][0], -choices[url][1])), choices

    def done():
        return report['saved'] >= limit or len(checked) >= max_checks

    try:
        active()
        rules = rules_snapshot()
        for url in known_urls[:2]:
            collect(url)
        if criteria.get('_verify_only') and report['saved']:
            report.update(status='checked', stop_reason='known_offer_verified')
            return report

        cat = criteria.get('category')
        route_type = cat or next((name for name, label in _ROUTE_TYPES.items()
                                  if types and type_matches(types, label)), None)
        brand_key = plain(brand)
        brand_routes = dict(getattr(source, 'BRAND_CATEGORIES', {}))
        if settings.scraping_validated_routes:
            brand_routes.update(getattr(source, 'DISCOVERY_BRANDS', {}))
        seed = brand_routes.get((route_type, brand_key))
        if not seed and (criteria.get('gpu_priority') or criteria.get('dedicated_gpu')):
            seed = source.GAMING_CATEGORIES.get(cat)
        if not seed and settings.scraping_validated_routes:
            seed = getattr(source, 'DISCOVERY_CATEGORIES', {}).get(route_type)
        if not seed:
            seed = source.CATEGORIES.get(cat)
        routes = [seed] if seed and permitted(seed, rules) else menu_routes(rules)
        listings_left = 2
        deferred = []
        visited_routes = set()
        while routes and listings_left and not done():
            active()
            url = routes.pop(0)
            if url in visited_routes:
                continue
            visited_routes.add(url)
            listings_left -= 1
            listing = fetch_listing(url)
            if listing is None or not listing.css(_LISTING_SELECTOR):
                rejected('unusable_listing_route')
                if url == seed and listings_left:
                    routes = menu_routes(rules)
                continue
            candidates, choices = listing_choices(listing, rules)
            nxt = listing.css('a.next::attr(href), link[rel="next"]::attr(href)').get()
            next_url = local_url(root, urljoin(listing.url, nxt)) if nxt else None
            if next_url and next_url not in visited_routes and permitted(next_url, rules):
                routes.insert(0, next_url)
            elif not candidates and url == seed and listings_left:
                routes.extend(route for route in menu_routes(rules) if route not in visited_routes)
            # If a second listing can be checked, reserve two PDP attempts and
            # defer known over-budget hints. PDP evidence always decides eligibility.
            has_next = bool(listings_left and routes)
            early_budget = max(0, max_checks - len(checked) - (2 if has_next else 0))
            for candidate in candidates:
                if done():
                    break
                if candidate in checked:
                    continue
                if has_next and (choices[candidate][0] or early_budget == 0):
                    deferred.append(candidate)
                    continue
                collect(candidate)
                early_budget -= 1

        pending = [local_url(root, u) for u in rules.sitemaps]
        visited = set()
        while pending and len(visited) < 6 and not done():
            active()
            url = pending.pop(0)
            if not url or url in visited or not permitted(url, rules):
                continue
            visited.add(url)
            try:
                index, entries = read_index(url, fetch_listing)
            except (ValueError, etree.XMLSyntaxError):
                rejected('invalid_sitemap')
                continue
            if index:
                pending.extend(local_url(root, child) for child in
                               sorted(entries, key=lambda child: 'product' not in child.lower()))
                continue
            candidates = []
            for entry in entries:
                if '.html' not in entry:
                    continue
                title = words_from_url(entry)
                product_url = local_url(root, entry)
                if product_url and relevant(title):
                    candidates.append((candidate_score(title), product_url))
            for _, product_url in sorted(candidates, reverse=True):
                collect(product_url)
                if done():
                    break
        for url in deferred:
            if done():
                break
            collect(url)
        report['status'] = 'checked' if report['checked'] else 'no_match'
        report['stop_reason'] = 'target_met' if report['saved'] >= limit else (
            'pdp_budget' if len(checked) >= max_checks else 'routes_exhausted')
    except Exception as exc:
        report['status'] = str(exc) if isinstance(exc, SourceUnavailable) else 'unavailable'
        report['stop_reason'] = report['status']
        log.warning('Discovery %s: %s', retailer, type(exc).__name__)
    return report

@traceable(name='discover_and_scrape_tunisian_offers', run_type='tool')
async def discover_products(criteria, cached, force=False, on_verified=None):
    from scraping.lookup import join

    # The known URLs determine actual work for a forced recheck.
    lookup_key = dict(criteria)
    if force:
        lookup_key['known_urls'] = sorted({p['product_url'] for p in cached})

    async def load(operation):
        loop = asyncio.get_running_loop()
        callback = partial(loop.call_soon_threadsafe, operation.publish)

        async def one(retailer):
            urls = [p['product_url'] for p in cached if p['supplier'] == retailer]
            lookup = dict(criteria, _verify_only=bool(urls and force))
            return await asyncio.to_thread(
                source_search, retailer, lookup, urls, on_verified=callback, cancel=operation.cancelled,
            )

        return await asyncio.gather(*(
            one(retailer) for retailer in SOURCES if retailer not in criteria.get('recent_retailers', [])
        ))

    return await join(lookup_key, force, load, on_verified)
