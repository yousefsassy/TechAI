"""Independent published-evidence judgments for cross-retailer comparison."""
from datetime import datetime
from decimal import Decimal

import pytest

from backend.services.price_comparison import comparison_groups, identity
from backend.services.shopping_criteria import Criteria, parse_budget


GTIN = {'gtin13': '4006381333931'}
MPN = {'mpn': 'MFG-4050'}
OTHER_GTIN = {'gtin13': '9780306406157'}
REPEATED_GTIN = {'gtin13': '0000000000000'}
RETAIL_SKU = {'sku': 'CATALOG-1982'}


def row(id, name, metadata, **evidence):
    product = {'name': name, 'brand': 'Acme', 'category': 'laptop', **evidence}
    return dict(
        product, id=id, supplier='spacenet' if id % 2 else 'scoop', price_tnd=Decimal(10 + id),
        stock_status='in_stock', product_url=f'https://spacenet.tn/products/{id}.html',
        scraped_at=datetime(2026, 11, 25, 19, 17), _identity=identity(product, metadata, id),
    )


@pytest.mark.parametrize('name_a,data_a,facts_a,name_b,data_b,facts_b,merge', [
    ('Acme laptop 512 GB black', GTIN, {}, 'Acme laptop 1 TB black', GTIN, {}, False),
    ('Acme laptop 512 GB black', GTIN, {}, 'Acme laptop 512 GB white', GTIN, {}, False),
    ('Acme laptop RTX 4060', GTIN, {'graphics':'RTX 4060'},
     'Acme laptop RTX 4060 Ti', GTIN, {'graphics':'RTX 4060 Ti'}, False),
    ('Acme laptop 512 GB', GTIN, {'ram_gb':8}, 'Acme laptop 512 GB', GTIN, {'ram_gb':16}, False),
    ('Acme mouse', MPN, {}, 'Pack de 2 Acme souris', MPN, {}, False),
    ('Acme mouse', GTIN, {}, 'Acme casque', {**GTIN}, {'brand':'Beta'}, False),
    ('Acme laptop 512 GB', {**GTIN, **MPN}, {}, 'Acme laptop 512 GB', {**OTHER_GTIN, **MPN}, {}, False),
    ('Acme laptop', REPEATED_GTIN, {}, 'Acme laptop', REPEATED_GTIN, {}, False),
    ('Acme laptop', RETAIL_SKU, {}, 'Acme laptop', RETAIL_SKU, {}, False),
    ('Acme laptop', {}, {'model':'MFG-4050'}, 'Acme laptop', {}, {'model':'MFG-4050'}, False),
    ('Acme laptop 512 GB noir', GTIN, {}, 'Acme notebook 512 GB black', GTIN, {}, True),
    ('Acme laptop', GTIN, {'graphics':'GeForce RTX 4060Ti'},
     'Acme notebook', GTIN, {'graphics':'RTX 4060 Ti'}, True),
    ('Acme laptop 512 GB', MPN, {}, 'Acme notebook 512 GB', {'mpn':'mfg 4050'}, {}, True),
    ('Acme laptop 512 GB', {**GTIN, **MPN}, {}, 'Acme notebook 512 GB', MPN, {}, True),
    ('Acme laptop MFG-4050 512 GB', {}, {'model':'MFG-4050'},
     'Acme laptop MFG 4050 512 GB', {}, {'model':'mfg 4050'}, True),
])
def test_published_variant_pair(name_a, data_a, facts_a, name_b, data_b, facts_b, merge):
    first, second = row(1, name_a, data_a, **facts_a), row(2, name_b, data_b, **facts_b)
    groups = comparison_groups([second, first])
    assert (len(groups) == 1) is merge
    if merge:
        card = groups[0].card()
        assert card['id'] == 1
        assert [offer['id'] for offer in card['offers']] == [1, 2]
        assert card['offers'][0]['scraped_at'] == '2026-11-25T19:17:00'


