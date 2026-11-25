"""Session-owned shopping conversations over verified catalog records."""
import hashlib
import json
from contextlib import nullcontext
import asyncio
import re
import json
import uuid
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from datetime import datetime, timedelta
from decimal import Decimal

from langsmith import traceable
from sqlalchemy import Text, func, select, type_coerce
from sqlalchemy.orm import joinedload

from backend.config import settings
from backend.database import Session
from backend.models import CartItem, Offer, Product, ShoppingConversation, ShoppingMessage, now
from backend.services.catalog import product_page, serialize, graphics_tier, graphics_from_details
from scraping.normalize import plain, category_name_valid
from backend.services import shopping_agent as agent, shopping_metrics as metrics, llm
from backend.services.price_comparison import comparison_groups, identity, mark_verified, recommendation_evidence
from backend.services.shopping_intent import (
    CHEAPEST, cart_action, displayed_positions, explicit_features, feature_refinement, get_public_plan,
    known_search, named_pair, price_comparison, remember_public_plan, shown_position, simple_search,
)
from backend.services.title_index import title_candidates
from scraping.discovery import discover_products
from backend.services import shopping_transactions as transactions
from backend.models import ShoppingTurn
from backend.services.shopping_terms import explicit_absences, literal_terms, new_title_terms, product_phrase, term_matches, type_matches
from backend.services.shopping_criteria import CATEGORIES, Criteria, parse_budget, query_text

BRANDS = ('apple', 'lenovo', 'dell', 'hp', 'asus', 'acer', 'samsung', 'msi', 'huawei', 'logitech', 'sony', 'jbl', 'razer', 'hyperx')
TEXT = {
    'en': dict(currency='Prices are listed in Tunisian dinars (TND). What is your maximum budget in TND? I cannot assume an exchange rate.',
        ambiguous_budget='Could you restate the maximum budget as an exact amount in TND?',
        cart_total='Your cart total is {total} TND, excluding shipping.', search_first='Search for products first, then tell me which one to add.',
        missing_number='That product number is not in the latest results.', added='Added to your cart. Estimated total: {total} TND.',
        removed='Removed from your cart. Estimated total: {total} TND.', no_cart='There is no product at that position in your cart.',
        category='What product or model should I look for in the saved catalog?',
        found='I found {count} saved offer(s) listed in stock. Lowest recorded price: {price} TND for {name} at {supplier}.',
        one=' Only one retailer has matching saved offers.', none='No saved offers from the last {hours} hours match these requirements. Try a higher TND budget or broader preferences.',
        unavailable=' Could not refresh: {retailers}.', pc_kind='Do you mean a laptop or desktop PC? You can also give a maximum budget in TND.',
        no_catalog='No saved {category} offers with confirmed stock from the last {hours} hours. The catalog updates in the background; try again after the next refresh.',
        no_brand='I found current {category} offers, but none explicitly list {brand}. You can ask for any brand.',
        no_stock_brand='I found a {brand} listing, but the retailer does not confirm it is in stock. I cannot recommend it as available.',
        no_budget='The lowest matching listed price is {price} TND, above your {budget} TND budget.',
        no_graphics='No saved offers matching your filters explicitly list a dedicated GPU. Gaming laptop pages are included in the daily refresh, but coverage may be incomplete.',
        strongest='Among the saved matching laptop offers, {name} lists {gpu} at {supplier} for {price} TND. These cards are ordered by GPU model tier, an estimate rather than measured performance; laptop GPU power limits and cooling can change the result. Verify specs, price, and stock with the retailer.'),
    'fr': dict(currency='Les prix sont en dinars tunisiens (TND). Quel est votre budget maximal en TND ? Je ne peux pas supposer un taux de change.',
        ambiguous_budget='Pouvez-vous préciser le montant maximal en TND ?',
        cart_total='Le total de votre panier est de {total} TND, hors livraison.', search_first='Cherchez d’abord des produits, puis indiquez lequel ajouter.',
        missing_number='Ce numéro ne figure pas dans les derniers résultats.', added='Ajouté au panier. Total estimé : {total} TND.',
        removed='Retiré du panier. Total estimé : {total} TND.', no_cart='Aucun produit à cette position dans le panier.',
        category='Quel produit ou modèle recherchez-vous dans le catalogue ?',
        found='J’ai trouvé {count} offre(s) indiquée(s) en stock au dernier relevé. Le prix le plus bas est de {price} TND chez {supplier} pour {name}.',
        one=' Un seul vendeur a des offres enregistrées correspondantes.', none='Aucune offre enregistrée au cours des dernières {hours} heures ne correspond. Essayez un budget TND plus élevé ou des critères plus larges.',
        unavailable=' Actualisation impossible pour : {retailers}.', pc_kind='Vous cherchez un PC portable ou de bureau ? Vous pouvez préciser votre budget maximal en TND.',
        no_catalog='Aucune offre enregistrée pour {category} avec stock confirmé au cours des dernières {hours} heures. Le catalogue se met à jour en arrière-plan ; réessayez après la prochaine mise à jour.',
        no_brand='J’ai trouvé des offres récentes pour {category}, mais aucune ne mentionne clairement {brand}. Vous pouvez demander toutes les marques.',
        no_stock_brand='J’ai trouvé une annonce {brand}, mais le vendeur ne confirme pas sa disponibilité. Je ne peux pas la recommander comme disponible.',
        no_budget='Le prix affiché le plus bas correspondant est de {price} TND, au-dessus de votre budget de {budget} TND.',
        no_graphics='Aucune offre enregistrée correspondant à vos critères ne mentionne un GPU dédié. Les PC portables gaming sont collectés chaque jour, mais la couverture peut être incomplète.',
        strongest='Parmi les PC portables enregistrés correspondants, {name} mentionne {gpu} chez {supplier} à {price} TND. Le classement par modèle de GPU est indicatif, pas un benchmark ; la puissance et le refroidissement peuvent modifier les performances. Vérifiez la fiche, le prix et le stock chez le vendeur.'),
    'ar': dict(currency='الأسعار معروضة بالدينار التونسي (TND). ما الحد الأقصى لميزانيتك بالدينار؟ لا يمكنني افتراض سعر الصرف.',
        ambiguous_budget='هل يمكنك تحديد المبلغ الأقصى بالدينار التونسي؟',
        cart_total='إجمالي سلتك {total} دينار تونسي، دون تكلفة التوصيل.', search_first='ابحث عن المنتجات أولاً، ثم أخبرني أي منتج تريد إضافته.',
        missing_number='رقم المنتج غير موجود في نتائج البحث الأخيرة.', added='تمت الإضافة إلى السلة. الإجمالي التقديري: {total} دينار تونسي.',
        removed='تمت إزالة المنتج. الإجمالي التقديري: {total} دينار تونسي.', no_cart='لا يوجد منتج بهذا الترتيب في السلة.',
        category='ما المنتج أو الطراز الذي تريد البحث عنه في الكتالوج؟',
        found='وجدت {count} عروض متوفرة عند آخر تحديث. أقل سعر مسجّل هو {price} دينار تونسي لدى {supplier} للمنتج {name}.',
        one=' يوجد متجر واحد فقط لديه عروض محفوظة مطابقة.', none='لا توجد عروض محفوظة خلال آخر {hours} ساعة تطابق شروطك. جرّب ميزانية أعلى بالدينار التونسي أو وسّع شروط البحث.',
        unavailable=' تعذّر تحديث بيانات: {retailers}.', pc_kind='هل تقصد حاسوباً محمولاً أم مكتبياً؟ يمكنك أيضاً تحديد ميزانيتك القصوى بالدينار التونسي.',
        no_catalog='لا توجد عروض محفوظة لفئة {category} مع توفر مؤكد خلال آخر {hours} ساعة. تُحدّث البيانات في الخلفية؛ حاول بعد التحديث التالي.',
        no_brand='وجدت عروضاً حديثة لفئة {category}، لكن لا يوجد عرض يذكر {brand} بوضوح. يمكنك البحث دون تحديد العلامة.',
        no_stock_brand='وجدت إعلاناً لمنتج {brand}، لكن المتجر لم يؤكد توفره. لا أستطيع تقديمه كخيار متوفر.',
        no_budget='أقل سعر معروض مطابق هو {price} دينار تونسي، أعلى من ميزانيتك {budget} دينار.',
        no_graphics='لا يذكر أي عرض محفوظ مطابق لشروطك بطاقة رسوميات منفصلة بوضوح. تُجمع صفحات الحواسيب المخصصة للألعاب يومياً، لكن التغطية قد تكون ناقصة.',
        strongest='بين عروض الحواسيب المحمولة المحفوظة المطابقة، يذكر {name} بطاقة {gpu} لدى {supplier} بسعر {price} دينار تونسي. الترتيب تقريبي بحسب فئة البطاقة وليس اختبار أداء؛ قدرة البطاقة والتبريد قد يغيّران النتيجة. تحقّق من المواصفات والسعر والتوفر لدى المتجر.'),
}
def say(language, key, **values):
    return TEXT[language][key].format(**values)
