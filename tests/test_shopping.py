from decimal import Decimal
from datetime import timedelta
from fastapi.testclient import TestClient
from backend.database import Session
from backend.main import app
from backend.models import Offer, Product, now
from backend.services.shopping import extract
from backend.services.shopping import say
from backend.services.shopping import explain_empty, search
from backend.services.catalog import listed_graphics, graphics_tier
from tests.test_auth import create_account

def test_currency_and_followup_requirements():
    criteria, issue = extract('Apple AirPods with a maximum budget of $150', {})
    assert criteria == {'category': 'earphones', 'brand': 'apple'}
    assert 'TND' in issue
    criteria, issue = extract('Under 500 TND, preferably Pro', criteria)
    assert issue is None
    assert criteria['budget'] == '500' and criteria['preference'] == 'pro'

def test_french_and_arabic_requirements():
    for text, language in [
        ('Je cherche des AirPods Apple à moins de 500 TND', 'fr'),
        ('ابحث عن سماعات آبل إيربودز بأقل من ٥٠٠ دينار', 'ar'),
    ]:
        criteria, issue = extract(text, {}, language)
        assert issue is None
        assert criteria == {'category':'earphones','brand':'apple','budget':'500'}
    criteria, issue = extract('ميزانيتي 150 دولار', {'category':'earphones'}, 'ar')
    assert 'دينار' in issue
    assert say('fr','cart_total',total='420.000').startswith('Le total')

def test_arabic_budget_and_new_pc_request():
    original, issue = extract('أبحث عن لابتوب للدراسة من Lenovo، بميزانية لا تتجاوز 2500 دينار تونسي', {}, 'ar')
    assert issue is None
    assert original == {'category':'laptop', 'brand':'lenovo', 'budget':'2500'}
    fresh, issue = extract('i want a pc that has a graphics card', original)
    assert issue is None
    assert fresh == {'dedicated_gpu':True}
    refined, _ = extract('with a dedicated graphics card', original)
    assert refined['brand'] == 'lenovo' and refined['budget'] == '2500' and refined['dedicated_gpu']

def test_keyboard_search_resets_earphones_and_parses_rgb_budget():
    criteria, issue = extract('i want keyboards that has RGB in them and less then 400TND', {'category':'earphones','brand':'apple','budget':'500'})
    assert issue is None
    assert criteria == {'category':'keyboard','budget':'400','preference':'rgb'}
    criteria, _ = extract('no i want ipads', criteria)
    assert criteria == {'category':'tablet','brand':'apple'}
    criteria, _ = extract('what about samsung', criteria)
    assert criteria == {'category':'tablet','brand':'samsung'}

def test_gpu_requires_explicit_discrete_model_and_empty_diagnostics():
    assert listed_graphics(Product(name='Lenovo RTX 4050', raw_specifications={})) == 'RTX 4050'
    assert listed_graphics(Product(name='Lenovo Intel Iris Xe', raw_specifications={})) is None
    with Session.begin() as db:
        p = Product(signature='diagnostic-laptop', category='laptop', brand='Lenovo', name='Lenovo study laptop', raw_specifications={'attributes':{'Graphiques':'Intel Iris Xe'}})
        db.add(p); db.flush()
        db.add(Offer(product_id=p.id, supplier_name='spacenet', supplier_product_id='diagnostic-laptop', product_url='https://spacenet.tn/pc-portable/diagnostic.html', price_tnd=Decimal('2600'), stock_status='in_stock'))
    assert not search({'category':'laptop','dedicated_gpu':True})
    assert '2600' in explain_empty({'category':'laptop','brand':'lenovo','budget':'2500'}, 'en')
    assert 'dedicated GPU' in explain_empty({'category':'laptop','dedicated_gpu':True}, 'en')

def test_catalog_search_accepts_product_terms_without_category():
    with Session.begin() as db:
        p=Product(signature='free-search',category='laptop',brand='MSI',name='MSI Katana RTX 4060',
                  ram_gb=16,storage_gb=512,raw_specifications={})
        db.add(p);db.flush()
        db.add(Offer(product_id=p.id,supplier_name='spacenet',supplier_product_id='free-search',
                     product_url='https://spacenet.tn/laptop/katana.html',price_tnd=Decimal('3500'),stock_status='in_stock'))
    assert len(search({'search_terms':['katana'],'min_ram_gb':16,'budget':'4000'}))==1
    assert search({'search_terms':['katana'],'min_ram_gb':32})==[]
    assert search({'search_terms':['chairs']})==[]

