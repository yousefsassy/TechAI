"""Shared, identified HTTP transport for daily and on-demand collection."""
import asyncio
import random
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import copy_context
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from threading import Event, Lock
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError, Timeout as CurlTimeout
from protego import Protego
from scrapling.fetchers import Fetcher
from sqlalchemy import text

from backend.config import settings
from backend.database import Session
from backend.models import RetailerClock, now
from backend.services import shopping_metrics as metrics
from backend.services.catalog import safe_url

USER_AGENT = 'TechStockAcademicBot/1.0'
FETCH = dict(timeout=15, impersonate=None, stealthy_headers=False, headers={'User-Agent': USER_AGENT},
             follow_redirects=False, retries=0)

RULES = {}
_ROBOTS_LOCKS = {}
_ROBOTS_LOCK = Lock()
_ACTIVE_PUBLIC_PAGES = {}
_PAGES_LOCK = Lock()
_BATCH_FETCH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='retailer-get')
_TRANSIENT = (CurlConnectionError, CurlTimeout, ConnectionError, TimeoutError)
_DENIED_STATUSES = {401, 403, 429, 503}
_RETRY_LIMIT = 1
_WAIT_POLL = 0.1


class SourceUnavailable(Exception):
    pass


@dataclass
class _PublicTransfer:
    future: Future = field(default_factory=Future)
    consumers: int = 1


class _TransferInterest:
    def __init__(self, transfer, cancel):
        self.transfer = transfer
        self.cancel = cancel

    def is_set(self):
        with _PAGES_LOCK:
            return self.cancel is not None and self.cancel.is_set() and self.transfer.consumers == 1

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if self.is_set():
                return True
            time.sleep(min(_WAIT_POLL, remaining))
        return self.is_set()


def _remaining(deadline, cancel=None):
    if cancel is not None and cancel.is_set():
        raise SourceUnavailable('cancelled')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceUnavailable('time_limit')
    return remaining


def allowed_url(root, url):
    if not safe_url(url):
        return False
    parts = urlsplit(url)
    if parts.netloc != urlsplit(root).netloc or parts.fragment:
        return False
    # Refuse storefront search/controller endpoints even if a robots snapshot
    # omitted them. Category pagination may use a query.
    return not re.search(r'(?:^|/)(?:recherche|search|api\.php)(?:/|$)', unquote(parts.path),
                         re.IGNORECASE) and not re.search(r'(?:^|&)controller=', unquote(parts.query), re.IGNORECASE)


def _claim(host, spacing, deadline, token, cancel):
    fair_share = _remaining(deadline, cancel)
    with Session.begin() as db:
        # Stay shorter than the request's remaining budget when a collector or
        # turn holds the SQLite write lock. Restore the pooled connection value.
        connection = db.connection()
        original_timeout = connection.exec_driver_sql('PRAGMA busy_timeout').scalar_one()
        bounded_timeout = min(int(fair_share * 1000), original_timeout)
        connection.exec_driver_sql(f'PRAGMA busy_timeout = {bounded_timeout:d}')
        try:
            with metrics.stage('sql_lock'):
                db.execute(text('BEGIN IMMEDIATE'))
        finally:
            connection.exec_driver_sql(f'PRAGMA busy_timeout = {original_timeout:d}')
        remaining = _remaining(deadline, cancel)
        current = time.time()
        clock = db.get(RetailerClock, host)
        if clock is None:
            clock = RetailerClock(host=host, next_request=0, paused_until=0,
                                  active_until=0, transport_failures=0)
            db.add(clock)
        if clock.paused_until > current:
            raise SourceUnavailable('retailer_paused')
        if clock.owner_token and clock.active_until > current:
            return min(max(clock.next_request - current, _WAIT_POLL), remaining)
        wait = clock.next_request - current
        if wait > 0:
            if wait >= remaining:
                raise SourceUnavailable('time_limit')
            return wait
        clock.owner_token = token
        clock.active_until = current + min(remaining, FETCH['timeout']) + 5
        clock.next_request = current + spacing
        return 0