_refresh_locks = {category: threading.Lock() for category in CATEGORIES}
_source_refresh_locks = {(retailer, category): threading.Lock() for category in (*CATEGORIES, 'accessories') for retailer in ('spacenet', 'scoop')}
_retry_after = {}
_brand_refresh_at = {}
_gaming_refresh_at = {}

def extract(message, previous, language='en'):
    """Read explicit constraints; follow-ups inherit the last successful search."""
    t = query_text(message)
    criteria = dict(previous or {})
    strongest_gpu = bool(re.search(r'\b(?:strongest|highest|most powerful|best|fastest|puissant|meilleur)\b.{0,35}\b(?:gpu|graphics|carte graphique)\b|(?:اقوى|أقوى|افضل|أفضل).{0,35}(?:رسوميات|شاشة)|\b(?:gpu|graphics)\s+(?:power|performance)\b', t))
    if strongest_gpu:
        criteria = {}  # An unqualified best-GPU search is not constrained by an old brand/budget.
    # A fresh generic-PC request must not silently inherit a prior brand or budget.
    generic_pc = bool(re.search(r'\b(?:pc|computer|ordinateur)\b', t)) and not any(
        word in t for category in ('laptop', 'desktop') for word in CATEGORIES[category])
    if generic_pc and re.search(r'\b(?:i want|i need|looking for|je cherche|new search)\b|ابحث عن', t):
        criteria = {}
    # In “mouse for my laptop” the laptop is a compatibility requirement.
    product_text = product_phrase(t)
    accessory = re.search(r'\b(?:stands?|supports?|chargers?|chargeurs?|cases?|coques?|housses?)\b', product_text)
    if accessory and not re.search(r'\b(?:with|avec)\b', product_text[:accessory.start()]):
        product_text = ''
    for category, words in CATEGORIES.items():
        if any(re.search(r'(?<!\w)' + re.escape(plain(w)) + r's?(?!\w)', product_text) for w in words):
            if criteria.get('category') != category:
                criteria = {'category': category}
            break
    if re.search(r'(?<!\w)(?:airpods|ipads?|ايربود[زس]?|ايباد|ابل)(?!\w)', product_text):
        criteria['brand'] = 'apple'
    else:
        localized_brands = {'لوجيتك': 'logitech', 'لوجيتش': 'logitech', 'لوجيتيك': 'logitech', 'سامسونج': 'samsung', 'لينوفو': 'lenovo'}
        for alias, brand in localized_brands.items():
            if alias in t:
                criteria['brand'] = brand
                break
        for brand in BRANDS:
            if re.search(r'\b' + brand + r'\b', t):
                criteria['brand'] = brand
                break
    if re.search(r'\b(any brand|all brands|n.importe quelle marque)\b', t):
        criteria.pop('brand', None)
    try:
        value, _ = parse_budget(message, language)
    except ValueError as exc:
        return criteria, say(language, str(exc))
    if value is not None:
        criteria['budget'] = str(value)
    if match := re.search(r'(?<!\w)(?:airpods|ايربودز|ايربودس)\s+(pro|max|برو|ماكس)?\s*(\d)?(?!\w)', t):
        edition = {'برو': 'pro', 'ماكس': 'max'}.get(match[1], match[1])
        if edition:
            criteria['airpods_edition'] = edition
        if match[2]:
            criteria['airpods_generation'] = int(match[2])
    absent = explicit_absences(t)
    if 'dedicated_gpu' in absent:
        for key in ('dedicated_gpu', 'gpu_priority', 'gpu_model'):
            criteria.pop(key, None)
        criteria['forbid_dedicated_gpu'] = True
    forbidden = list(dict.fromkeys([*criteria.get('forbidden_terms', []), *(
        term for term in absent if term != 'dedicated_gpu'
    )]))
    if forbidden:
        criteria['forbidden_terms'] = forbidden
        if criteria.get('preference') in forbidden:
            criteria.pop('preference')
        if 'search_terms' in criteria:
            criteria['search_terms'] = [term for term in criteria['search_terms'] if term not in forbidden]
    for term in ('rgb', 'pro', 'noise cancelling', 'anc', '16 gb', '32 gb', '512 gb', '1 tb'):
        if term not in absent and re.search(r'(?<!\w)' + term + r'(?!\w)', t):
            criteria['preference'] = term
            criteria.pop('soft_preference', None)
            if re.search(r'\b(?:preferably|ideally|de preference|idealement)\b.{0,14}' + term, t):
                criteria['soft_preference'] = True
    for term in ('bluetooth', 'wireless', 'sans fil', 'لاسلكي', 'لاسلكية', 'wired', 'filaire', 'mecanique', 'mechanical'):
        canonical = 'wireless' if term in ('sans fil', 'لاسلكي', 'لاسلكية') else term
        if canonical not in absent and re.search(r'(?<!\w)' + term + r'(?!\w)', t):
            criteria['preference'] = term
            criteria.pop('soft_preference', None)
            if re.search(r'\b(?:preferably|ideally|de preference|idealement)\b.{0,14}' + term, t):
                criteria['soft_preference'] = True
            break
    if 'برو' in t: criteria['preference'] = 'pro'
    if 'dedicated_gpu' not in absent and re.search(r'\b(?:dedicated|discrete)\s+(?:gpu|graphics)|\b(?:graphics card|gpu|carte graphique)\b|بطاقة\s*(?:رسوميات|شاشة)|كرت\s*(?:شاشة|شاشه)', t):
        criteria.pop('forbid_dedicated_gpu', None)
        criteria['dedicated_gpu'] = True
    if 'dedicated_gpu' not in absent and (strongest_gpu or (criteria.get('category') == 'laptop' and re.search(r'\b(best|meilleur)\b.*\b(gpu|graphics|carte graphique)\b', t))):
        criteria['gpu_priority'] = True
        criteria['dedicated_gpu'] = True
    if preference := criteria.get('preference'):
        criteria['forbidden_terms'] = [term for term in forbidden if not term_matches(term, plain(preference))]
        if not criteria['forbidden_terms']:
            criteria.pop('forbidden_terms')
    return criteria, None