def test_strongest_gpu_resets_old_filters_and_ranks_saved_laptops():
    criteria, issue=extract('I want to know the laptop that has the highest GPU power',
                            {'category':'laptop','brand':'lenovo','budget':'2500','preference':'rgb'})
    assert issue is None
    assert criteria == {'category':'laptop','gpu_priority':True,'dedicated_gpu':True}
    assert graphics_tier('RTX 5080') > graphics_tier('RTX 4070') > graphics_tier('RTX 3050')
    with Session.begin() as db:
        for gpu,price in [('RTX 3050','2500'),('RTX 5080','8499'),('RTX 4070','4800')]:
            p=Product(signature='power-'+gpu,category='laptop',brand='MSI',name='PC Portable Gamer '+gpu,raw_specifications={})
            db.add(p);db.flush()
            db.add(Offer(product_id=p.id,supplier_name='spacenet',supplier_product_id='power-'+gpu,
                         product_url=f'https://spacenet.tn/pc-portable-gamer/{p.id}.html',
                         price_tnd=Decimal(price),stock_status='in_stock'))
    results=search(criteria)
    assert [p['graphics'] for p in results] == ['RTX 5080','RTX 4070','RTX 3050']

def test_gaming_listing_refresh_uses_permitted_categories(monkeypatch):
    from backend.services import shopping
    from scraping import refresh_catalog, spacenet, scoop
    calls=[]
    monkeypatch.setattr(shopping,'_gaming_refresh_at',{})
    monkeypatch.setattr(shopping.settings, 'spacenet_gaming_listing_limit', 72)
    monkeypatch.setattr(refresh_catalog,'refresh',lambda *args,**kwargs: calls.append((args,kwargs)) or [dict(products_extracted=1)])
    assert spacenet.GAMING_CATEGORIES['laptop'].endswith('204-pc-portable-gamer-tunisie')
    assert scoop.GAMING_CATEGORIES['laptop'].endswith('192-pc-portable-gamer')
    assert len(shopping.refresh_gaming())==2
    assert calls == [(('spacenet','laptop',72),{'listing':'gaming'}),
                     (('scoop','laptop',40),{'listing':'gaming'})]

def test_live_refresh_checks_two_tunisian_sources(monkeypatch):
    from backend.services.shopping import refresh_sources
    from scraping import refresh_catalog
    calls = []
    def fake_refresh(retailer, category, limit):
        calls.append((retailer, category, limit))
        return [dict(products_extracted=1, errors=[])]
    monkeypatch.setattr(refresh_catalog, 'refresh', fake_refresh)
    result = refresh_sources('laptop')
    assert sorted(calls) == sorted([('spacenet', 'laptop', 5), ('scoop', 'laptop', 5)])
    assert [item['status'] for item in result] == ['refreshed', 'refreshed']

def test_targeted_lenovo_refresh_and_wrong_category(monkeypatch):
    from backend.services import shopping
    from scraping import refresh_catalog
    from scraping.normalize import category_name_valid
    calls = []
    monkeypatch.setattr(shopping, '_brand_refresh_at', {})
    monkeypatch.setattr(refresh_catalog, 'refresh', lambda *args, **kwargs: calls.append((args, kwargs)) or [dict(products_extracted=2)])
    assert shopping.refresh_brand('laptop','lenovo')[0]['status'] == 'refreshed'
    assert calls == [(('spacenet','laptop',5),{'brand':'lenovo'})]
    assert shopping.refresh_brand('laptop','lenovo') == []
    assert not category_name_valid('laptop','Refroidisseur De Processeur MSI')
    assert category_name_valid('laptop','PC Portable Lenovo IdeaPad')
    assert category_name_valid('keyboard','Clavier Gamer RGB')
    assert not category_name_valid('keyboard','Repose Poignet RGB pour Clavier')