def test_missing_variant_cannot_bridge_conflict():
    black = row(1, 'Acme laptop black', GTIN)
    white = row(2, 'Acme laptop white', GTIN)
    unspecified = row(3, 'Acme laptop', GTIN)
    cards = [group.card() for group in comparison_groups([unspecified, white, black])]
    assert len(cards) == len({card['group_id'] for card in cards}) == 3


def test_group_id_survives_equivalent_offer_replacement():
    first, second = row(1, 'Acme laptop black', GTIN), row(2, 'Acme laptop noir', GTIN)
    before = comparison_groups([first, second])[0].card()
    after = comparison_groups([second])[0].card()
    assert before['id'] != after['id'] and before['group_id'] == after['group_id']


@pytest.mark.parametrize('message,language,value,exclusive', [
    ('Laptop under 2,500 TND', 'en', '2500', True),
    ('Laptop up to TND 2,500.750', 'en', '2500.750', False),
    ('PC portable jusqu’à 2 500,750 TND', 'fr', '2500.750', False),
    ('سعر لابتوب أقل من ٢٬٥٠٠٫٧٥٠ دينار', 'ar', '2500.750', True),
    ('under 400 TND', 'en', '400', True),
])
def test_budget_millimes_and_explicit_boundaries(message, language, value, exclusive):
    budget, strict = parse_budget(message, language)
    assert budget == Decimal(value)
    assert strict is exclusive
    assert Criteria({'budget': str(budget), 'exclusive_budget': strict}).price_matches(budget) is not strict


@pytest.mark.parametrize('message,error', [
    ('under USD 500', 'currency'),
    ('up to 500 EUR', 'currency'),
    ('budget of €500', 'currency'),
    ('under 2,5,00 TND', 'ambiguous_budget'),
    ('under -400 TND', 'ambiguous_budget'),
])
def test_invalid_budget_abstains(message, error):
    with pytest.raises(ValueError, match=error):
        parse_budget(message, 'en')


def test_paged_search_matches_exhaustive_offer_groups():
    """The winning group retains an eligible retailer offer loaded after the first 12."""
    from datetime import timedelta

    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from backend.database import Session
    from backend.models import Offer, Product, now
    from backend.services.shopping import search

    observations = [('spacenet', 'first-group', 10, 'in_stock', 0, GTIN)]
    observations.extend(('spacenet', f'filler-{index}', 12 + index, 'in_stock', 0, {})
                        for index in range(25))
    observations += [
        ('scoop', 'second-store-first-group', 28, 'in_stock', 0, GTIN),
        ('scoop', 'stale-first-group', 7, 'in_stock', 49, GTIN),
        ('spacenet', 'unavailable-first-group', 6, 'unknown', 0, GTIN),
    ]
    with Session.begin() as db:
        for supplier, key, price, status, age, metadata in observations:
            host = 'spacenet.tn' if supplier == 'spacenet' else 'www.scoop.com.tn'
            product = Product(signature=key, name=f'Souris Acme {key}', category='discovered', brand='Acme',
                              raw_specifications={'metadata': metadata})
            db.add(product)
            db.flush()
            db.add(Offer(product_id=product.id, supplier_name=supplier, supplier_product_id=key,
                         product_url=f'https://{host}/products/{key}.html', price_tnd=price,
                         stock_status=status, scraped_at=now() - timedelta(hours=age)))

    expected = {}
    with Session() as db:
        offers = db.scalars(select(Offer).options(joinedload(Offer.product))).all()
        for offer in offers:
            if not offer.product.name.startswith('Souris Acme ') or offer.stock_status != 'in_stock':
                continue
            if offer.scraped_at < now() - timedelta(hours=48) or offer.price_tnd >= 400:
                continue
            label = offer.product.raw_specifications['metadata'].get('gtin13', str(offer.product_id))
            expected.setdefault(label, []).append((offer.price_tnd, offer.id))
    ranked = sorted(sorted(group) for group in expected.values())[:12]
    assert len(ranked) == 12 and len(ranked[0]) == 2
    actual = search({'type_terms': ['mouse'], 'budget': '400', 'exclusive_budget': True, 'sort':'price'})
    assert [[(Decimal(offer['price_tnd']), offer['id']) for offer in card['offers']] for card in actual] == ranked