@traceable(name='tunisian_retailer_refresh', run_type='tool')
def refresh_sources(category):
    """Refresh each permitted retailer before searching; throttle per source/category."""
    from scraping.refresh_catalog import refresh
    def refresh_one(retailer):
        with _source_refresh_locks[(retailer,category)]:
            key = (retailer, category)
            with Session() as db:
                latest = db.scalar(select(Offer.scraped_at).join(Offer.product).where(
                    Offer.supplier_name == retailer, Offer.product.has(category=category),
                    Offer.is_demo.is_(False)).order_by(Offer.scraped_at.desc()).limit(1))
            if latest and latest >= now() - timedelta(minutes=20):
                return dict(retailer=retailer, status='recent')
            if time.monotonic() < _retry_after.get(key, 0):
                return dict(retailer=retailer, status='unavailable')
            try:
                # Scoop earphone listings are paginated in small groups; visit enough
                # pages to include Apple products without using the disallowed search URL.
                limit = 18 if category == 'earphones' and retailer == 'scoop' else 8 if category == 'keyboard' else 5
                if category == 'accessories':
                    limit = 25
                report = refresh(retailer, category, limit)[0]
                count = report.get('products_extracted', 0)
                status = 'refreshed' if count else 'no_products'
                if report.get('errors') and not count: status = 'unavailable'
                if status == 'unavailable': _retry_after[key] = time.monotonic() + 300
                return dict(retailer=retailer, status=status, products=count)
            except Exception:
                _retry_after[key] = time.monotonic() + 300
                return dict(retailer=retailer, status='unavailable')
    with ThreadPoolExecutor(max_workers=2) as pool:
        from scraping import spacenet, scoop
        retailers = [name for name, source in [('spacenet', spacenet), ('scoop', scoop)] if category in source.CATEGORIES]
        results = list(pool.map(refresh_one, retailers))
    return results

@traceable(name='targeted_brand_refresh', run_type='tool')
def refresh_brand(category, brand):
    """Use a retailer's public brand category when the broad first page misses it."""
    from scraping import spacenet
    from scraping.refresh_catalog import refresh
    if (category, brand) not in spacenet.BRAND_CATEGORIES: return []
    key = (category, brand)
    with _refresh_locks[category]:
        if time.monotonic() < _brand_refresh_at.get(key, 0): return []
        _brand_refresh_at[key] = time.monotonic() + 1200
        try:
            report = refresh('spacenet', category, 5, brand=brand)[0]
            return [dict(retailer='spacenet', status='refreshed' if report.get('products_extracted') else 'unavailable')]
        except Exception:
            return [dict(retailer='spacenet', status='unavailable')]

@traceable(name='gaming_laptop_refresh', run_type='tool')
def refresh_gaming():
    """Populate GPU offers from public gaming laptop categories on the daily job."""
    from scraping.refresh_catalog import refresh
    outcomes=[]
    for retailer, limit in (('spacenet', settings.spacenet_gaming_listing_limit), ('scoop', 40)):
        key=(retailer,'gaming_laptops')
        if time.monotonic() < _gaming_refresh_at.get(key,0): continue
        _gaming_refresh_at[key]=time.monotonic()+1200
        try:
            report=refresh(retailer,'laptop',limit,listing='gaming')[0]
            outcomes.append(dict(retailer=retailer,status='refreshed' if report.get('products_extracted') else 'unavailable'))
        except Exception:
            outcomes.append(dict(retailer=retailer,status='unavailable'))
    return outcomes

def _catalog_query(resolved, cutoff, *, recheck=False, include_unavailable=False):
    query = select(
        Offer.id, Offer.product_id, Offer.supplier_name.label('supplier'), Offer.price_tnd,
        Offer.stock_status, Offer.product_url, Offer.scraped_at,
        Product.name, Product.category, Product.brand, Product.model, Product.ram_gb,
        Product.storage_gb, type_coerce(Product.raw_specifications, Text).label('raw_specifications'),
    ).join(Offer.product).where(Offer.is_demo.is_(False), Offer.price_tnd > 0)
    if not recheck:
        query = query.where(Offer.scraped_at >= cutoff)
        if not include_unavailable:
            query = query.where(Offer.stock_status == 'in_stock')
        if resolved.budget is not None:
            query = query.where(Offer.price_tnd < resolved.budget if resolved.fields.get('exclusive_budget')
                                else Offer.price_tnd <= resolved.budget)
    if resolved.category and not resolved.types:
        categories = [resolved.category]
        if resolved.category in ('keyboard', 'headset'):
            categories.append('accessories')
        query = query.where(Product.category.in_(categories))
    return title_candidates(query, resolved.types)


def _decode_evidence(value):
    try:
        evidence = json.loads(value) if value else {}
    except (ValueError, TypeError):
        return {}
    return evidence if isinstance(evidence, dict) else {}


# Bound normalized public evidence by both entry count and input size.
# The full value is the key, so replacement/deletion cannot return old evidence.
_cached_evidence = lru_cache(maxsize=256)(_decode_evidence)


def _eligible_candidates(db, resolved, cutoff, *, recheck=False, include_unavailable=False, instrument=False):
    query = _catalog_query(resolved, cutoff, recheck=recheck, include_unavailable=include_unavailable)
    examined = 0
    for examined, row in enumerate(db.connection().execute(query).mappings(), 1):
        if not product_page(row['product_url']):
            continue
        value = row['raw_specifications'] or ''
        raw = _cached_evidence(value) if len(value) <= 8192 else _decode_evidence(value)
        graphics = graphics_from_details(row['name'], raw)
        if not resolved.matches_product(name=row['name'], category=row['category'], brand=row['brand'],
                                        model=row['model'], raw=raw, ram=row['ram_gb'],
                                        storage=row['storage_gb'], graphics=graphics):
            continue
        yield dict(row, graphics=graphics, raw_specifications=raw)
    if instrument:
        metrics.increment('candidates_examined', examined)


