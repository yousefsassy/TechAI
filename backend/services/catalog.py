from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit
from sqlalchemy import or_, select
from sqlalchemy.orm import contains_eager, joinedload
from backend.database import Session
from backend.models import Offer, Product, now
from backend.config import settings
from backend.schemas import Requirements
import re

HOSTS = {'www.tunisianet.com.tn','www.mytek.tn','spacenet.tn','www.scoop.com.tn'}
IMAGE_PATH = re.compile(r'\.(?:jpe?g|png|webp|svg|gif|avif)$', re.I)

GPU_PATTERN = re.compile(
    r'\b(?:geforce\s*)?(?:rtx|gtx)\s*\d{3,4}(?:\s*(?:ti|super))?\b'
    r'|\b(?:radeon\s*rx\s*\d{3,4}|rx\s*\d{4})(?:\s*(?:xtx|xt|gre))?\b'
    r'|\bintel\s*arc\s*[ab]\s*\d{3}\b',
    re.I,
)
NVIDIA_MODEL = re.compile(r'\b(?:rtx|gtx)\s*(\d{3,4})(?:\s*(ti|super))?\b', re.I)

def listed_graphics(product):
    """Return only explicitly listed discrete GPU models, never inferred hardware."""
    return graphics_from_details(product.name, product.raw_specifications)

def graphics_from_details(name, raw):
    if not isinstance(raw, dict):
        raw = {}
    attrs = raw.get('attributes') or {}
    parts = [name or '', str(raw.get('description') or '')]
    if isinstance(attrs, dict):
        parts.extend(str(value) for value in attrs.values())
    found = GPU_PATTERN.search(' '.join(parts))
    return found.group(0) if found else None

GPU_TIERS = {'5090':120,'4090':115,'5080':110,'4080':105,'5070 ti':100,'5070':95,
             '4070':90,'4070 super':94,'4060 ti':88,'5060':85,'4060':80,'5050':75,'4050':70,'3080':65,'3070':60,
             '3060':55,'3050 ti':53,'3050':50,'2050':35,'1660 ti':42,'1650 ti':28,'1650':25}

def graphics_tier(graphics):
    """Rough model-tier ordering, not a measured GPU benchmark or laptop TGP."""
    if not graphics:
        return -1
    model = NVIDIA_MODEL.search(graphics)
    if not model:
        return -1
    number, suffix = model.groups()
    return GPU_TIERS.get(f'{number} {suffix.lower()}' if suffix else number, -1)

def _allowed_url_parts(url):
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != 'https' or parts.hostname not in HOSTS or parts.username
            or parts.password or port not in (None, 443)):
        return None
    return parts


def safe_url(url: str | None, demo=False):
    return url if _allowed_url_parts(url) else None


def product_page(url):
    parts = _allowed_url_parts(url)
    return url if parts and not IMAGE_PATH.search(parts.path) else None


def serialize(o):
    p = o.product
    raw = p.raw_specifications
    if not isinstance(raw, dict): raw = {}
    metadata = raw.get('metadata')
    rating = metadata.get('aggregateRating') if isinstance(metadata, dict) else None
    if not isinstance(rating, dict): rating = {}
    attributes = raw.get('attributes')
    return dict(id=o.id, name=p.name, category=p.category, brand=p.brand, model=p.model,
        processor=p.processor, ram_gb=p.ram_gb, storage_gb=p.storage_gb, storage_type=p.storage_type,
        screen_size=p.screen_size, operating_system=p.operating_system,
        supplier=o.supplier_name, price_tnd=str(o.price_tnd), stock_status=o.stock_status,
        available_quantity=o.available_quantity, product_url=product_page(o.product_url),
        image_url=safe_url(o.image_url), scraped_at=o.scraped_at.isoformat(), is_demo=o.is_demo,
        description=str(raw.get('description') or '')[:500],
        specifications=attributes if isinstance(attributes, dict) else {},
        graphics=listed_graphics(p),
        rating=rating.get('ratingValue'), rating_count=rating.get('reviewCount'))

