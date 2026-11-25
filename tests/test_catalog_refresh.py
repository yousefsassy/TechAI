from datetime import timedelta

from backend.database import Session
from backend.models import CatalogRefresh, now
from backend.services import catalog_refresh, shopping

def test_24_hour_lease_and_retry(monkeypatch):
    counts=[]
    monkeypatch.setattr(shopping,'refresh_sources',lambda category: counts.append(category) or [dict(status='refreshed')])
    monkeypatch.setattr(shopping,'refresh_brand',lambda *args: [dict(status='refreshed')])
    monkeypatch.setattr(shopping,'refresh_gaming',lambda: [dict(status='refreshed')])
    assert catalog_refresh.run_cycle() is True
    assert counts == [*shopping.CATEGORIES, 'accessories']
    assert catalog_refresh.status()['last_completed_at']
    assert catalog_refresh.run_cycle() is False
    with Session.begin() as db:
        row=db.get(CatalogRefresh,catalog_refresh.JOB_ID)
        row.last_completed_at=now()-timedelta(hours=25)
    assert catalog_refresh.run_cycle() is True
    assert len(counts) == 2*(len(shopping.CATEGORIES) + 1)

def test_refresh_failure_preserves_success_and_retries_later(monkeypatch):
    monkeypatch.setattr(shopping,'refresh_sources',lambda _: [dict(status='unavailable')])
    monkeypatch.setattr(shopping,'refresh_brand',lambda *args: [dict(status='unavailable')])
    monkeypatch.setattr(shopping,'refresh_gaming',lambda: [dict(status='unavailable')])
    assert catalog_refresh.run_cycle() is False
    state=catalog_refresh.status()
    assert state['last_completed_at'] is None and state['last_error']
    assert catalog_refresh.run_cycle() is False