def _catalog_rank(product, criteria, budget, observed_at):
    price, offer_id = product['price_tnd'], product['id']
    if criteria.get('sort') == 'price':
        return price, offer_id
    if criteria.get('gpu_priority'):
        return -graphics_tier(product['graphics']), price, offer_id
    preferred = criteria.get('preferred_terms', ())
    if criteria.get('soft_preference') and criteria.get('preference'):
        preferred = (*preferred, criteria['preference'])
    recommend = criteria.get('sort') == 'recommend'
    if not preferred and not recommend:
        return price, offer_id
    has_mouse_evidence = recommend and re.search(r'\b(?:mouse|souris)\b', product['name'], re.I)
    detail = ''
    if preferred or has_mouse_evidence:
        raw = product['raw_specifications'] or {}
        detail = plain(' '.join((product['name'], str(raw.get('description') or '')[:500],
                                 str(raw.get('attributes') or {}))))
    score = sum(term_matches(term, detail) for term in preferred)
    if recommend:
        capability = len(recommendation_evidence(product, detail)) if has_mouse_evidence else 0
        if product['category'] in ('laptop', 'desktop', 'tablet'):
            capability += min(product['ram_gb'] or 0, 32) / 16 + min(product['storage_gb'] or 0, 1024) / 512
        if criteria.get('intended_use') == 'programming':
            capability += 2 * ((product['ram_gb'] or 0) >= 16) + ((product['storage_gb'] or 0) >= 512)
        value = min(2, capability * float(budget or price) / (4 * float(price)))
        age = (observed_at - product['scraped_at']).total_seconds() / 3600
        freshness = max(0, 1 - age / settings.catalog_max_age_hours)
        return -round(4 * score + capability + value + .15 * freshness, 3), price, offer_id
    return -score, price, offer_id


@traceable(name='catalog_product_search', run_type='tool')
def search(criteria, *, recheck=False):
    resolved = Criteria(criteria)
    observed_at = now()
    cutoff = observed_at - timedelta(hours=settings.catalog_max_age_hours)
    with Session() as db:
        # SQLite's legacy driver otherwise gives sequential SELECTs different snapshots.
        db.connection().exec_driver_sql('BEGIN')
        with metrics.stage('catalog_read'):
            candidates = list(_eligible_candidates(db, resolved, cutoff, recheck=recheck, instrument=True))
        if not candidates:
            return []
        def rank(row):
            return _catalog_rank(row, criteria, resolved.budget, observed_at)
        with metrics.stage('ranking'):
            if recheck:
                winning_ids = [row['id'] for row in sorted(candidates, key=rank)[:12]]
                groups = []
            else:
                for row in candidates:
                    raw = row['raw_specifications'] or {}
                    row['_identity'] = identity(row, raw.get('metadata') or {}, row['product_id'])
                groups = sorted(comparison_groups(candidates), key=lambda group: rank(group.best()))[:12]
                winning_ids = [group.best()['id'] for group in groups]
        with metrics.stage('catalog_hydration'):
            offers = {offer.id: serialize(offer) for offer in db.scalars(
                select(Offer).options(joinedload(Offer.product)).where(Offer.id.in_(winning_ids))
            )}
        metrics.increment('rows_hydrated', len(offers))
        if recheck:
            return [offers[offer_id] for offer_id in winning_ids]
        results = []
        for group in groups:
            card = group.card()
            results.append(dict(offers[card['id']], **{
                key: card[key] for key in ('offers', 'group_id', 'match_reason', 'match_rule_version')
            }))
        return results


def explain_empty(criteria, language):
    """Diagnose empty results using eligibility without hydrating or ranking product cards."""
    category = criteria.get('category')
    if not category and criteria.get('title_terms'):
        label = ' '.join(criteria['title_terms'])
        return {
            'en': f'I couldn’t find “{label}” with your filters in the saved Tunisian listings.',
            'fr': f'Je n’ai trouvé aucune offre pour « {label} » avec vos critères dans le catalogue tunisien enregistré.',
            'ar': f'لم أجد عروضاً مطابقة لـ «{label}» حسب شروطك في الكتالوج التونسي المحفوظ.'
        }[language]
    if not category:
        return say(language, 'none', hours=settings.catalog_max_age_hours)
    cutoff = now() - timedelta(hours=settings.catalog_max_age_hours)
    with Session() as db:
        brand = criteria.get('brand')
        if brand:
            branded = Criteria({'category': category, 'brand': brand})
            if not next(_eligible_candidates(db, branded, cutoff), None):
                if next(_eligible_candidates(db, branded, cutoff, include_unavailable=True), None):
                    return say(language, 'no_stock_brand', brand=brand)
                if next(_eligible_candidates(db, Criteria({'category': category}), cutoff), None):
                    return say(language, 'no_brand', category=category, brand=brand)
                return say(language, 'no_catalog', category=category, hours=settings.catalog_max_age_hours)
        if not next(_eligible_candidates(db, Criteria({'category': category}), cutoff), None):
            return say(language, 'no_catalog', category=category, hours=settings.catalog_max_age_hours)
        resolved = Criteria(criteria)
        if resolved.budget:
            filtered = Criteria({key: value for key, value in criteria.items() if key != 'budget'})
            matching = _eligible_candidates(db, filtered, cutoff)
            lowest = min((row['price_tnd'] for row in matching), default=None)
            if lowest and not resolved.price_matches(lowest):
                return say(language, 'no_budget', price=lowest, budget=criteria['budget'])
    if criteria.get('dedicated_gpu'):
        return say(language, 'no_graphics')
    return say(language, 'none', hours=settings.catalog_max_age_hours)

def cart(sid, *, db=None):
    with (nullcontext(db) if db is not None else Session()) as session:
        items = session.scalars(
            select(CartItem).where(CartItem.user_session_id == sid)
            .options(joinedload(CartItem.offer).joinedload(Offer.product)).order_by(CartItem.id)
        ).all()
        data = [dict(offer=serialize(i.offer), quantity=i.quantity,
                     subtotal_tnd=str(i.offer.price_tnd * i.quantity)) for i in items]
    return dict(items=data, total_tnd=str(sum((Decimal(i['subtotal_tnd']) for i in data), Decimal(0))),
                currency='TND', note='Estimated merchandise total; prices and stock may change at checkout. Shipping is excluded.')


