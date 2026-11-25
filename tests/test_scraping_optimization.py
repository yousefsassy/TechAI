import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event
from types import SimpleNamespace

import pytest
from scrapling import Selector
from scrapling.engines.toolbelt.custom import Response
from sqlalchemy import select

from backend.config import settings
from backend.database import Session
from backend.models import Offer, RetailerClock, now
from scraping import discovery, public_pages
from scraping.normalize import money, stock
from scraping.parser import parse_product
from scraping.refresh_catalog import demote, refresh, save


ROOT = 'https://www.scoop.com.tn'
HREF = ROOT + '/2300-souris-optique.html'


def pdp(name='Souris Optique Nova', price='159.900', extra=''):
    data = {'@type': 'Product', 'name': name, 'sku': 'current',
            'offers': {'priceCurrency': 'TND', 'price': price, 'availability': 'https://schema.org/InStock'}}
    return f'<script type="application/ld+json">{json.dumps(data)}</script>{extra}'


def response(url, body='', status=200, headers=None):
    return Response(url, body, status, '', {}, headers or {}, {}, _log=False)


@pytest.mark.parametrize('value', ['-59.900', '−59.900', '59.9999', '10 / 20', 'US 19.000', '25 00.0', '59.'])
def test_ingestion_rejects_ambiguous_or_non_tnd_prices(value):
    with pytest.raises(ValueError):
        money(value)


def test_ingestion_accepts_local_grouping_at_millime_precision():
    assert money('٢٬٤٩٩٫٠٠٠ TND') == Decimal('2499.000')
    assert money('2 499,125 DT') == Decimal('2499.125')


@pytest.mark.parametrize('value, status', [
    ('Non disponible en ligne', 'out_of_stock'),
    ('Disponible sur commande', 'unknown'),
    ('Disponible en magasin', 'unknown'),
    ('LimitedAvailability', 'in_stock'),
    ('https://schema.org/SoldOut', 'out_of_stock'),
])
def test_stock_qualifiers_precede_optimistic_keywords(value, status):
    assert stock(value) == status


def test_conflicting_dom_stock_and_structured_stock_are_unknown():
    html = pdp(extra='<p id="product-availability">Disponible sur commande</p>')
    parsed = parse_product(Selector(html, url=HREF), HREF, 'discovered', 'scoop')
    assert parsed.stock_status == 'unknown'
    assert parsed.available_quantity is None


def test_observation_fence_preserves_offer_identity_and_demotion_timestamp():
    initial = now() - timedelta(minutes=6)
    later = initial + timedelta(minutes=2)
    current = parse_product(Selector(pdp(), url=HREF), HREF, 'discovered', 'scoop', observed_at=initial)
    assert save(current) == 'inserted'
    with Session() as db:
        offer_id = db.scalar(select(Offer.id).where(Offer.product_url == HREF))
    demote(HREF, later)
    assert save(current.model_copy(update={'observed_at': initial + timedelta(minutes=1)})) == 'superseded'
    with Session() as db:
        offer = db.get(Offer, offer_id)
        assert offer.scraped_at == initial
        assert offer.stock_status == 'unknown'
    moved = ROOT + '/2500-souris-optique.html'
    assert save(current.model_copy(update={'url': moved, 'observed_at': later + timedelta(minutes=1)})) == 'updated'
    with Session() as db:
        offer = db.get(Offer, offer_id)
        assert offer.product_url == moved
        assert offer.stock_status == 'in_stock'


def test_page_two_gets_pdp_budget_with_strict_price_and_route_policy(monkeypatch):
    calls = []
    seed = ROOT + '/36-souris'
    next_page = seed + '?page=2'
    boundary = ROOT + '/2000-souris-optique.html'
    forbidden = ROOT + '/private-souris.html'
    next_html = f'<a class="next" href="{next_page}">Suivante</a>'

    def listing(url, price):
        return (f'<article class="product-miniature"><h2><a href="{url}">Souris Optique</a></h2>'
                f'<span class="price">{price} TND</span></article>')

    def get(url, *args, **kwargs):
        calls.append(url)
        if url.endswith('robots.txt'):
            return response(url, 'User-agent: *\nDisallow: /private')
        if url == seed:
            return response(url, next_html + listing(forbidden, '1.000') + ''.join(
                listing(ROOT + f'/{number}-souris-optique.html', '450.000') for number in range(7)))
        if url == next_page:
            return response(url, listing(boundary, '100.000') + listing(HREF, '99.000'))
        if url == HREF:
            return response(url, pdp(price='99.000'))
        if url == boundary:
            return response(url, pdp(price='100.000'))
        return response(url, '', 404)

    monkeypatch.setattr(discovery, 'get_page', get)
    monkeypatch.setattr(discovery, '_RULES', {})
    monkeypatch.setattr(discovery, '_MENU_ROUTES', {})
    monkeypatch.setattr(settings, 'scraping_validated_routes', True)
    verified = []
    criteria = {'type_terms': ['mouse'], 'budget': '100', 'exclusive_budget': True}
    report = discovery.source_search('scoop', criteria, [], limit=1, on_verified=verified.append)
    assert report['saved'] == 1
    assert report['urls'] == verified == [HREF]
    assert calls == [ROOT + '/robots.txt', seed, next_page, HREF]
    assert forbidden not in calls