def test_tablet_brand_categories_and_keyboard_sources(monkeypatch):
    from backend.services import shopping
    from scraping import refresh_catalog, spacenet, scoop
    assert spacenet.BRAND_CATEGORIES[('tablet','apple')].endswith('215-ipad')
    assert spacenet.BRAND_CATEGORIES[('tablet','samsung')].endswith('897-tablette-samsung')
    assert scoop.CATEGORIES['keyboard'].endswith('73-clavier')
    calls=[]
    monkeypatch.setattr(shopping, '_brand_refresh_at', {})
    monkeypatch.setattr(refresh_catalog, 'refresh', lambda *args, **kwargs: calls.append((args,kwargs)) or [dict(products_extracted=2)])
    assert shopping.refresh_brand('tablet','apple')[0]['status'] == 'refreshed'
    assert shopping.refresh_brand('tablet','samsung')[0]['status'] == 'refreshed'
    assert [c[1]['brand'] for c in calls] == ['apple','samsung']

def test_conversation_cart_and_session_isolation(monkeypatch):
    from backend.services import shopping
    monkeypatch.setattr(shopping.settings, 'openai_api_key', '')
    monkeypatch.setattr(shopping, 'refresh_sources', lambda category: [dict(retailer='spacenet', status='recent')])
    with Session.begin() as db:
        p = Product(signature='airpods-real-fixture', category='earphones', brand='Apple', model='AirPods 3', name='Apple AirPods 3', raw_specifications={'description':'Wireless earphones'})
        db.add(p); db.flush()
        db.add(Offer(product_id=p.id, supplier_name='spacenet', supplier_product_id='fixture-1',
                     product_url='https://spacenet.tn/earbuds-tunisie/fixture-1.html', price_tnd=Decimal('420'), stock_status='in_stock'))
    with TestClient(app) as client:
        first = {'Authorization':'Bearer ' + create_account(client,'first-shopping@example.com')[1]['token']}
        other = {'Authorization':'Bearer ' + create_account(client,'other-shopping@example.com')[1]['token']}
        found = client.post('/shopping/chat', headers=first, json={'message':'Find Apple AirPods under 500 TND'}).json()
        assert len(found['products']) == 1
        cid = found['conversation_id']; product_id = found['products'][0]['id']
        followup = client.post('/shopping/chat', headers=first, json={'conversation_id':cid,'message':'Add the cheapest AirPods to my cart'}).json()
        assert followup['cart']['total_tnd'] == '420.000'
        assert client.get('/shopping/cart', headers=other).json()['items'] == []
        assert client.get(f'/shopping/conversations/{cid}', headers=other).status_code == 404
        updated = client.put('/shopping/cart', headers=first, json={'offer_id':product_id,'quantity':2})
        assert updated.status_code == 200, updated.text
        assert updated.json()['total_tnd'] == '840.000'
        assert client.post('/shopping/chat', headers=first, json={'conversation_id':cid,'message':'Show me the total price of my cart'}).json()['cart']['total_tnd'] == '840.000'
        assert client.post('/shopping/chat', headers=first, json={'conversation_id':cid,'message':'Remove the first product'}).json()['cart']['items'] == []
        french = client.post('/shopping/chat', headers=first, json={'message':'Je cherche des AirPods Apple à moins de 500 TND','language':'fr'}).json()
        assert len(french['products']) == 1 and french['reply'].startswith('J’ai trouvé')
        arabic = client.post('/shopping/chat', headers=first, json={'message':'ابحث عن سماعات آبل إيربودز بأقل من ٥٠٠ دينار','language':'ar'}).json()
        assert len(arabic['products']) == 1 and arabic['reply'].startswith('وجدت')
        added = client.post('/shopping/chat', headers=first, json={'conversation_id':arabic['conversation_id'],'message':'أضف الأرخص إلى السلة','language':'ar'}).json()
        assert added['cart']['total_tnd'] == '420.000' and 'السلة' in added['reply']
        pc = client.post('/shopping/chat', headers=first, json={'conversation_id':arabic['conversation_id'],'message':'i want a pc that has a graphics card','language':'en'}).json()
        assert 'laptop or desktop' in pc['reply'] and pc['products'] == []

