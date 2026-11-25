from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.database import Session, init_db
from backend.models import Offer, Product, now
from backend.services import shopping
from backend.services.price_comparison import mark_verified
from tests.test_conversational_discovery import add


def listing(name, price, retailer='spacenet', gtin=None, stock='in_stock', age=0):
    host = {'spacenet':'spacenet.tn', 'scoop':'www.scoop.com.tn'}[retailer]
    with Session.begin() as db:
        product = Product(name=name, category='discovered', signature=retailer + name,
                          brand='Logitech', raw_specifications={'metadata':{'gtin13':gtin}})
        db.add(product)
        db.flush()
        url = f'https://{host}/{product.id}.html'
        db.add(Offer(product_id=product.id, supplier_name=retailer, supplier_product_id=str(product.id),
                     product_url=url, price_tnd=Decimal(price), stock_status=stock, scraped_at=now()-timedelta(hours=age)))
        return url


@pytest.mark.parametrize('query', ['Show me gaming mice under 400 TND', 'Montre-moi une souris gamer sous 400 TND',
                                  'اعرض لي ماوس للالعاب تحت ٤٠٠ دينار'])
async def test_gaming_budget_and_stocks(query):
    for name, price in [('Souris USB', 7), ('Pack Clavier Souris Gamer', 20), ('Tapis de souris gamer', 35), ('Gaming Mouse Pad', 15),
                        ('Souris Gamer', 90), ('Souris Gamer Pro', 400)]:
        listing(name, price)
    listing('Souris gamer blanche', 30, stock='unknown')
    listing('Souris gamer grise', 45, age=96)
    result = await shopping.reply('shopper', query)
    assert [p['name'] for p in result['products']] == ['Souris Gamer']
    assert result['metrics']['cache_hits'] == 1 and result['metrics']['estimated_cost_usd'] == 0


async def test_price_groups_ranking_and_followup():
    a = listing('Souris Gamer Logitech sans fil 16000 DPI', '89.990', gtin='4006381333931')
    b = listing('Logitech Souris Gaming Wireless 16000 DPI', '99.000', 'scoop', '4006381333931')
    listing('Souris Gamer sans fil Junior', '25')
    result = await shopping.reply('user', 'Find the best wireless gaming mouse under 400 TND')
    best, other = result['products']
    assert best['price_tnd'] == '89.990' and [o['difference_tnd'] for o in best['offers']] == ['0.000','9.010']
    assert [o['product_url'] for o in best['offers']] == [a,b] and '16000 dpi' in result['reply'].lower()
    mark_verified([best], {b})
    assert not best['verified_this_turn'] and best['offers'][1]['verified_this_turn']
    again = await shopping.reply('user', 'Only show products that are in stock', result['conversation_id'])
    assert [p['id'] for p in again['products']] == [best['id'], other['id']]
    cheaper = await shopping.reply('user', 'Show me something cheaper', result['conversation_id'])
    assert [p['id'] for p in cheaper['products']] == [other['id']]


async def test_model_and_spec_constraints():
    listing('Apple iPhone 15', 90)
    listing('Coque iPhone 16', 5)
    listing('Apple iPhone 16 Pro occasion', 100)
    listing('Apple iPhone 16 128 Go', 120)
    result = await shopping.reply('phone', 'Where can I find the cheapest iPhone 16?')
    assert [p['name'] for p in result['products']] == ['Apple iPhone 16 128 Go']
    for gpu, ram in [('RTX4060', 16), ('RTX 4060', 8), ('RTX 4050', 32)]:
        name = f'PC Portable {gpu} {ram} Go'
        add(name, 'laptop', 2999)
        with Session.begin() as db:
            db.scalar(select(Product).where(Product.name == name)).ram_gb = ram
    result = await shopping.reply('laptop', 'Find me a laptop with 16 GB RAM and an RTX 4060.')
    assert [p['name'] for p in result['products']] == ['PC Portable RTX4060 16 Go']


async def test_named_models_and_missing_budget():
    add('Souris Gamer Logitech G305', 'discovered', 150)
    add('Souris Gamer Razer Viper', 'discovered', 140)
    result = await shopping.reply('pair', 'Compare the Logitech G305 and Razer Viper')
    assert len(result['products']) == 2
    result = await shopping.reply('new', 'Find me the best wireless gaming mouse for my budget.')
    assert 'maximum budget' in result['reply']
    result = await shopping.reply('new', 'under 300 TND', result['conversation_id'])
    assert result['products'] == []


def test_index_rebuild_update_and_delete():
    listing('Souris Junior', 20)
    init_db()
    with Session.begin() as db:
        p = db.scalar(select(Product).where(Product.name == 'Souris Junior'))
        p.name = 'Clavier Junior'
    assert not shopping.search({'type_terms':['mice']})
    assert shopping.search({'type_terms':['clavier']})
    with Session.begin() as db:
        p = db.scalar(select(Product).where(Product.name == 'Clavier Junior'))
        for offer in p.offers:
            db.delete(offer)
        db.flush()
        db.delete(p)
    assert not shopping.search({'type_terms':['clavier']})


def test_mpn_grouping_preserves_variants_and_rejects_image_destinations():
    for name, retailer in [('Souris Gamer noire', 'scoop'), ('Gaming Mouse black', 'spacenet'),
                           ('Souris Gamer blanche', 'spacenet'), ('Souris Gamer image', 'scoop')]:
        listing(name, 60, retailer)
    with Session.begin() as db:
        for p in db.scalars(select(Product).where(Product.brand == 'Logitech')):
            p.raw_specifications = {'metadata':{'mpn':'910-005282'}}
            if 'image' in p.name:
                p.offers[0].product_url += '.jpg'
    cards = shopping.search({'type_terms':['mouse']})
    assert sorted(len(p['offers']) for p in cards) == [1, 2]
    assert all(o['product_url'].endswith('.html') for p in cards for o in p['offers'])
