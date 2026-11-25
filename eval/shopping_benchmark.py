"""Run offline shopping profiles against an isolated copy of a catalog.

For a paired run, use this same driver with --checkout pointing to each source
tree and the same --catalog and --clock. Example:

    python -m eval.shopping_benchmark --catalog /tmp/snapshot.db \
        --clock 2026-11-25T19:17:00 --trials 5 --profile warm --profile scale

No source database is opened for writes. Cold profiles replace only origin
responses and preserve real pacing. Stream isolates connection framing and
progress by publishing two committed fixture observations 175 ms apart.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from threading import Event
from time import perf_counter, process_time
from types import SimpleNamespace
from urllib.parse import urlsplit


MOUSE = 'I want to see the mouses that are under 400TND'
GAMING = 'Show me gaming mice under 400 TND'
QUERIES = (
    ('mice', MOUSE, 'en'),
    ('gaming_mice_en', GAMING, 'en'),
    ('gaming_mice_fr', 'Montre-moi des souris gamer à moins de 400 TND', 'fr'),
    ('gaming_mice_ar', 'أرني فأرة ألعاب بأقل من ٤٠٠ دينار', 'ar'),
    ('programming', 'Je cherche un ordinateur portable pour programmer', 'fr'),
    ('wireless_mice', 'Show me Logitech wireless mice', 'en'),
    ('wireless_headset', 'Show me a wireless Logitech headset', 'en'),
    ('rgb_keyboard', 'Show me RGB keyboards under 400 TND', 'en'),
    ('exact_airpods', 'Trouve des AirPods Pro 2 sous 500 TND', 'fr'),
    ('ram_gpu', 'Laptop with 16 GB RAM and RTX 4060', 'en'),
)
DIRECT = {
    'mice': {'type_terms': ['souris'], 'budget': '400'},
    'laptops': {'category': 'laptop', 'budget': '4000'},
    'recommended_laptops': {'category': 'laptop', 'budget': '4000', 'sort': 'recommend'},
}


def distribution(values):
    if not values:
        return None
    latencies = sorted(values)
    return {
        'runs': len(latencies),
        'p50_ms': round(statistics.median(latencies), 3),
        'p95_ms': round(latencies[math.ceil(.95 * len(latencies)) - 1], 3),
        'max_ms': round(latencies[-1], 3),
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def cards_summary(cards):
    # The digest checks complete displayed evidence without exporting merchant text.
    return {
        'cards': len(cards),
        'ordered_ids': [card['id'] for card in cards],
        'offer_ids': [[offer['id'] for offer in card.get('offers', [card])] for card in cards],
        'evidence_sha256': digest(cards),
        'stores': sorted({offer['supplier'] for card in cards for offer in card.get('offers', [card])}),
    }


def database_path():
    return Path(os.environ['DATABASE_URL'].removeprefix('sqlite:///'))


def clone_catalog(source, target, scale=0):
    """Copy just product and offer evidence. Synthetic copies retain offer mix."""
    if not source:
        return
    with sqlite3.connect(target) as db:
        db.execute('ATTACH DATABASE ? AS snapshot', (Path(source).resolve().as_uri() + '?mode=ro',))
        if not scale:
            for table in ('products', 'supplier_offers'):
                source_columns = {row[1] for row in db.execute(f'PRAGMA snapshot.table_info({table})')}
                columns = ', '.join(row[1] for row in db.execute(f'PRAGMA main.table_info({table})')
                                    if row[1] in source_columns)
                db.execute(f'INSERT INTO {table} ({columns}) SELECT {columns} FROM snapshot.{table}')
            return
        product_fields = ('category', 'brand', 'model', 'name', 'processor', 'ram_gb', 'storage_gb',
                          'storage_type', 'screen_size', 'operating_system', 'raw_specifications',
                          'created_at', 'updated_at')
        offer_fields = ('supplier_name', 'price_tnd', 'stock_status', 'available_quantity',
                        'image_url', 'scraped_at', 'is_demo', 'created_at', 'updated_at')
        with_fields = ', '.join(f'p.{field}' for field in product_fields)
        with_offers = ', '.join(f'o.{field}' for field in offer_fields)
        base = list(db.execute(
            f'SELECT {with_fields}, {with_offers}, o.product_url '
            'FROM snapshot.supplier_offers o JOIN snapshot.products p ON p.id = o.product_id '
            'WHERE NOT o.is_demo ORDER BY o.id LIMIT 126'
        ))
        if not base:
            raise ValueError('Scale profile requires a snapshot with non-demo offers')
        products, offers = [], []
        width = len(product_fields)
        for index in range(1, scale + 1):
            row = base[(index - 1) % len(base)]
            products.append((index, f'bench-{index}', *row[:width]))
            domain = urlsplit(row[-1]).netloc
            offers.append((index, index, f'bench-{index}', f'https://{domain}/bench/{index}.html', *row[width:-1]))
        for table, fields, rows in (
            ('products', ('id', 'signature', *product_fields), products),
            ('supplier_offers', ('id', 'product_id', 'supplier_product_id', 'product_url', *offer_fields), offers),
        ):
            db.executemany(f'INSERT INTO {table} ({", ".join(fields)}) VALUES ({",".join("?" for _ in fields)})', rows)


def direct_profile(repeats):
    from sqlalchemy import event
    from sqlalchemy.orm import Session as OrmSession

    from backend.database import engine
    from backend.schemas import SearchInput
    from backend.services import catalog, shopping

    cases = {label: lambda criteria=criteria: shopping.search(criteria) for label, criteria in DIRECT.items()}
    request = SearchInput(requirements={'category': 'laptop', 'budget': 4000})
    cases['legacy_laptop'] = lambda: catalog.search_products(request)
    output = {}
    for label, search in cases.items():
        search()
        times = []
        for _ in range(repeats):
            started = perf_counter()
            cards = search()
            times.append((perf_counter() - started) * 1000)
        counts = Counter()

        def statement(_connection, _cursor, query, *_):
            if query.lstrip().upper().startswith(('SELECT', 'WITH')):
                counts['selects'] += 1

        def load(_session, instance):
            counts[type(instance).__name__] += 1

        event.listen(engine, 'before_cursor_execute', statement)
        event.listen(OrmSession, 'loaded_as_persistent', load)
        try:
            search()
        finally:
            event.remove(engine, 'before_cursor_execute', statement)
            event.remove(OrmSession, 'loaded_as_persistent', load)
        output[label] = {**distribution(times), **cards_summary(cards), 'selects': counts['selects'],
                         'offer_entities': counts['Offer'], 'product_entities': counts['Product']}
    return output


def authenticate():
    from backend.database import Session
    from backend.models import ShoppingAuthSession, ShoppingUser, now

    token = 'benchmark-token-for-local-use-only-00000000000000000'
    with Session.begin() as db:
        db.add(ShoppingUser(id='benchmark-user', email='benchmark@example.test',
                            password_hash='not-used', totp_secret_encrypted='not-used', mfa_enabled=True))
        db.flush()
        db.add(ShoppingAuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id='benchmark-user',
                                   expires_at=now() + timedelta(hours=1)))
    return {'Authorization': f'Bearer {token}'}


class Trace:
    def __init__(self, app):
        self.app = app
        self.frames = []
        self.started = perf_counter()

    async def __call__(self, scope, receive, send):
        async def capture(message):
            if message['type'] == 'http.response.body' and message.get('body') and scope['path'].endswith('/stream'):
                for line in message['body'].splitlines(keepends=True):
                    self.frames.append(((perf_counter() - self.started) * 1000, line))
            await send(message)
        await self.app(scope, receive, capture)

    def samples(self):
        summary = []
        for timestamp, line in self.frames:
            frame = json.loads(line)
            cards = frame.get('data', frame).get('products', frame.get('upserts', []))
            summary.append({
                'type': frame.get('type', 'http_error'), 'ms': round(timestamp, 3), 'bytes': len(line), 'cards': len(cards),
                'verified_offers': sum(offer.get('verified_this_turn', False)
                                       for card in cards for offer in card.get('offers', [card])),
            })
        return summary


async def post(client, headers, message=MOUSE, language='en', **extra):
    started = perf_counter()
    response = await client.post('/shopping/chat', headers=headers, json={'message': message, 'language': language, **extra})
    data = response.json()
    return (perf_counter() - started) * 1000, response.status_code, data


async def warmed_api(app, headers, repeats, bursts):
    import httpx

    output = {'queries': {}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        for label, message, language in QUERIES:
            await post(client, headers, message, language)
            samples = [await post(client, headers, message, language) for _ in range(repeats)]
            output['queries'][label] = summarize_posts(samples)
        _, _, first = await post(client, headers, GAMING)
        observation = await post(client, headers, 'Which one is cheapest?', conversation_id=first['conversation_id'])
        output['followup'] = summarize_posts([observation])
        for amount in (1, 8, 24, 48):
            samples = []
            for _ in range(bursts):
                started = perf_counter()
                responses = await asyncio.gather(*(post(client, headers) for _ in range(amount)))
                burst = summarize_posts(responses)
                burst['wall_ms'] = round((perf_counter() - started) * 1000, 3)
                burst['conversations'] = len({body.get('conversation_id') for _, status, body in responses if status == 200})
                samples.append(burst)
            output[f'burst_{amount}'] = samples
    return output


def summarize_posts(samples):
    outcomes = Counter()
    last = {}
    for _, status, body in samples:
        outcomes[str(status)] += 1
        if status == 200:
            last = body
            outcomes['matches' if body.get('products') else 'empty'] += 1
    return {
        **distribution([ms for ms, _, _ in samples]),
        **cards_summary(last.get('products', [])),
        'statuses': dict(outcomes),
    }


def held_writer(db_path, ready):
    import time

    with sqlite3.connect(db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        ready.set()
        time.sleep(.35)


async def contention(app, headers, runs):
    import httpx

    samples = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        for _ in range(runs):
            ready = Event()
            writer = asyncio.create_task(asyncio.to_thread(held_writer, database_path(), ready))
            if not await asyncio.to_thread(ready.wait, 2):
                await writer
                raise RuntimeError('Fixture writer never acquired its transaction')
            chat = asyncio.create_task(post(client, headers))
            scheduled = perf_counter() + .020
            await asyncio.sleep(max(0, scheduled - perf_counter()))
            health = await client.get('/health')
            delay = (perf_counter() - scheduled) * 1000
            elapsed, status, result = await chat
            await writer
            samples.append({'health_delay_ms': round(delay, 3), 'health_status': health.status_code,
                            'chat_ms': round(elapsed, 3), 'chat_status': status,
                            'chat_cards': len(result.get('products', []))})
    return {
        'trials': samples, 'health_delay': distribution([row['health_delay_ms'] for row in samples]),
        'chat_latency': distribution([row['chat_ms'] for row in samples]),
    }


def fixture_pdp(price):
    evidence = {'@type': 'Product', 'name': 'Souris Gamer Logitech G502 Noir',
                'brand': {'name': 'Logitech'}, 'mpn': '910-005470',
                'offers': {'price': price, 'priceCurrency': 'TND',
                           'availability': 'https://schema.org/InStock'}}
    return '<script type="application/ld+json">' + json.dumps(evidence) + '</script>'


async def post_stream(app, headers, message, version, *, scenario_start=None):
    import httpx

    trace = Trace(app)
    payload = {'message': message, 'language': 'en'}
    route = '/shopping/chat/stream' + ('?stream_version=2' if version == 2 else '')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=trace), base_url='http://fixture') as client:
        response = await client.post(route, headers=headers, json=payload)
    frames = trace.samples()
    final = next((json.loads(body) for (_, body), summary in zip(reversed(trace.frames), reversed(frames))
                  if summary['type'] == 'result'), {})
    final = final.get('data', final)
    return {
        'http_status': response.status_code, 'frames': frames, 'bytes': len(response.content),
        'timeline_offset_ms': round((trace.started - scenario_start) * 1000, 3) if scenario_start else None,
        'first_preview_ms': next((frame['ms'] for frame in frames if frame['type'] == 'progress' and frame['cards']), None),
        'first_verified_ms': next((frame['ms'] for frame in frames
                                   if frame['type'] == 'progress' and frame['verified_offers']), None),
        'final_ms': frames[-1]['ms'] if final else None,
        'final': cards_summary(final.get('products', [])),
        'prices': [[offer['price_tnd'] for offer in card.get('offers', [card])] for card in final.get('products', [])],
        'reply_type': frames[-1]['type'] if frames else None,
    }


async def cold(app, headers, seeded, version, *, partial=False):
    from scrapling import Selector

    from backend.config import settings
    from scraping import discovery, public_pages

    settings.shopping_live_discovery = True
    starts = []
    prices = {'spacenet.tn': '145.900', 'www.scoop.com.tn': '159.900'}
    path = '/36-souris'
    started = perf_counter()

    def get(url, **_):
        parsed = urlsplit(url)
        if parsed.hostname not in prices:
            raise AssertionError('Benchmark attempted access to an unexpected fixture origin')
        status = 200
        headers = {}
        if parsed.path == '/robots.txt':
            kind = 'robots'
            html = 'User-agent: *\nDisallow: /private\n'
            if partial and parsed.hostname == 'spacenet.tn':
                status = 429
                headers = {'Retry-After': '600'}
        elif parsed.path == '/':
            kind = 'home'
            html = f'<a href="{path}">Souris gamer</a>'
        elif parsed.path == path:
            kind = 'listing'
            html = ('<article class="product-miniature"><h2><a href="/12345-souris-gamer-logitech-g502.html">'
                    'Souris Gamer Logitech G502 Noir</a></h2>En stock</article>')
        elif parsed.path == '/12345-souris-gamer-logitech-g502.html':
            kind = 'pdp'
            html = fixture_pdp(prices[parsed.hostname])
        else:
            raise AssertionError(f'Unexpected fixture path {parsed.path}')
        starts.append({'host': parsed.hostname, 'kind': kind, 'status': status,
                       'ms': round((perf_counter() - started) * 1000, 3)})
        return SimpleNamespace(status=status, body=html.encode(), url=url, headers=headers,
                               css=Selector(html, url=url).css)

    for source in discovery.SOURCES.values():
        source.DISCOVERY_CATEGORIES = {'mouse': source.ROOT + path} if seeded else {}
    public_pages.Fetcher.get = get
    leader = asyncio.create_task(post_stream(app, headers, GAMING, version, scenario_start=started))
    await asyncio.sleep(.05)
    follower = asyncio.create_task(post_stream(app, headers, GAMING, version, scenario_start=started))
    first, second = await asyncio.gather(leader, follower)
    origin_calls = len(starts)
    warm = await post_stream(app, headers, GAMING, version, scenario_start=started)
    preview_gap = None
    if first['first_verified_ms'] is not None and second['first_verified_ms'] is not None:
        preview_gap = round(second['timeline_offset_ms'] + second['first_verified_ms'] -
                            first['timeline_offset_ms'] - first['first_verified_ms'], 3)
    return {'leader': first, 'follower': second, 'replay': warm, 'follower_preview_lag_ms': preview_gap, 'origin_gets': origin_calls,
            'warm_origin_gets': len(starts) - origin_calls, 'origin_starts': starts}


async def quick_stream(app, headers, version):
    from scrapling import Selector

    from backend.config import settings
    from backend.services import shopping
    from scraping.parser import parse_product
    from scraping.refresh_catalog import save

    from backend.database import Session
    from backend.models import Offer, Product, now

    settings.shopping_live_discovery = True
    observations = []
    details = 'Capteur optique avec boutons programmables, sensibilité réglable sur plusieurs profils. '
    with Session.begin() as db:
        for number in range(12):
            product = Product(signature=f'bench-stream-{number}', category='discovered',
                              name=f'Souris Gamer Fixture F{number}', brand='Fixture',
                              raw_specifications={'description': details * 7, 'attributes': {'DPI': 12000}})
            db.add(product)
            db.flush()
            db.add(Offer(product_id=product.id, supplier_name='spacenet', supplier_product_id=f'bench-stream-{number}',
                         product_url=f'https://spacenet.tn/bench-stream/{number}.html', price_tnd=90 + number,
                         stock_status='in_stock', scraped_at=now() - timedelta(hours=8)))

    async def discover(_criteria, _candidates, force=False, on_verified=None):
        reports = []
        for index, source in enumerate(('spacenet', 'scoop')):
            await asyncio.sleep(.175)
            host = {'spacenet': 'spacenet.tn', 'scoop': 'www.scoop.com.tn'}[source]
            url = f'https://{host}/12345-souris-gamer-logitech-g502-fixture.html'
            price = ['45.900', '39.900'][index]
            parsed = parse_product(Selector(fixture_pdp(price), url=url), url, 'discovered', source)
            save(parsed)
            observations.append(perf_counter())
            if on_verified:
                on_verified(url)
            reports.append({'retailer': source, 'status': 'checked', 'checked': 1, 'saved': 1, 'urls': [url]})
        return reports

    shopping.discover_products = discover
    started = perf_counter()
    result = await post_stream(app, headers, GAMING, version)
    result['committed_observations_ms'] = [round((timestamp - started) * 1000, 3) for timestamp in observations]
    return result


def freeze(clock):
    """Freeze persisted-observation comparison, leaving pacing's real clock intact."""
    from backend.services import catalog, shopping

    # Legacy and optimized paths import the observation clock into their own modules.
    for module in (catalog, shopping):
        module.now = lambda: clock