@traceable(name='cart_update', run_type='tool')
def update_cart(sid, offer_id, quantity, *, db=None):
    with (nullcontext(db) if db is not None else transactions.transaction()) as session:
        item = session.scalar(select(CartItem).where(CartItem.user_session_id == sid, CartItem.offer_id == offer_id))
        if quantity == 0:
            if item:
                session.delete(item)
        else:
            offer = session.get(Offer, offer_id)
            if not offer or offer.stock_status != 'in_stock' or not product_page(offer.product_url):
                raise ValueError('Product is unavailable')
            if offer.scraped_at < now() - timedelta(hours=settings.catalog_max_age_hours):
                raise ValueError('Product price is stale; search again')
            if offer.available_quantity is not None and quantity > offer.available_quantity:
                raise ValueError('Requested quantity exceeds published stock')
            if item:
                item.quantity = quantity
            else:
                session.add(CartItem(user_session_id=sid, offer_id=offer_id, quantity=quantity))
        return cart(sid, db=session)

def conversations(sid):
    with Session() as db:
        rows = db.scalars(select(ShoppingConversation).where(ShoppingConversation.user_session_id == sid).order_by(ShoppingConversation.updated_at.desc())).all()
        return [dict(id=c.id, title=c.title, updated_at=c.updated_at.isoformat()) for c in rows]

def conversation(sid, cid, *, limit=None, before_message_id=None):
    with Session() as db:
        c = db.get(ShoppingConversation, cid)
        if not c or c.user_session_id != sid:
            raise ValueError('Conversation not found')
        query = select(ShoppingMessage).where(ShoppingMessage.conversation_id == cid)
        if limit is None and before_message_id is None:
            rows = db.scalars(query.order_by(ShoppingMessage.id)).all()
            return dict(id=c.id, title=c.title, messages=[
                dict(role=m.role, content=m.content, products=m.products) for m in rows
            ])
        if before_message_id is not None:
            query = query.where(ShoppingMessage.id < before_message_id)
        limit = limit or 4
        newest = db.scalars(query.order_by(ShoppingMessage.id.desc()).limit(limit + 1)).all()
        rows = newest[:limit]
        has_older = len(newest) > limit
        return dict(id=c.id, title=c.title, messages=[
            dict(id=m.id, role=m.role, content=m.content, products=m.products) for m in reversed(rows)
        ], has_older=has_older, next_before_message_id=rows[-1].id if has_older else None)


def validate_conversation(sid, cid):
    with Session() as db:
        owner = db.scalar(select(ShoppingConversation.user_session_id).where(ShoppingConversation.id == cid))
        if owner != sid:
            raise ValueError('Conversation not found')


def is_cart_command(message):
    return cart_action(message) is not None


def _cart_command(message, sid, last_ids, language='en', *, db=None):
    action = cart_action(message)
    if action is None:
        return None
    if action == 'total':
        return say(language, 'cart_total', total=cart(sid, db=db)['total_tnd'])

    text = plain(message)
    position = shown_position(text)
    index = 0 if position is None else position - 1
    if action == 'remove':
        items = cart(sid, db=db)['items']
        if not 0 <= index < len(items):
            return say(language, 'no_cart')
        result = update_cart(sid, items[index]['offer']['id'], 0, db=db)
        return say(language, 'removed', total=result['total_tnd'])

    if not last_ids:
        return say(language, 'search_first')
    if re.search(CHEAPEST, text):
        with (nullcontext(db) if db is not None else Session()) as cart_session:
            target = cart_session.scalar(select(Offer.id).where(Offer.id.in_(last_ids))
                                         .order_by(Offer.price_tnd, Offer.id).limit(1))
    else:
        target = last_ids[index] if 0 <= index < len(last_ids) else None
    if target is None:
        return say(language, 'missing_number')
    try:
        result = update_cart(sid, target, 1, db=db)
    except ValueError as exc:
        return str(exc)
    return say(language, 'added', total=result['total_tnd'])


async def interpret_turn(message, previous, language):
    explicit, issue = extract(message, {}, language)
    text = plain(message)
    generic_pc = not explicit.get('category') and re.search(r'\b(pc|computer|ordinateur)\b', product_phrase(message))
    if generic_pc and (not previous or re.search(r'i want|i need|looking for|je cherche|ابحث عن', text)):
        return explicit, issue

    subject = new_title_terms(message, explicit)
    category_changed = explicit.get('category') and explicit['category'] != previous.get('category')
    context = {} if category_changed or subject else previous
    criteria, issue = extract(message, context, language)
    model_codes = [word for word in literal_terms(message)
                   if re.fullmatch(r'[a-z]+[0-9]+[a-z0-9]*', word)]
    if explicit.get('category') and model_codes:
        explicit['title_terms'] = model_codes
        criteria['title_terms'] = model_codes
    if subject:
        criteria['title_terms'] = subject
    if issue:
        return criteria, issue

    return criteria, None


def _saved_turn(db, sid, turn_id, fingerprint):
    if turn_id is None:
        return None
    row = db.get(ShoppingTurn, (sid, turn_id))
    if row is None:
        return None
    if row.request_hash != fingerprint:
        raise transactions.ShoppingConflict('Turn ID already belongs to a different request.')
    assistant = db.get(ShoppingMessage, row.assistant_message_id)
    return dict(row.result, reply=assistant.content, products=assistant.products)


def _turn_input(sid, cid, turn_id, fingerprint):
    with Session() as db:
        replay = _saved_turn(db, sid, turn_id, fingerprint)
        if replay is not None or cid is None:
            return replay, None, {}, [], []
        c = db.get(ShoppingConversation, cid)
        if not c or c.user_session_id != sid:
            raise ValueError('Conversation not found')
        query = select(ShoppingMessage).where(ShoppingMessage.conversation_id == cid).order_by(ShoppingMessage.id.desc())
        recent = db.execute(query.with_only_columns(
            ShoppingMessage.id, ShoppingMessage.role, ShoppingMessage.content,
        ).limit(12)).all()
        history = [(role, content[:800]) for _, role, content in reversed(recent)]
        shown = []
        if c.last_offer_ids:
            cards = db.scalar(query.with_only_columns(ShoppingMessage.products)
                              .where(func.json_array_length(ShoppingMessage.products) > 0).limit(1))
            if cards:
                by_id = {p['id']: p for p in cards}
                shown = [by_id[offer_id] for offer_id in c.last_offer_ids if offer_id in by_id]
        return None, recent[0].id if recent else 0, c.criteria or {}, history, shown


def memory(sid, cid):
    _, _, criteria, history, shown = _turn_input(sid, cid, None, None)
    return criteria, history, shown


def localized(language, en, fr, ar):
    return {'en': en, 'fr': fr, 'ar': ar}[language]


def retailer_terms(criteria):
    if criteria.get('type_terms'):
        return criteria['type_terms']
    category = criteria.get('category')
    if category:
        return list(CATEGORIES.get(category, (category,))) + {
            'tablet':['tablette', 'galaxy tab'], 'earphones':['ecouteur','ecouteurs'],
            'desktop':['pc de bureau'], 'laptop':['pc portable gamer']
        }.get(category, [])
    return []