def _retry_after(page):
    header = str(page.headers.get('Retry-After') or page.headers.get('retry-after') or '').strip()
    try:
        return time.time() + int(header) if header.isdigit() else parsedate_to_datetime(header).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def _challenge(page):
    headers = {key.lower(): value for key, value in page.headers.items()}
    if str(headers.get('cf-mitigated', '')).lower() == 'challenge':
        return True
    if page.status != 200:
        return False
    body = page.body[:8192].lower()
    return b'<title>just a moment' in body and (b'cf-chl-' in body or b'challenge-form' in body)


def _page_kind(url):
    path = urlsplit(url).path.lower()
    if path == '/robots.txt':
        return 'robots'
    if path in ('', '/'):
        return 'navigation'
    if path.endswith(('.xml', '.xml.gz')):
        return 'sitemap'
    if path.endswith('.html'):
        return 'pdp'
    return 'listing'


def _complete(host, token, *, transient=False, pause=0, attempted=True):
    with Session.begin() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        clock = db.get(RetailerClock, host)
        if clock is None or clock.owner_token != token:
            return
        clock.owner_token = None
        clock.active_until = 0
        if attempted:
            clock.transport_failures = (clock.transport_failures + 1) if transient else 0
        if clock.transport_failures >= 3:
            pause = max(pause, time.time() + 300)
        clock.paused_until = max(clock.paused_until, pause)


def _request(url, host, deadline, token, cancel):
    try:
        timeout = min(FETCH['timeout'], _remaining(deadline, cancel))
    except SourceUnavailable:
        _complete(host, token, attempted=False)
        raise
    try:
        observed_at = now()
        with metrics.stage('http'):
            page = Fetcher.get(url, **dict(FETCH, timeout=timeout))
        if isinstance(getattr(page, 'meta', None), dict):
            page.meta['observed_at'] = observed_at
    except Exception as exc:
        metrics.record_request(host, False, kind=_page_kind(url))
        _complete(host, token, transient=isinstance(exc, _TRANSIENT))
        raise
    try:
        challenged = _challenge(page)
        denied = challenged or page.status in _DENIED_STATUSES
        metrics.record_request(host, 200 <= page.status < 300 and not denied,
                               kind=_page_kind(url), status=page.status)
        pause = max(_retry_after(page), time.time() + 300) if denied else 0
    except Exception:
        _complete(host, token)
        raise
    _complete(host, token, pause=pause)
    if denied:
        raise SourceUnavailable('retailer_challenge' if challenged else f'http_{page.status}')
    return page


def _parameters(url, delay, deadline):
    if not safe_url(url):
        raise SourceUnavailable('unsupported_url')
    host = urlsplit(url).hostname
    spacing = max(delay, settings.scraping_request_delay, 10 if host == 'spacenet.tn' else 2)
    return host, spacing, deadline if deadline is not None else time.monotonic() + 95


def _wait(wait, deadline, cancel):
    if wait >= _remaining(deadline, cancel):
        raise SourceUnavailable('time_limit')
    if cancel is None:
        time.sleep(wait)
    elif cancel.wait(wait):
        raise SourceUnavailable('cancelled')


def get_page(url, delay=0, deadline=None, *, cancel=None):
    host, spacing, deadline = _parameters(url, delay, deadline)
    for retry in range(_RETRY_LIMIT + 1):
        token = uuid4().hex
        while wait := _claim(host, spacing, deadline, token, cancel):
            with metrics.stage('host_spacing'):
                _wait(wait, deadline, cancel)
        try:
            return _request(url, host, deadline, token, cancel)
        except _TRANSIENT as exc:
            if retry == _RETRY_LIMIT:
                raise SourceUnavailable('transport_failure') from exc
            metrics.increment('http_retries')
            _wait(random.uniform(0.05, 0.2), deadline, cancel)


def get_page_shared(key, fetch, deadline, cancel=None):
    """Join active public URL work while keeping each caller's eligibility private."""
    with _PAGES_LOCK:
        pending = _ACTIVE_PUBLIC_PAGES.get(key)
        if pending is None:
            if len(_ACTIVE_PUBLIC_PAGES) >= 32:
                raise SourceUnavailable('retailer_queue_full')
            pending = _ACTIVE_PUBLIC_PAGES[key] = _PublicTransfer()
            leader = True
        else:
            pending.consumers += 1
            leader = False
    if leader:
        try:
            page = fetch(_TransferInterest(pending, cancel) if cancel else None)
        except BaseException as exc:
            pending.future.set_exception(exc)
            raise
        else:
            pending.future.set_result(page)
            return page
        finally:
            with _PAGES_LOCK:
                _ACTIVE_PUBLIC_PAGES.pop(key, None)
    try:
        with metrics.stage('queue_wait'):
            while not pending.future.done():
                try:
                    return pending.future.result(timeout=min(_WAIT_POLL, _remaining(deadline, cancel)))
                except FutureTimeout:
                    pass
        return pending.future.result()
    finally:
        with _PAGES_LOCK:
            pending.consumers -= 1