def matches(o, r: Requirements, *, oldest=None):
    p = o.product
    if not r.category or not r.quantity or not r.budget: raise ValueError('missing_request_parameters')
    if p.category != r.category or o.stock_status != 'in_stock': return False
    if oldest is None:
        oldest = now() - timedelta(hours=settings.offer_max_age_hours)
    if o.scraped_at < oldest: return False
    if o.available_quantity is not None and o.available_quantity < r.quantity: return False
    if not product_page(o.product_url): return False
    if r.brands and (p.brand or '').lower() not in [b.lower() for b in r.brands]: return False
    if r.min_ram and (p.ram_gb is None or p.ram_gb < r.min_ram): return False
    if r.min_storage and (p.storage_gb is None or p.storage_gb < r.min_storage): return False
    if r.storage_type and p.storage_type != r.storage_type: return False
    if r.processor and r.processor.lower() not in (p.processor or '').lower(): return False
    return o.price_tnd > 0 and o.price_tnd * r.quantity <= r.budget

def list_catalog(*, limit=None, after_id=None):
    """Keep the full legacy listing available and allow ID-keyset pagination."""
    query = select(Offer).options(joinedload(Offer.product)).order_by(Offer.id)
    if after_id is not None:
        query = query.where(Offer.id > after_id)
    if limit is not None:
        query = query.limit(limit)
    with Session() as db:
        return [serialize(offer) for offer in db.scalars(query)]


def search_products(data):
    requirements = data.requirements
    if not requirements.category or not requirements.quantity or not requirements.budget:
        with Session() as db:
            if db.scalar(select(Offer.id).limit(1)) is None:
                return []
        raise ValueError('missing_request_parameters')

    oldest = now() - timedelta(hours=settings.offer_max_age_hours)
    ceiling = requirements.budget / requirements.quantity
    if data.max_unit_price is not None:
        ceiling = min(ceiling, data.max_unit_price)
    # SQLite compares its stored numeric value. Include sub-millime legacy
    # values that SQLAlchemy could round into budget before the Decimal check.
    ceiling += Decimal('0.001')
    query = (select(Offer).join(Offer.product).options(contains_eager(Offer.product))
             .where(Product.category == requirements.category, Offer.stock_status == 'in_stock',
                    Offer.scraped_at >= oldest, Offer.price_tnd > 0, Offer.price_tnd <= ceiling,
                    or_(Offer.available_quantity.is_(None),
                        Offer.available_quantity >= requirements.quantity))
             .order_by(Offer.id))
    if requirements.min_ram:
        query = query.where(Product.ram_gb >= requirements.min_ram)
    if requirements.min_storage:
        query = query.where(Product.storage_gb >= requirements.min_storage)
    if requirements.storage_type:
        query = query.where(Product.storage_type == requirements.storage_type)

    matches_found = []
    with Session() as db:
        # Stream in small batches and apply the original URL, Unicode and
        # exact Decimal rules before enforcing the public 100-offer cap.
        with db.execute(query.execution_options(yield_per=128)) as rows:
            for offer in rows.scalars():
                if not matches(offer, requirements, oldest=oldest):
                    continue
                if data.max_unit_price is not None and offer.price_tnd > data.max_unit_price:
                    continue
                matches_found.append(serialize(offer))
                if len(matches_found) == 100:
                    break
    return matches_found


def compare_offers(data):
    with Session() as db:
        query = select(Offer).options(joinedload(Offer.product)).where(Offer.id.in_(data.offer_ids))
        offers = [offer for offer in db.scalars(query) if matches(offer, data.requirements)]
        from backend.services.ranking import rank
        return rank([serialize(o) for o in offers],data.requirements)


def get_offer_details(data):
    with Session() as db:
        o=db.get(Offer,data.offer_id)
        if o is None: raise ValueError('offer_not_found')
        return serialize(o)

def check_budget(data):
    with Session() as db:
        o=db.get(Offer,data.offer_id)
        if o is None: raise ValueError('offer_not_found')
        total=o.price_tnd*data.quantity
        return dict(total=str(total), budget=str(data.budget), remaining=str(data.budget-total), valid=0 < total <= data.budget)