async def fallback_plan(message, previous, shown, language):
    text = plain(message)
    # Discard the dismissed topic in an explicit correction.
    last_clause = re.split(r'\b(?:forget|oublie)\b[^.!?;]*[.!?;]\s*', message, flags=re.I)[-1] or message
    if re.search(r'which (?:one|of)|among (?:them|these)|lequel|ايهما|أيهما', text):
        metric = 'gpu' if 'gpu' in text or 'graph' in text else 'price' if 'cheap' in text else 'features'
        return agent.CompareShown(metric=metric), previous, None
    if re.search(r"^(?:what(?:.s| is| are)|why|how (?:do|does|is|are)|explain|tell me about|quelle est|comment|pourquoi|ما الفرق|ما هو|اشرح)\b", text):
        provider = 'OpenRouter' if settings.llm_provider == 'openrouter' else 'OpenAI'
        info = localized(language, f'{provider} is unavailable for an open-ended answer. I can still look up products or compare the cards shown.',
                         f'{provider} est indisponible pour cette explication. Je peux chercher des produits ou comparer les fiches affichées.',
                         f'خدمة {provider} غير متاحة للإجابة العامة الآن. يمكنني البحث عن منتجات أو مقارنة العروض المعروضة.')
        return agent.GeneralAnswer(answer=info), previous, None
    if previous and re.search(r'cheaper|moins cher|ارخص', text) and shown:
        return None, cheaper_than(previous, shown), None
    if previous and re.search(r'under|budget|moins de|تحت|اقل من', text) and not extract(message, {})[0].get('category') and not new_title_terms(message, extract(message, {})[0]):
        criteria, issue = extract(message, previous, language)
    else:
        criteria, issue = await interpret_turn(last_clause, previous, language)
    # Common retailer translations keep discovery usable during a provider outage.
    if re.search(r'coffee (?:machine|maker)|cafetiere|machine a cafe|الة قهوة', plain(last_clause)):
        criteria, issue = extract(last_clause, {}, language)
        criteria['type_terms'] = ['machine à café', 'cafetière', 'machine espresso']
    if criteria.get('title_terms') in (['mouse'], ['mice'], ['mouses'], ['souris']):
        criteria.pop('title_terms')
        criteria['type_terms'] = ['souris']
    if criteria.get('category') == 'laptop' and re.search(r'gaming|gamer', text):
        criteria['search_terms'] = ['gaming']
    return None, criteria, issue


def cheaper_than(criteria, shown):
    result = dict(criteria)
    if shown:
        ceiling = Decimal(shown[0]['price_tnd']) - Decimal('0.001')
        result.pop('exclusive_budget', None)
        result['budget'] = str(min(ceiling, Decimal(str(result.get('budget') or ceiling))))
    return result


def fast_followup(message, previous, shown, language):
    text = plain(message).strip(' !?.؟')
    if price_comparison(text):
        return agent.CompareShown(metric='price'), previous, None
    if settings.shopping_local_planner and (positions := displayed_positions(text)):
        return agent.CompareShown(metric='features', positions=positions), previous, None
    if not any(previous.get(key) for key in ('category', 'type_terms', 'title_terms', 'brand', 'subjects')):
        return None
    if settings.shopping_local_planner and (refinement := feature_refinement(text)):
        criteria, issue = extract(message, previous, language)
        if refinement[1] in ('avec', 'with', 'only', 'فقط'):
            if criteria.get('preference') and criteria.get('forbidden_terms'):
                criteria['forbidden_terms'] = [
                    term for term in criteria['forbidden_terms'] if not term_matches(term, criteria['preference'])
                ]
                if not criteria['forbidden_terms']:
                    criteria.pop('forbidden_terms')
            if criteria.get('dedicated_gpu'):
                criteria.pop('forbid_dedicated_gpu', None)
        return None, explicit_features(message, criteria), issue
    if re.fullmatch(r'\d+[.,]?\d*\s*(?:tnd|dt|dinars?|دينار)', text):
        criteria, issue = extract(message, previous, language)
        criteria.pop('exclusive_budget', None)
        return None, criteria, issue
    if shown and re.fullmatch(r'(?:(?:show me )?something cheaper|cheaper(?: ones)?|moins cher|ارخص)', text):
        return None, cheaper_than(previous, shown), None
    if re.fullmatch(r'(?:show me (?:some|ones|those) |only )?(?:under|below|less than|moins de|sous|تحت|اقل من)\s*[\d.,٬٫ ]+\s*(?:tnd|dt|dinars?|دينار(?: تونسي)?)?', text):
        criteria, issue = extract(message, previous, language)
        if criteria.get('budget') or issue:
            return None, explicit_features(message, criteria), issue
    return None