def _claim_and_dispatch(url, host, spacing, deadline, token, cancel):
    wait = _claim(host, spacing, deadline, token, cancel)
    if wait:
        return wait, None
    return 0, _BATCH_FETCH_POOL.submit(copy_context().run, _request, url, host, deadline, token, cancel)


async def get_page_async(url, delay=0, deadline=None):
    host, spacing, deadline = _parameters(url, delay, deadline)
    cancelled = Event()
    try:
        for retry in range(_RETRY_LIMIT + 1):
            token = uuid4().hex
            while True:
                wait, request = await asyncio.to_thread(_claim_and_dispatch,
                                                         url, host, spacing, deadline, token, cancelled)
                if request is not None:
                    break
                if wait >= _remaining(deadline):
                    raise SourceUnavailable('time_limit')
                with metrics.stage('host_spacing'):
                    await asyncio.sleep(wait)
            try:
                # Cancelling a caller must let the blocking transfer release
                # its own clock token instead of releasing it early.
                transfer = asyncio.wrap_future(request)
                transfer.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
                return await asyncio.shield(transfer)
            except _TRANSIENT as exc:
                if retry == _RETRY_LIMIT:
                    raise SourceUnavailable('transport_failure') from exc
                metrics.increment('http_retries')
                await asyncio.sleep(random.uniform(0.05, 0.2))
    finally:
        cancelled.set()


def cache_lifetime(headers, ceiling):
    directive = str(headers.get('Cache-Control') or headers.get('cache-control') or '').lower()
    if re.search(r'\b(?:no-cache|no-store)\b', directive):
        return 0
    max_age = re.search(r'(?:^|,)\s*max-age=(\d+)', directive)
    return min(ceiling, int(max_age[1])) if max_age else ceiling


def get_rules(root, deadline=None, *, fetch=None, cache=None, cancel=None):
    """Use one robots decision per origin and identified bot; expired rules fail closed."""
    fetch = get_page if fetch is None else fetch
    cache = RULES if cache is None else cache
    deadline = time.monotonic() + 95 if deadline is None else deadline
    cached = cache.get(root)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    with _ROBOTS_LOCK:
        lock = _ROBOTS_LOCKS.setdefault(root, Lock())
    if not lock.acquire(timeout=_remaining(deadline, cancel)):
        raise SourceUnavailable('time_limit')
    try:
        cached = cache.get(root)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        kwargs = {'deadline': deadline}
        if cancel is not None:
            kwargs['cancel'] = cancel
        response = fetch(root + '/robots.txt', **kwargs)
        if response.status != 200 or len(response.body) > 512_000 or response.body.lstrip().startswith(b'<'):
            raise SourceUnavailable('robots_unavailable')
        try:
            rules = Protego.parse(response.body.decode('utf-8-sig'))
        except (ValueError, UnicodeError) as exc:
            raise SourceUnavailable('robots_unavailable') from exc
        ttl = cache_lifetime(getattr(response, 'headers', {}), 600)
        if ttl:
            cache[root] = time.monotonic() + ttl, rules
        return rules
    finally:
        lock.release()


class PacedSession:
    """Scrapling batch transport, with cancellable pacing outside the fetch pool."""
    _is_alive = False

    def __init__(self, root):
        self.root = root

    async def __aenter__(self):
        self._is_alive = True
        return self

    async def __aexit__(self, *args):
        self._is_alive = False

    async def fetch(self, url, **kwargs):
        deadline = time.monotonic() + 95
        rules = await asyncio.to_thread(get_rules, self.root, deadline)
        if not allowed_url(self.root, url) or not rules.can_fetch(url, USER_AGENT):
            raise SourceUnavailable('robots_disallow')
        return await get_page_async(url, float(rules.crawl_delay(USER_AGENT) or 0), deadline)
