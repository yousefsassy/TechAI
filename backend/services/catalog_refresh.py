"""Best-effort scheduled catalog collection, independent of shopping requests."""
import asyncio
import logging
from datetime import timedelta

from langsmith import traceable
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert

from backend.config import settings
from backend.database import Session
from backend.models import CatalogRefresh, now

log = logging.getLogger(__name__)
# A new collection plan gets its first run when the updated backend starts.
JOB_ID = 'shopping_catalog_audio_v3'
BRAND_PAGES = (('laptop', 'lenovo'), ('tablet', 'apple'), ('tablet', 'samsung'))

def claim():
    """One SQLite-backed lease prevents duplicate runs across API workers."""
    current = now()
    with Session.begin() as db:
        db.execute(insert(CatalogRefresh).values(id=JOB_ID, last_completed_at=None,
            lease_until=None, last_error=None).on_conflict_do_nothing(index_elements=['id']))
        result = db.execute(update(CatalogRefresh).where(
            CatalogRefresh.id == JOB_ID,
            or_(CatalogRefresh.lease_until.is_(None), CatalogRefresh.lease_until < current),
            or_(CatalogRefresh.last_completed_at.is_(None),
                CatalogRefresh.last_completed_at <= current - timedelta(hours=settings.catalog_refresh_hours))
        ).values(lease_until=current + timedelta(hours=4), last_error=None))
        return result.rowcount == 1

def finish(error=None):
    values = dict(lease_until=now() + timedelta(minutes=30) if error else None,
                  last_error=str(error)[:200] if error else None)
    if not error: values['last_completed_at'] = now()
    with Session.begin() as db:
        db.execute(update(CatalogRefresh).where(CatalogRefresh.id == JOB_ID).values(**values))

def status():
    with Session() as db:
        row = db.get(CatalogRefresh, JOB_ID)
        return dict(last_completed_at=row.last_completed_at.isoformat() + 'Z' if row and row.last_completed_at else None,
                    running=bool(row and row.lease_until and row.lease_until > now() and not row.last_error),
                    last_error=row.last_error if row else None,
                    interval_hours=settings.catalog_refresh_hours)

@traceable(name='scheduled_catalog_refresh', run_type='chain')
def run_cycle():
    if not claim(): return False
    from backend.services.shopping import CATEGORIES, refresh_sources, refresh_brand, refresh_gaming
    try:
        outcomes = list(refresh_brand('headset', 'logitech'))
        for category in (*CATEGORIES, 'accessories'):
            outcomes.extend(refresh_sources(category))
        for category, brand in BRAND_PAGES:
            outcomes.extend(refresh_brand(category, brand))
        outcomes.extend(refresh_gaming())
        if not outcomes or all(item['status'] == 'unavailable' for item in outcomes):
            raise RuntimeError('All retailer refreshes unavailable')
        finish()
        return True
    except Exception as exc:
        log.warning('Scheduled catalog refresh failed: %s', exc)
        finish(error=exc)
        return False

async def loop():
    """Catch up at startup, then check the persisted 24-hour schedule each minute."""
    while True:
        await asyncio.to_thread(run_cycle)
        await asyncio.sleep(60)