async def plan_turn(message, language, previous, history, shown):
    if followup := fast_followup(message, previous, shown, language):
        return followup
    if subjects := named_pair(message, BRANDS):
        return None, {'subjects': subjects}, None
    public_key = None
    if settings.shopping_local_planner and not (previous or history or shown) and len(message) <= 120:
        public_key = (plain(message), language)
        if cached := get_public_plan(public_key):
            return None, cached, None
    if simple := simple_search(message, previous, language, extract):
        if public_key is not None and not simple[2]:
            remember_public_plan(public_key, simple[1])
        return simple
    if settings.shopping_local_planner:
        clause = re.split(r'\b(?:forget|oublie)\b[^.!?;]*[.!?;]\s*', message, flags=re.I)[-1] or message
        context = previous if clause == message else {}
        if known := known_search(clause, context, language, extract, CATEGORIES):
            if known[2]:
                return None, previous, known[2]
            if public_key is not None and clause == message:
                remember_public_plan(public_key, known[1])
            return known
        _, issue = extract(clause, {}, language)
        if issue:
            return None, previous, issue
    if llm.configured():
        try:
            action = await agent.choose_tool(message, language, previous, history, shown)
            if isinstance(action, agent.SearchProducts):
                explicit, issue = extract(message, {}, language)
                clause = re.split(r'\b(?:forget|oublie)\b[^.!?;]*[.!?;]\s*', message, flags=re.I)[-1] or message
                named, _ = extract(clause, {}, language)
                if named.get('category') and action.category and named['category'] != action.category:
                    # Ground an explicit new subject even if a model echoes the last search.
                    return await fallback_plan(clause, {}, [], language)
                previous_types = retailer_terms(previous)
                repeated_type = bool(action.category and action.category == previous.get('category')) or any(
                    type_matches(previous_types, phrase) for phrase in action.type_terms)
                new_subject = new_title_terms(clause, named)
                explicit_switch = re.search(r'\b(?:now i need|instead show me|new search|maintenant je cherche)\b', plain(clause))
                if repeated_type and new_subject and explicit_switch and not type_matches(action.type_terms, clause):
                    return await fallback_plan(clause, {}, [], language)
                same_topic = action.mode == 'refine' and (repeated_type or not previous_types)
                criteria = dict(previous) if same_topic else {}
                fields = action.model_dump(mode='json', exclude_none=True, exclude_defaults=True,
                                           exclude={'mode','fresh','cheaper','clear_filters','compare_retailers'})
                if not same_topic and 'budget' not in explicit:
                    if previous.get('budget') is not None and str(previous['budget']) == str(fields.get('budget')):
                        fields.pop('budget', None)
                category = named.get('category') or action.category
                if category and category in CATEGORIES:
                    fields['category'] = category
                    fields['type_terms'] = list(dict.fromkeys([*fields.get('type_terms', ()), *retailer_terms({'category': category})]))
                subject = product_phrase(clause)
                if not named.get('category') and re.search(r'\b(?:mouse|mice|mouses|souris)\b', subject) and not re.search(
                    r'\b(?:keyboard|clavier|pad|tapis|packs?|bundles?|combos?|kits?)\b', subject
                ):
                    fields.pop('category', None)
                    criteria.pop('category', None)
                    fields['type_terms'] = ['souris']
                if named.get('dedicated_gpu'):
                    fields['dedicated_gpu'] = True
                for constraint in ('forbid_dedicated_gpu', 'forbidden_terms'):
                    if named.get(constraint):
                        fields[constraint] = named[constraint]
                if named.get('gpu_priority'):
                    fields['gpu_priority'] = True
                if category == 'headset' and named.get('preference') == 'rgb' and not named.get('soft_preference'):
                    fields['search_terms'] = list(dict.fromkeys([
                        'rgb', *(term for term in fields.get('search_terms', []) if not term_matches(term, 'rgb'))]))
                criteria.update(fields)
                for key in action.clear_filters:
                    criteria.pop(key, None)
                for constraint in ('brand', 'airpods_edition', 'airpods_generation', 'preference'):
                    if constraint in named:
                        criteria[constraint] = named[constraint]
                if named.get('preference'):
                    criteria.pop('soft_preference', None)
                    if named.get('soft_preference'):
                        criteria['soft_preference'] = True
                        if 'search_terms' in criteria:
                            criteria['search_terms'] = [term for term in criteria['search_terms']
                                                        if not term_matches(term, plain(named['preference']))]
                if criteria.get('forbid_dedicated_gpu'):
                    for constraint in ('dedicated_gpu', 'gpu_model', 'gpu_priority'):
                        criteria.pop(constraint, None)
                if criteria.get('forbidden_terms'):
                    excluded = criteria['forbidden_terms']
                    for constraint in ('search_terms', 'preferred_terms'):
                        if constraint in criteria:
                            criteria[constraint] = [term for term in criteria[constraint]
                                                    if not any(term_matches(x, plain(term)) for x in excluded)]
                    if any(term_matches(x, plain(criteria.get('preference', ''))) for x in excluded):
                        criteria.pop('preference', None)
                if explicit.get('budget'):
                    criteria['budget'] = explicit['budget']
                if action.cheaper and action.mode == 'refine':
                    criteria = cheaper_than(criteria, shown)
                return action, explicit_features(clause, criteria), issue
            return action, previous, None
        except Exception as exc:
            logging.getLogger(__name__).warning('Shopping agent unavailable: %s', type(exc).__name__)
    action, criteria, issue = await fallback_plan(message, previous, shown, language)
    return action, explicit_features(message, criteria), issue


async def _discover_for_turn(lookup, candidates, fresh, criteria, progress):
    if progress is None:
        return await discover_products(lookup, candidates, force=fresh)
    changed = asyncio.Event()
    verified = set()

    def committed(url):
        verified.add(url)
        changed.set()

    task = asyncio.create_task(discover_products(lookup, candidates, force=fresh, on_verified=committed))
    task.add_done_callback(lambda _: changed.set())
    first_preview = True
    delivered = set()
    try:
        while not task.done():
            await changed.wait()
            changed.clear()
            if verified == delivered:
                continue
            if not first_preview:
                await asyncio.sleep(0.05)
                if task.done():
                    break
            delivered = verified.copy()
            cards = await transactions.read(search, criteria)
            mark_verified(cards, delivered)
            if cards:
                progress(cards)
                first_preview = False
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@traceable(name='find_shopping_listings', run_type='tool')
async def find_listings(criteria, fresh, language, compare_stores=False, *, progress=None):
    key = json.dumps(criteria, sort_keys=True, default=str, separators=(',', ':'))
    cached_cards = await transactions.queued_public_read(key, search, criteria)
    # Each turn sets its own verification flags.
    products = [dict(card, offers=[dict(offer) for offer in card['offers']])
                if 'offers' in card else dict(card) for card in cached_cards]
    metrics.retrieved(products)
    recent_cutoff = now() - timedelta(hours=settings.shopping_fresh_hours)
    recent_stores = [
        {offer['supplier'] for offer in card.get('offers', [card])
         if datetime.fromisoformat(offer['scraped_at'].rstrip('Z')) >= recent_cutoff}
        for card in products
    ]
    reports = []
    needs_more_stores = compare_stores and (not products or any(len(stores) < 2 for stores in recent_stores))
    if settings.shopping_live_discovery and (fresh or not any(recent_stores) or needs_more_stores):
        lookup = dict(criteria)
        types = retailer_terms(criteria)
        if types:
            lookup['type_terms'] = types
        if lookup.get('type_terms') or lookup.get('title_terms') or lookup.get('brand'):
            if needs_more_stores and recent_stores and not fresh:
                lookup['recent_retailers'] = sorted(set.intersection(*recent_stores))
            if progress:
                progress(products)
            scrape_start = time.perf_counter()
            candidates = await transactions.read(search, criteria, recheck=True)
            reports = await _discover_for_turn(lookup, candidates, fresh, criteria, progress)
            metrics.scraped(scrape_start, reports)
            products = await transactions.read(search, criteria)
    verified = {url for report in reports for url in report['urls']}
    mark_verified(products, verified)
    if products:
        best = products[0]
        if criteria.get('gpu_priority') and best['graphics']:
            answer = say(language, 'strongest', price=best['price_tnd'], supplier=best['supplier'], name=best['name'], gpu=best['graphics'])
        elif criteria.get('sort') == 'recommend':
            lead = localized(language, 'My first pick among the matching listings', 'Mon premier choix parmi les offres correspondantes', 'خياري الأول بين العروض المطابقة')
            answer = f"{lead} · {best['name']} — {best['price_tnd']} TND ({best['supplier']})."
            details = plain(' '.join([best['name'], best['description'], str(best['specifications'])]))
            requested = [*criteria.get('preferred_terms', []), *criteria.get('search_terms', [])]
            if criteria.get('preference'):
                requested.append(criteria['preference'])
            features = list(dict.fromkeys([*(t for t in requested if term_matches(t, details)), *recommendation_evidence(best)]))
            if features:
                match = localized(language, 'Listed features', 'Caractéristiques indiquées', 'المواصفات المذكورة')
                answer += f" {match} · {', '.join(features)}."

        else:
            lowest = min(products, key=lambda p: Decimal(p['price_tnd']))
            answer = say(language, 'found', count=sum(len(p.get('offers', [p])) for p in products), price=lowest['price_tnd'], supplier=lowest['supplier'], name=lowest['name'])
    elif reports:
        answer = localized(language, 'I couldn’t verify an in-stock listing matching those requirements.',
                           'Je n’ai trouvé aucune offre en stock vérifiable correspondant à vos critères.',
                           'لم أعثر على عرض متوفر يمكن التحقق منه بهذه الشروط.')
    else:
        answer = await transactions.read(explain_empty, criteria, language)
    displayed_urls = {o['product_url'] for p in products for o in p.get('offers', [p])}
    if verified.intersection(displayed_urls):
        stores = ', '.join(r['retailer'] for r in reports if displayed_urls.intersection(r['urls']))
        answer += ' ' + localized(language, f'Checked retailer pages this turn at {stores}.', f'Fiches vérifiées maintenant chez {stores}.', f'تحققت الآن من صفحات {stores}.')
    elif reports and all(r['status'] == 'recent_attempt' for r in reports):
        answer += ' ' + localized(language, 'A lookup for this search is recent or in progress.', 'Une recherche identique est récente ou en cours.', 'يوجد بحث مماثل حديث أو قيد التنفيذ.')
    elif reports and not products:
        checked = ', '.join(r['retailer'] for r in reports if r['status'] in ('checked','no_match'))
        if checked:
            answer += ' ' + localized(language, f'Searched {checked}.', f'Recherche effectuée chez {checked}.', f'بحثت في {checked}.')
    unavailable = [r['retailer'] for r in reports if r['status'] not in ('checked','no_match','recent_attempt')]
    if unavailable:
        answer += say(language, 'unavailable', retailers=', '.join(unavailable))
    return answer, products