def test_chat_switches_earbuds_to_rgb_keyboards_and_tablet_brands(monkeypatch):
    from backend.services import shopping
    monkeypatch.setattr(shopping.settings, 'openai_api_key', '')
    monkeypatch.setattr(shopping, 'refresh_sources', lambda _: [dict(retailer='spacenet',status='recent')])
    monkeypatch.setattr(shopping, 'refresh_brand', lambda *args: [])
    with Session.begin() as db:
        for category,brand,name,price in [
            ('earphones','Promate','Ecouteur PROMATE ATOM Bordeaux',35),
            ('keyboard','Redragon','Clavier Gamer Redragon RGB',199),
            ('keyboard','Generic','Clavier Sans Lumière',50),
            ('tablet','Apple','Apple iPad 11 128Go',1599),
            ('tablet','Samsung','Tablette Samsung Galaxy Tab A11',469),
        ]:
            p=Product(signature='switch-'+name,category=category,brand=brand,name=name,raw_specifications={})
            db.add(p);db.flush()
            db.add(Offer(product_id=p.id,supplier_name='spacenet',supplier_product_id='switch-'+name,
                         product_url=f'https://spacenet.tn/product/{p.id}.html',price_tnd=Decimal(price),stock_status='in_stock'))
    with TestClient(app) as client:
        auth={'Authorization':'Bearer '+create_account(client,'switch-shopping@example.com')[1]['token']}
        first=client.post('/shopping/chat',headers=auth,json={'message':'find earbuds'}).json()
        cid=first['conversation_id']
        keyboard=client.post('/shopping/chat',headers=auth,json={'conversation_id':cid,'message':'i want keyboards that has RGB in them and less then 400TND'}).json()
        assert len(keyboard['products'])==1 and keyboard['products'][0]['category']=='keyboard'
        ipad=client.post('/shopping/chat',headers=auth,json={'conversation_id':cid,'message':'no i want ipads'}).json()
        assert len(ipad['products'])==1 and ipad['products'][0]['brand']=='Apple'
        samsung=client.post('/shopping/chat',headers=auth,json={'conversation_id':cid,'message':'what about samsung'}).json()
        assert len(samsung['products'])==1 and samsung['products'][0]['brand']=='Samsung'

def test_chat_does_not_scrape_and_uses_saved_offers(monkeypatch):
    from backend.services import shopping
    monkeypatch.setattr(shopping.settings, 'openai_api_key', '')
    monkeypatch.setattr(shopping, 'refresh_sources', lambda *_: (_ for _ in ()).throw(AssertionError('live scrape in chat')))
    monkeypatch.setattr(shopping, 'refresh_brand', lambda *_: (_ for _ in ()).throw(AssertionError('live scrape in chat')))
    with Session.begin() as db:
        p=Product(signature='cached-tablet',category='tablet',brand='Samsung',name='Samsung Galaxy Tab',raw_specifications={})
        db.add(p);db.flush()
        db.add(Offer(product_id=p.id,supplier_name='spacenet',supplier_product_id='cached-tablet',
                     product_url='https://spacenet.tn/tablette/samsung.html',price_tnd=Decimal('469'),
                     stock_status='in_stock',scraped_at=now()-timedelta(hours=25)))
    with TestClient(app) as client:
        headers={'Authorization':'Bearer '+create_account(client,'cached-shopping@example.com')[1]['token']}
        result=client.post('/shopping/chat',headers=headers,json={'message':'Samsung tablet under 500 TND'}).json()
        assert len(result['products'])==1 and 'saved offer' in result['reply']
        assert client.get('/shopping/catalog-status',headers=headers).status_code==200
    with Session.begin() as db:
        o=db.query(Offer).filter(Offer.supplier_product_id=='cached-tablet').one()
        o.scraped_at=now()-timedelta(hours=49)
    assert search({'category':'tablet','brand':'samsung'}) == []

def test_chat_recommends_strongest_listed_gpu_without_old_budget(monkeypatch):
    from backend.services import shopping
    monkeypatch.setattr(shopping.settings,'openai_api_key','')
    with Session.begin() as db:
        for name,brand,gpu,price in [('Lenovo RTX 3050','Lenovo','RTX 3050',2400),
                                     ('MSI RTX 5080','MSI','RTX 5080',8000)]:
            p=Product(signature='chat-'+gpu,category='laptop',brand=brand,name=name,raw_specifications={})
            db.add(p);db.flush()
            db.add(Offer(product_id=p.id,supplier_name='spacenet',supplier_product_id='chat-'+gpu,
                         product_url=f'https://spacenet.tn/laptop/{p.id}.html',price_tnd=Decimal(price),stock_status='in_stock'))
    with TestClient(app) as client:
        headers={'Authorization':'Bearer '+create_account(client,'gpu-chat@example.com')[1]['token']}
        first=client.post('/shopping/chat',headers=headers,json={'message':'Lenovo laptop under 2500 TND'}).json()
        ranked=client.post('/shopping/chat',headers=headers,json={'conversation_id':first['conversation_id'],
            'message':'I want to know the laptop that has the highest GPU power'}).json()
        assert ranked['products'][0]['graphics']=='RTX 5080'
        assert 'model tier' in ranked['reply'] and 'RTX 5080' in ranked['reply']
        by_name=client.post('/shopping/chat',headers=headers,json={'message':'show me MSI'}).json()
        assert len(by_name['products'])==1 and by_name['products'][0]['brand']=='MSI'