def peak_rss_bytes():
    try:
        import resource
    except ImportError:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)


def worker(args):
    from backend.config import settings
    from backend.database import engine, init_db
    from backend.main import app
    from backend.services import shopping_metrics
    from backend.models import Offer
    from sqlalchemy import select, func

    init_db()
    clone_catalog(args.catalog if args.worker in ('warm', 'scale', 'contention') else None, database_path(), args.scale)
    with engine.connect() as db:
        inventory = db.scalar(select(func.count()).select_from(Offer))
    # Use the selected observation clock for catalog eligibility; live PDPs use real timestamps.
    if args.worker in ('warm', 'scale', 'contention'):
        freeze(datetime.fromisoformat(args.clock))
    cpu_started = process_time()
    started = perf_counter()
    if args.worker == 'scale':
        evidence = direct_profile(args.repeats)
    else:
        auth = authenticate()

        async def scenario():
            async with app.router.lifespan_context(app):
                if args.worker == 'warm':
                    return {'direct': direct_profile(args.repeats),
                            'api': await warmed_api(app, auth, args.repeats, args.bursts)}
                if args.worker == 'contention':
                    return await contention(app, auth, args.bursts)
                if args.worker == 'stream':
                    return await quick_stream(app, auth, args.stream_version)
                return await cold(app, auth, args.worker != 'cold', args.stream_version,
                                  partial=args.worker == 'cold_partial')

        evidence = asyncio.run(scenario())
    print(json.dumps({
        'profile': args.worker, 'inventory': inventory, 'evidence': evidence,
        'settings': {'fresh_hours': settings.shopping_fresh_hours, 'max_age_hours': settings.catalog_max_age_hours,
                     'minimum_pacing_seconds': settings.scraping_request_delay},
        'metrics': shopping_metrics.snapshot(), 'wall_ms': round((perf_counter() - started) * 1000, 3),
        'cpu_ms': round(1000 * (process_time() - cpu_started), 3),
        'max_rss_bytes': peak_rss_bytes(),
    }))