def test_blocked_seed_falls_back_to_permitted_navigation(monkeypatch):
    calls = []
    seed = ROOT + '/36-souris'
    listing_url = ROOT + '/1136-souris'

    def get(url, *args, **kwargs):
        calls.append(url)
        if url.endswith('robots.txt'):
            return response(url, 'User-agent: *\nDisallow: /36-souris')
        if url == ROOT + '/':
            return response(url, f'<a href="{listing_url}">Souris</a>')
        if url == listing_url:
            return response(url, f'<article class="product-miniature"><h2><a href="{HREF}">Souris</a></h2></article>')
        assert url == HREF
        return response(url, pdp())

    monkeypatch.setattr(discovery, 'get_page', get)
    monkeypatch.setattr(discovery, '_RULES', {})
    monkeypatch.setattr(discovery, '_MENU_ROUTES', {})
    report = discovery.source_search('scoop', {'type_terms': ['mouse']}, [], limit=1)
    assert report['saved'] == 1
    assert seed not in calls
    assert listing_url in calls


def test_robots_and_same_active_pdp_shared_for_two_budgets(monkeypatch):
    calls = []
    first_at_pdp = Event()
    second_joined = Event()

    def get(url, *args, **kwargs):
        calls.append(url)
        if url.endswith('robots.txt'):
            return response(url, 'User-agent: *\nAllow: /')
        if url == HREF:
            first_at_pdp.set()
            assert second_joined.wait(2)
            return response(url, pdp(price='159.900'))
        return response(url, '', 404)

    original = discovery.get_page_shared

    def shared(key, fetch, deadline, cancel):
        if key[0] != HREF:
            return original(key, fetch, deadline, cancel)
        # Signal after the second waiter has reached the registry.
        if first_at_pdp.is_set():
            with public_pages._PAGES_LOCK:
                future = public_pages._ACTIVE_PUBLIC_PAGES[key].future
                result = future.result

                def joined(*args, **kwargs):
                    second_joined.set()
                    return result(*args, **kwargs)

                monkeypatch.setattr(future, 'result', joined)
        return original(key, fetch, deadline, cancel)

    monkeypatch.setattr(discovery, 'get_page', get)
    monkeypatch.setattr(discovery, 'get_page_shared', shared)
    monkeypatch.setattr(discovery, '_RULES', {})
    monkeypatch.setattr(discovery, '_MENU_ROUTES', {})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(discovery.source_search, 'scoop',
                            {'type_terms': ['mouse'], 'budget': '200', '_verify_only': True}, [HREF], 1)
        assert first_at_pdp.wait(2)
        second = pool.submit(discovery.source_search, 'scoop',
                             {'type_terms': ['mouse'], 'budget': '150', '_verify_only': True}, [HREF], 1)
        assert first.result(timeout=3)['urls'] == [HREF]
        assert second.result(timeout=3)['urls'] == []
    assert calls.count(ROOT + '/robots.txt') == calls.count(HREF) == 1


def test_transport_retry_after_does_not_reserve_a_second_request(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return response(url, '', 429, {'Retry-After': '600'})

    monkeypatch.setattr(public_pages.Fetcher, 'get', get)
    with pytest.raises(public_pages.SourceUnavailable, match='http_429'):
        public_pages.get_page(HREF)
    with pytest.raises(public_pages.SourceUnavailable, match='retailer_paused'):
        public_pages.get_page(HREF)
    assert calls == [HREF]
    with Session() as db:
        clock = db.get(RetailerClock, 'www.scoop.com.tn')
        assert clock.paused_until >= time.time() + 595
        assert clock.owner_token is None


def test_transport_retries_once_then_pauses_after_third_failure(monkeypatch):
    calls = []
    instant = 1_800_000_000.0

    def clock():
        return instant

    def sleep(seconds):
        nonlocal instant
        instant += seconds

    monkeypatch.setattr(public_pages, 'time', SimpleNamespace(time=clock, monotonic=clock, sleep=sleep))

    def get(url, **kwargs):
        calls.append(instant)
        raise TimeoutError()

    monkeypatch.setattr(public_pages.Fetcher, 'get', get)
    with pytest.raises(public_pages.SourceUnavailable, match='transport_failure'):
        public_pages.get_page(HREF)
    with pytest.raises(public_pages.SourceUnavailable, match='retailer_paused'):
        public_pages.get_page(HREF)
    with Session() as db:
        clock_row = db.get(RetailerClock, 'www.scoop.com.tn')
        assert clock_row.transport_failures == 3
        assert clock_row.paused_until == calls[-1] + 300
    assert len(calls) == 3
    assert all(later - earlier >= 2 for earlier, later in zip(calls, calls[1:]))
    sleep(300)
    monkeypatch.setattr(public_pages.Fetcher, 'get', lambda url, **kwargs: response(url, pdp()))
    assert public_pages.get_page(HREF).status == 200
    with Session() as db:
        assert db.get(RetailerClock, 'www.scoop.com.tn').transport_failures == 0


def test_batch_single_paced_robots_request(monkeypatch):
    calls = []
    listing_url = ROOT + '/300-pc-portables'
    body = f'<a class="product-thumbnail" href="{HREF}">laptop</a>'
    def get(url, **kwargs):
        calls.append((url, time.monotonic()))
        return response(url, {ROOT + '/robots.txt': 'User-agent: *\nAllow: /',
                              listing_url: body, HREF: pdp('PC Portable Nova')}[url])

    monkeypatch.setattr(public_pages.Fetcher, 'get', get)
    monkeypatch.setattr(public_pages, 'RULES', {})
    result = refresh('scoop', 'laptop', limit=1)
    assert result[0]['products_extracted'] == 1
    assert [url for url, _ in calls] == [ROOT + '/robots.txt', listing_url, HREF]
    assert all(later[1] - earlier[1] >= 1.95 for earlier, later in zip(calls, calls[1:]))