def test_chat_switches_to_headsets_and_open_catalog_titles(monkeypatch):
    from backend.services import shopping

    # Exercise both a working fast path and an unavailable model on unknown nouns.
    monkeypatch.setattr(shopping.settings, 'openai_api_key', 'test')
    calls = []
    async def unavailable(message, language, previous, history, shown):
        calls.append((message, previous))
        raise TimeoutError()
    monkeypatch.setattr(shopping.agent, 'choose_tool', unavailable)
    with Session.begin() as db:
        for category, brand, name, price in [
            ('laptop', 'MSI', 'MSI Gaming RTX 4050', 2499),
            ('headset', 'Logitech', 'Micro Casque Logitech H390', 99),
            ('headset', 'JBL', 'Casque JBL', 65),
            ('accessories', 'Logitech', 'Souris sans fil Logitech M185', 50),
        ]:
            p = Product(signature=name, category=category, brand=brand, name=name,
                        raw_specifications={'description': 'Compatible avec imprimante et casque Logitech'})
            db.add(p)
            db.flush()
            db.add(Offer(product_id=p.id, supplier_name='scoop', supplier_product_id=name,
                         product_url=f'https://www.scoop.com.tn/test/{p.id}.html',
                         price_tnd=Decimal(price), stock_status='in_stock'))
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer ' + create_account(client, 'free-talk@example.com')[1]['token']}
        def ask(message, cid=None):
            result = client.post('/shopping/chat', headers=headers,
                                 json={'message': message, 'conversation_id': cid})
            assert result.status_code == 200, result.text
            return result.json()
        first = ask('give me a laptop with a good GPU')
        assert first['products'][0]['brand'] == 'MSI'
        cid = first['conversation_id']
        message = 'now show me a good logitech headset'
        headsets = ask(message, cid)
        assert [p['name'] for p in headsets['products']] == ['Micro Casque Logitech H390']
        assert all(prompt != message for prompt, _ in calls)
        assert ask('under 75 TND', cid)['products'] == []
        assert ask('now show me a wireless mouse', cid)['products'][0]['name'] == 'Souris sans fil Logitech M185'
        assert ask('what about printers?', cid)['products'] == []
        assert any(prompt == 'what about printers?' for prompt, _ in calls)
        assert ask('add the cheapest to my cart', cid)['cart']['items'] == []


def test_discovery_uses_only_matching_public_category_links():
    from scrapling import Selector
    from scraping.refresh_catalog import category_links
    page = Selector('<a href="/42-souris">Souris</a><a href="/50-claviers">Claviers</a>'
                    '<a href="/200-souris-prix?sort=price">souris</a>'
                    '<a href="/88-imprimante">imprimante</a>',
                    url='https://www.scoop.com.tn/293-peripheriques-et-accessoires')
    assert category_links(page, [['mouse'], ['printer'], ['mouse']], lambda url: 'imprimante' not in url) == [
        'https://www.scoop.com.tn/42-souris']


async def test_free_search_keeps_product_identity_across_languages_and_model_errors(monkeypatch):
    from backend.services import shopping
    previous = {'category': 'laptop', 'brand': 'msi', 'gpu_priority': True}
    async def wrong_category(*args):
        return dict(previous)
    monkeypatch.setattr(shopping.settings, 'openai_api_key', 'test')
    monkeypatch.setattr(shopping.agent, 'choose_tool', wrong_category)
    for text, language in [('Montre-moi un bon casque Logitech', 'fr'),
                           ('اعرض لي سماعة رأس من لوجيتك', 'ar')]:
        criteria, _ = await shopping.interpret_turn(text, previous, language)
        assert criteria == {'category': 'headset', 'brand': 'logitech'}
    for message, terms in [('now show me a webcam', ['webcam']),
                           ('a mouse for my laptop', ['mouse']),
                           ('show me a laptop stand', ['laptop', 'stand'])]:
        criteria, _ = await shopping.interpret_turn(message, previous, 'en')
        assert criteria == {'title_terms': terms}
