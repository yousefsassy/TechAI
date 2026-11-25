"""Replay frozen shopping relevance judgments in a disposable database.

Run with `python -m eval.shopping_quality --checkout /path/to/checkout`.
The same queries, inventory and grading apply to the baseline and optimized code.
"""
import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile


FIXTURE = Path(__file__).with_name('shopping_quality.json')


def dcg(values):
    return sum((2 ** grade - 1) / math.log2(i + 2) for i, grade in enumerate(values))


def populate(items):
    from backend.database import Session, init_db
    from backend.models import Offer, Product

    init_db()
    clock = datetime(2026, 11, 25, 19, 17)
    ids = {}
    with Session.begin() as db:
        for item in items:
            key, category, brand, title, cost, fields = item
            fields = dict(fields)
            status = fields.pop('availability', 'in_stock')
            stale = fields.pop('stale', False)
            raw = fields.pop('raw', {})
            product = Product(signature=key, name=title, category=category, brand=brand,
                              raw_specifications=raw, **fields)
            db.add(product)
            db.flush()
            offer = Offer(product_id=product.id, supplier_name='spacenet', supplier_product_id=key,
                          product_url=f'https://spacenet.tn/products/{key}.html', price_tnd=Decimal(cost),
                          stock_status=status, scraped_at=clock - timedelta(hours=49 if stale else 0))
            db.add(offer)
            db.flush()
            ids[offer.id] = key
    return clock, ids


async def replay(fixture):
    from backend.services import shopping

    clock, ids = populate(fixture['inventory'])
    shopping.now = lambda: clock
    records = []
    for case in fixture['queries']:
        for language, message in case['messages'].items():
            expected = case['grades']
            previous = {}
            if case.get('context'):
                _, previous, issue = await shopping.plan_turn(case['context'][language], language, {}, [], [])
                if issue:
                    previous = {}
            _, criteria, issue = await shopping.plan_turn(message, language, previous, [], [])
            cards = [] if issue else shopping.search(criteria)
            observed = [ids[card['id']] for card in cards]
            missed = sorted(set(expected).difference(observed))
            leaked = sorted(set(observed).difference(expected))
            records.append(dict(
                case=case['case'], language=language, query=message, expected=expected,
                observed=observed, missed=missed, leaked=leaked,
                abstention=bool(issue), empty_ok=(not observed and bool(issue) == case.get('clarify', False)) if not expected else None,
                recall_at_12=(len(expected) - len(missed)) / len(expected) if expected else None,
                ndcg_at_10=dcg([expected.get(key, 0) for key in observed[:10]]) /
                dcg(sorted(expected.values(), reverse=True)[:10]) if expected else None,
            ))
    aggregates = defaultdict(list)
    for row in records:
        aggregates['all'].append(row)
        aggregates[row['language']].append(row)
    return {'summary': {key: summarize(rows) for key, rows in aggregates.items()}, 'cases': records}


def summarize(rows):
    nonempty = [row for row in rows if row['expected']]
    empty = [row for row in rows if not row['expected']]
    return dict(
        queries=len(rows), rated_nonempty=len(nonempty), rated_empty=len(empty),
        recall_at_12=statistics.mean(row['recall_at_12'] for row in nonempty),
        ndcg_at_10=statistics.mean(row['ndcg_at_10'] for row in nonempty),
        queries_with_filter_violations=sum(bool(row['leaked']) for row in rows),
        leaked_cards=sum(len(row['leaked']) for row in rows),
        correct_empty=sum(bool(row['empty_ok']) for row in empty),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, default=FIXTURE.resolve().parents[1])
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(asyncio.run(replay(json.loads(FIXTURE.read_text()))), ensure_ascii=False, indent=2))
        return
    with tempfile.TemporaryDirectory(prefix='techstock-quality-') as tmp:
        subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker'], cwd=tmp, env={
            **os.environ, 'PYTHONPATH': str(args.checkout.resolve()), 'DATABASE_URL': f'sqlite:///{tmp}/quality.db',
            'SHOPPING_LIVE_DISCOVERY': 'false', 'CATALOG_AUTO_REFRESH': 'false', 'LANGSMITH_TRACING': 'false',
            'OPENAI_API_KEY': '', 'OPENROUTER_API_KEY': '', 'LANGSMITH_API_KEY': '', 'SHOPPING_LOCAL_PLANNER': 'true',
        }, check=True)


if __name__ == '__main__':
    main()