def source_metadata(checkout):
    def git(*arguments):
        command = subprocess.run(['git', '-C', str(checkout), *arguments], capture_output=True, check=False)
        return command.stdout if command.returncode == 0 else b''

    files = {}
    for folder in ('backend', 'eval', 'scraping', 'agent', 'mcp_server', 'frontend/src'):
        path = checkout / folder
        for file in path.rglob('*'):
            if file.is_file() and '__pycache__' not in file.parts:
                files[str(file.relative_to(checkout))] = hashlib.sha256(file.read_bytes()).hexdigest()
    return {
        'checkout': str(checkout),
        'revision': git('rev-parse', 'HEAD').decode().strip() or None,
        'tracked_diff_sha256': hashlib.sha256(git('diff', '--binary', 'HEAD')).hexdigest() if (checkout / '.git').exists() else None,
        'source_sha256': digest(files),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--clock', default='2026-11-25T19:17:00')
    parser.add_argument('--repeats', type=int, default=30)
    parser.add_argument('--trials', type=int, default=1, help='Independent worker processes per profile')
    parser.add_argument('--bursts', type=int, default=3, help='Bursts per concurrency or writer holds per trial')
    parser.add_argument('--scale', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--stream-version', type=int, choices=(1, 2), default=1)
    profiles = ('warm', 'scale', 'contention', 'stream', 'cold', 'cold_seeded', 'cold_partial')
    parser.add_argument('--profile', choices=profiles, action='append')
    parser.add_argument('--worker', choices=profiles, help=argparse.SUPPRESS)
    parser.add_argument('--timeout', type=int, default=180, help='Maximum seconds permitted per worker')
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    if not min(args.repeats, args.trials, args.bursts, args.timeout) >= 1:
        parser.error('repeats, trials, bursts and timeout must be positive')
    try:
        args.clock = datetime.fromisoformat(args.clock)
        if args.clock.tzinfo:
            args.clock = args.clock.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        parser.error('clock must be an ISO 8601 timestamp')
    selected = args.profile or ['warm']
    if {'warm', 'scale', 'contention'}.intersection(selected) and not args.catalog:
        parser.error('--catalog is required for warm, scale and contention profiles')
    catalog = args.catalog.resolve() if args.catalog else None
    if catalog and not catalog.is_file():
        parser.error('catalog file does not exist')
    checkout = args.checkout.resolve()
    output = {
        **source_metadata(checkout), 'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'catalog_sha256': hashlib.sha256(catalog.read_bytes()).hexdigest() if catalog else None,
        'clock': args.clock.isoformat(), 'executed_at': datetime.now(timezone.utc).isoformat(),
        'python': platform.python_version(), 'platform': platform.platform(), 'machine': platform.machine(),
        'cpu_count': os.cpu_count(),
        'dependencies': {name: importlib.metadata.version(name)
                         for name in ('fastapi', 'sqlalchemy', 'httpx', 'scrapling')},
        'sqlite': sqlite3.sqlite_version, 'trials': [],
    }
    for profile in selected:
        for scale in ((126, 1260, 6300) if profile == 'scale' else (0,)):
            for _ in range(args.trials):
                with tempfile.TemporaryDirectory(prefix='techstock-benchmark-') as tmp:
                    env = {**os.environ, 'PYTHONPATH': str(checkout), 'DATABASE_URL': f'sqlite:///{tmp}/catalog.db',
                           'OPENAI_API_KEY': '', 'OPENROUTER_API_KEY': '', 'LANGSMITH_API_KEY': '',
                           'LANGSMITH_TRACING': 'false', 'SHOPPING_LIVE_DISCOVERY': 'false', 'PYTHON_DOTENV_DISABLED': '1',
                           'CATALOG_AUTO_REFRESH': 'false', 'EMAIL_MODE': 'sandbox'}
                    command = [sys.executable, str(Path(__file__).resolve()), '--worker', profile, '--clock',
                               args.clock.isoformat(), '--scale', str(scale), '--repeats', str(args.repeats),
                               '--bursts', str(args.bursts), '--stream-version', str(args.stream_version)]
                    if catalog:
                        staged = Path(tmp) / 'snapshot.db'
                        # Read-only attach works on the isolated copy even for a mode=wal snapshot.
                        with sqlite3.connect(staged) as destination:
                            with sqlite3.connect(catalog.as_uri() + '?mode=ro', uri=True) as origin:
                                origin.backup(destination)
                        command += ['--catalog', str(staged)]
                    try:
                        result = subprocess.run(command, env=env, cwd=tmp, stdout=subprocess.PIPE, check=True, timeout=args.timeout)
                    except subprocess.CalledProcessError as exc:
                        raise SystemExit(f'{profile} worker failed with exit status {exc.returncode}') from exc
                    except subprocess.TimeoutExpired as exc:
                        raise SystemExit(f'{profile} exceeded the {args.timeout}-second worker deadline') from exc
                    output['trials'].append(json.loads(result.stdout))
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
