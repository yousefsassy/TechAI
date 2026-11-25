"""Bounded, process-local shopping telemetry with request-local summaries."""
from bisect import bisect_left
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from time import perf_counter

from backend.config import settings

_current = ContextVar('shopping_observation', default=None)
_totals = Counter()
_requests = defaultdict(lambda: deque(maxlen=20))
_histograms = {}
_lock = Lock()
_BUCKETS_MS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 15_000, 30_000, 120_000)
_STAGES = frozenset({
    'turn', 'routing', 'model', 'sql_read', 'sql_write', 'sql_lock', 'ranking',
    'queue_wait', 'host_spacing', 'http', 'parse', 'commit', 'first_cached', 'first_verified',
    'stream_send', 'final',
})
_COUNTERS = frozenset({
    'sql_statements', 'rows_hydrated', 'candidates_examined', 'pdp_accepted', 'pdp_rejected',
    'http_retries', 'stream_bytes', 'previews_coalesced', 'previews_dropped', 'shared_lookups',
    'provider_cooldowns',
})
_HOSTS = {'spacenet.tn', 'www.scoop.com.tn', 'www.mytek.tn', 'www.tunisianet.com.tn'}
_STORES = {'spacenet', 'scoop', 'mytek', 'tunisianet'}
_PAGE_KINDS = {'robots', 'navigation', 'listing', 'sitemap', 'pdp'}


def _bucket(name, duration_ms):
    bucket = bisect_left(_BUCKETS_MS, max(0, duration_ms))
    if name not in _histograms:
        _histograms[name] = [0] * (len(_BUCKETS_MS) + 1)
    _histograms[name][bucket] += 1


def record_stage(name, elapsed_ms):
    if name in _STAGES:
        with _lock:
            _bucket(name, elapsed_ms)


@contextmanager
def stage(name):
    started = perf_counter()
    try:
        yield
    finally:
        record_stage(name, elapsed(started))


def increment(name, amount=1):
    if name in _COUNTERS and amount >= 0:
        with _lock:
            _totals[name] += amount
            if name == 'shared_lookups' and (m := _current.get()) is not None:
                m['shared'] = True


def record_request(host, ok, *, kind=None, status=None):
    # 'other' keeps labels bounded even if a retailer sends an unexpected host.
    host = host if host in _HOSTS else 'other'
    kind = kind if kind in _PAGE_KINDS else 'other'
    status_class = f'{status // 100}xx' if isinstance(status, int) and 100 <= status < 600 else 'unknown'
    with _lock:
        _requests[host].append(ok)
        _totals['http_requests'] += 1
        _totals['http_failures'] += int(not ok)
        _totals[f'http_{host}_{kind}_{status_class}'] += 1


def source_health():
    with _lock:
        return {host: (sum(history) + 4) / (len(history) + 5) for host, history in _requests.items()}


def begin():
    return _current.set(dict(start=perf_counter(), first_ms=None, first_verified_ms=None,
                             lookups=0, hits=0, scrapes=0, failures=0, scrape_ms=0,
                             initial_hit=False, shared=False, input_tokens=0, output_tokens=0,
                             llm_calls=0, unknown_usage=0))


def elapsed(start):
    return round((perf_counter() - start) * 1000, 2)


def retrieved(cards):
    if (m := _current.get()) is not None:
        if m['lookups'] == 0:
            m['initial_hit'] = bool(cards)
        m['lookups'] += 1
        m['hits'] += bool(cards)


def first(cards):
    m = _current.get()
    if not cards or m is None:
        return
    current = elapsed(m['start'])
    verified = any(offer.get('verified_this_turn') for card in cards for offer in card.get('offers', [card]))
    if m['first_ms'] is None:
        m['first_ms'] = current
        if not verified:
            record_stage('first_cached', current)
    if verified and m['first_verified_ms'] is None:
        m['first_verified_ms'] = current
        record_stage('first_verified', current)


def scraped(start, reports):
    if (m := _current.get()) is not None:
        m['scrape_ms'] += elapsed(start)
        for report in reports:
            if report['status'] != 'recent_attempt':
                m['scrapes'] += 1
                m['failures'] += report['status'] not in ('checked', 'no_match')


def model_started():
    if (m := _current.get()) is not None:
        m['llm_calls'] += 1
        m['unknown_usage'] += 1


def model_usage(usage):
    keys = ('input_tokens', 'output_tokens')
    if (m := _current.get()) is not None and usage and all(isinstance(usage.get(key), int) for key in keys):
        m['unknown_usage'] = max(0, m['unknown_usage'] - 1)
        for key in keys:
            m[key] += usage[key]


def finish(token, cards, success):
    m = _current.get()
    _current.reset(token)
    stores = {o['supplier'] for p in cards for o in p.get('offers', [p])}
    duration = elapsed(m['start'])
    cost = None
    if not m['llm_calls']:
        cost = 0
    elif not m['unknown_usage'] and settings.input_cost_per_million is not None and settings.output_cost_per_million is not None:
        cost = (m['input_tokens'] * settings.input_cost_per_million + m['output_tokens'] * settings.output_cost_per_million) / 1e6
    with _lock:
        _bucket('turn', duration)
        if success:
            _bucket('final', duration)
        if m['first_ms'] is not None:
            _totals['first_result_ms'] += m['first_ms']
            _totals['turns_with_results'] += 1
        if cost is not None:
            _totals['estimated_cost_usd'] += cost
        _totals.update({key: m[key] for key in ('lookups', 'hits', 'scrapes', 'failures', 'llm_calls',
                                               'unknown_usage', 'input_tokens', 'output_tokens', 'scrape_ms')})
        _totals.update(turns=1, failed_turns=int(not success), duration_ms=duration,
                       initial_local_hits=int(m['initial_hit']),
                       fully_local_hits=int(m['initial_hit'] and success and not m['scrapes'] and not m['shared']),
                       verified_live_hits=int(m['first_verified_ms'] is not None))
        outcome = 'failed' if not success else ('partial' if cards and m['failures']
                                               else 'matches' if cards else 'unavailable' if m['failures'] else 'empty')
        _totals['outcome_' + outcome] += 1
        for store in stores:
            _totals['results_from_' + (store if store in _STORES else 'other')] += 1
    return dict(total_ms=duration, first_result_ms=m['first_ms'], first_verified_ms=m['first_verified_ms'],
                retrievals=m['lookups'], cache_hits=m['hits'], scrape_ms=m['scrape_ms'],
                retailer_failures=m['failures'], retailers=sorted(stores), input_tokens=m['input_tokens'],
                output_tokens=m['output_tokens'], estimated_cost_usd=cost)


def snapshot():
    with _lock:
        counts = dict(_totals)
        counts['stage_histograms'] = {
            'upper_bounds_ms': list(_BUCKETS_MS),
            'bucket_counts': {key: counts.copy() for key, counts in _histograms.items()},
        }
    counts['cache_hit_rate'] = counts.get('hits', 0) / counts['lookups'] if counts.get('lookups') else None
    return counts
