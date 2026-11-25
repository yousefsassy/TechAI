from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, event, select

from backend.database import Session, engine, migrate
from backend.models import Offer, Product
from backend.schemas import Requirements, SearchInput
from backend.services.catalog import graphics_from_details, graphics_tier, list_catalog, matches, search_products, serialize
from backend.services.title_index import title_candidates


def test_search_matches_exhaustive_oracle_before_enforcing_limit():
    with Session.begin() as db:
        product = Product(signature='retrieval', category='laptop', name='Variant laptop',
                          brand='Kron', processor='Élan', ram_gb=16, storage_gb=512,
                          storage_type='SSD')
        db.add(product)
        for i in range(130):
            db.add(Offer(product=product, supplier_name='test', supplier_product_id=f'retrieval-{i}',
                         price_tnd=Decimal('333.334' if i == 15 else '333.333'),
                         stock_status='in_stock', available_quantity=2 if i < 8 else None,
                         product_url=(f'https://example.org/{i}' if i < 15 else
                                      f'https://www.mytek.tn/retrieval-{i}.html')))

    request = SearchInput(requirements=Requirements(category='laptop', quantity=3, budget='1000',
                                                   brands=['kron'], processor='élan', min_ram=16,
                                                   min_storage=512, storage_type='SSD'),
                          max_unit_price=Decimal('333.3335'))
    with Session() as db:
        oracle = [serialize(offer) for offer in db.scalars(select(Offer).order_by(Offer.id))
                  if matches(offer, request.requirements)
                  and offer.price_tnd <= request.max_unit_price][:100]

    statements = []

    def count_selects(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)

    event.listen(engine, 'before_cursor_execute', count_selects)
    try:
        actual = search_products(request)
    finally:
        event.remove(engine, 'before_cursor_execute', count_selects)

    assert len(oracle) == 100
    assert actual == oracle
    assert len(statements) == 1


def test_catalog_keyset_pages_preserve_full_result():
    full = list_catalog()
    first = list_catalog(limit=2)
    second = list_catalog(limit=2, after_id=first[-1]['id'])
    assert len(full) == 3
    assert first + second == full
    assert list_catalog(after_id=full[-1]['id']) == []


def test_catalog_keeps_legacy_rows_with_malformed_raw_metadata_readable():
    with Session.begin() as db:
        db.get(Product, 1).raw_specifications = {'attributes': ['old'], 'metadata': True}
        db.get(Product, 2).raw_specifications = ['old']
    first, second, _ = list_catalog()
    for row in (first, second):
        assert row['specifications'] == {}
        assert row['graphics'] is None
        assert row['rating'] is None


def test_procurement_search_rejects_image_links_from_the_retailer():
    with Session.begin() as db:
        db.get(Offer, 1).product_url = 'https://www.mytek.tn/demo-laptop.JPG?w=720'
    request = SearchInput(requirements=Requirements(category='laptop', budget='5000'))
    assert search_products(request) == []


def test_gpu_strength_accepts_both_ti_spellings():
    assert graphics_tier('RTX 4060Ti') == graphics_tier('RTX 4060 Ti') == 88
    assert graphics_tier('RTX 4060') == 80
    assert graphics_tier('Intel Arc A770') == -1


def test_discrete_gpu_evidence_retains_published_radeon_variants():
    assert graphics_from_details('Gaming PC Radeon RX 7900 XT', {}) == 'Radeon RX 7900 XT'
    assert graphics_from_details('Gaming PC RX7900XTX', {}) == 'RX7900XTX'
    assert graphics_from_details('Sony RX100 camera', {}) is None
    assert graphics_tier('Radeon RX 7900 XTX') == -1


def test_retailer_clock_migration_preserves_existing_pacing(tmp_path):
    legacy = create_engine(f'sqlite:///{tmp_path / "legacy.db"}')
    try:
        with legacy.begin() as connection:
            connection.exec_driver_sql(
                'CREATE TABLE retailer_request_clocks '
                '(host VARCHAR PRIMARY KEY, next_request FLOAT, paused_until FLOAT)'
            )
            connection.exec_driver_sql(
                "INSERT INTO retailer_request_clocks VALUES ('spacenet.tn', 10.5, 300.25)"
            )
            migrate(connection)
        with legacy.begin() as connection:
            migrate(connection)
            row = connection.exec_driver_sql(
                'SELECT next_request, paused_until, active_until, owner_token, transport_failures '
                'FROM retailer_request_clocks'
            ).one()
            assert row == (10.5, 300.25, 0.0, None, 0)
    finally:
        legacy.dispose()


def test_two_processes_can_initialize_the_same_database(tmp_path):
    env = dict(os.environ, DATABASE_URL=f'sqlite:///{tmp_path / "concurrent.db"}')
    source = Path(__file__).resolve().parents[1]
    command = [sys.executable, '-c', 'from backend.database import init_db; init_db()']
    workers = []
    try:
        for _ in range(2):
            worker = subprocess.Popen(command, cwd=source, env=env, stderr=subprocess.PIPE)
            workers.append(worker)
        for worker in workers:
            _, error = worker.communicate(timeout=15)
            assert worker.returncode == 0, error.decode()
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate()


def test_fts_retains_accented_legacy_titles_and_tracks_updates():
    with Session.begin() as db:
        product = Product(signature='accented-mouse', category='accessories', name='فَأْرَة لاسلكية')
        offer = Offer(product=product, supplier_name='test', supplier_product_id='accented-mouse',
                      price_tnd=Decimal('50.000'), stock_status='in_stock',
                      product_url='https://www.mytek.tn/accented-mouse.html')
        db.add(offer)
    query = title_candidates(select(Offer.id), ['mouse'])
    with Session.begin() as db:
        assert db.scalars(query).all() == [offer.id]
        db.get(Product, product.id).name = 'Souris sans fil'
    with Session.begin() as db:
        assert db.scalars(query).all() == [offer.id]
        db.get(Product, product.id).name = 'Clavier sans fil'
    with Session() as db:
        assert db.scalars(query).all() == []
        db.get(Product, product.id).name = 'Souris sans fil'
        db.flush()
        assert db.scalars(query).all() == [offer.id]
        db.rollback()
        assert db.scalars(query).all() == []
    with Session.begin() as db:
        db.delete(db.get(Offer, offer.id))
        db.flush()
        db.delete(db.get(Product, product.id))
    with Session() as db:
        direct_match = "SELECT rowid FROM product_titles WHERE product_titles MATCH 'clavier'"
        assert db.connection().exec_driver_sql(direct_match).all() == []