@traceable(name='shopping_conversation', run_type='chain')
async def reply(sid, message, cid=None, language='en', *, progress=None, turn_id=None):
    token = metrics.begin()
    products = []
    success = False
    def emit(cards):
        metrics.first(cards)
        progress(cards)
    try:
        key = cid or turn_id
        if key is None:
            result = await _reply(sid, message, cid, language, turn_id, progress=emit if progress else None)
        else:
            async with transactions.turn_lock(sid, key):
                result = await _reply(sid, message, cid, language, turn_id, progress=emit if progress else None)
        products = result['products']
        metrics.first(products)
        success = True
    finally:
        observation = metrics.finish(token, products, success)
    return dict(result, metrics=observation)


async def _reply(sid, message, cid, language, turn_id, *, progress=None):
    fingerprint = hashlib.sha256(json.dumps([cid, language, message]).encode()).hexdigest() if turn_id else None
    replay, version, previous, history, shown = (
        await transactions.read(_turn_input, sid, cid, turn_id, fingerprint)
        if cid or turn_id else (None, None, {}, [], [])
    )
    if replay is not None:
        return replay
    cid = cid or uuid.uuid4().hex
    criteria = previous
    products = []
    replace_cards = False
    command = is_cart_command(message)
    answer = None
    if not command:
        action, criteria, issue = await plan_turn(message, language, previous, history, shown)
        if isinstance(action, agent.GeneralAnswer):
            answer = action.answer
        elif isinstance(action, agent.CompareShown):
            answer, products = agent.comparison(mark_verified(shown, set()), action, language)
            replace_cards = bool(products)
        elif issue:
            answer = issue
        elif not criteria.get('budget') and re.search(r'\b(?:my budget|mon budget)\b|ميزانيتي', plain(message)):
            answer = localized(language, 'What is your maximum budget in TND?', 'Quel est votre budget maximal en TND ?', 'ما الحد الأقصى لميزانيتك بالدينار؟')
        elif criteria.get('subjects'):
            pair = criteria['subjects']
            snapshots = [[] for _ in pair]
            async def lookup(position, subject):
                def publish(cards):
                    snapshots[position] = cards[:1]
                    progress([card for subset in snapshots for card in subset])
                filters = {k: v for k, v in criteria.items() if k != 'subjects'}
                result = await find_listings({**filters, **subject}, False, language,
                                             progress=publish if progress else None)
                if progress:
                    publish(result[1])
                return result
            results = await asyncio.gather(*(lookup(i, subject) for i, subject in enumerate(pair)))
            lines = []
            for subject, (summary, found) in zip(pair, results):
                if found:
                    products.append(found[0])
                else:
                    lines.append(' '.join([subject['brand'], *subject['title_terms']]) + ' · ' + summary)
            if products:
                comparison, _ = agent.comparison(products, agent.CompareShown(metric='features'), language)
                lines.insert(0, comparison)
            answer = '\n'.join(lines)
            replace_cards = True
        elif not any(criteria.get(k) for k in ('category','type_terms','title_terms','brand')):
            answer = say(language, 'pc_kind' if re.search(r'\b(?:pc|computer|ordinateur)\b', plain(message)) else 'category')
            replace_cards = True
        else:
            fresh = action.fresh if action else bool(re.search(r'\b(live|recheck|refresh|actualise)\b|تحقق الان', plain(message)))
            compare_stores = action.compare_retailers if action else bool(re.search(r'compare prices|comparer les prix|قارن الاسعار', plain(message)))
            answer, products = await find_listings(criteria, fresh, language, compare_stores, progress=progress)
            replace_cards = True
    return await transactions.write(
        _commit_turn, sid, cid, message, language, turn_id, fingerprint, version,
        criteria, answer, products, replace_cards, command, [p['id'] for p in shown],
    )


def _commit_turn(sid, cid, message, language, turn_id, fingerprint, version,
                 criteria, answer, products, replace_cards, command, last_ids):
    with transactions.transaction() as db:
        replay = _saved_turn(db, sid, turn_id, fingerprint)
        if replay is not None:
            return replay
        if version is None:
            c = ShoppingConversation(id=cid, user_session_id=sid, title=message[:70], criteria={}, last_offer_ids=[])
            db.add(c)
            db.flush()
        else:
            c = db.get(ShoppingConversation, cid)
            if c is None or c.user_session_id != sid:
                raise ValueError('Conversation not found')
            latest = db.scalar(select(func.max(ShoppingMessage.id)).where(ShoppingMessage.conversation_id == cid))
            if (latest or 0) != version:
                raise transactions.ShoppingConflict('Conversation changed during this turn. Retry to use the latest results.')
        if command:
            answer = _cart_command(message, sid, last_ids, language, db=db) or say(language, 'category')
        c.criteria = criteria
        if replace_cards:
            c.last_offer_ids = [p['id'] for p in products]
        c.updated_at = now()
        assistant = ShoppingMessage(conversation_id=cid, role='assistant', content=answer, products=products)
        db.add_all([ShoppingMessage(conversation_id=cid, role='user', content=message, products=[]), assistant])
        metadata = dict(conversation_id=cid, conversation_title=c.title,
                        updated_at=c.updated_at.isoformat(), cart=cart(sid, db=db))
        if turn_id:
            db.add(ShoppingTurn(owner_id=sid, turn_id=turn_id, request_hash=fingerprint,
                                conversation_id=cid, assistant_message_id=assistant.id, result=metadata))
        return dict(metadata, reply=answer, products=products)
