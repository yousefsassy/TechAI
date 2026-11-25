import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from scrapling import Selector

from backend.config import settings
from backend.database import Session
from backend.main import app
from backend.models import Offer, Product, now
from backend.services import llm, shopping, shopping_agent as agent
from scraping import discovery
from tests.test_auth import create_account


def add(name, category, price, age=0):
    with Session.begin() as db:
        p = Product(signature=name, category=category, name=name, raw_specifications={})
        db.add(p)
        db.flush()
        offer = Offer(product_id=p.id, supplier_name='spacenet', supplier_product_id=name,
                      product_url=f'https://spacenet.tn/test/{p.id}.html', price_tnd=Decimal(price),
                      stock_status='in_stock', scraped_at=now()-timedelta(hours=age))
        db.add(offer)
        db.flush()
        return offer.product_url


def test_unknown_product_is_discovered_parsed_cached_and_filtered(monkeypatch):
    root = 'https://spacenet.tn'
    target = root + '/machines/881-machine-a-cafe.html'
    accessory = root + '/machines/883-filtre-pour-machine-a-cafe.html'
    other = root + '/laptops/882-msi-gaming.html'
    calls = []
    def get(url, *args):
        calls.append(url)
        if url.endswith('/robots.txt'):
            return SimpleNamespace(status=200, body=b'User-agent: *\nDisallow: /private\n')
        if url.endswith('/'):
            html = '<a href="/78-machines-a-cafe">Machines à café</a>'
        elif url.endswith('/78-machines-a-cafe'):
            html = ''.join(f'<article class="product-miniature"><h2><a href="{u}">{name}</a></h2>En stock</article>'
                           for u, name in [(target, 'Machine à café Nova'), (other, 'MSI Gaming'),
                                           (accessory, 'Filtre pour machine à café'), (root+'/private/1.html','Machine à café')])
        elif url == target:
            data = {'@type':'Product', 'name':'Machine à café Nova', 'brand':{'name':'Nova'}, 'sku':'881',
                    'offers':{'price':'249.900','priceCurrency':'TND','availability':'https://schema.org/InStock'}}
            html = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
        else:
            raise AssertionError(url)
        page = Selector(html, url=url)
        return SimpleNamespace(status=200, body=html.encode(), url=url, css=page.css)
    monkeypatch.setattr(discovery, 'get_page', lambda url, *a, **kw: get(url, *a))
    monkeypatch.setattr(discovery, '_RULES', {})
    monkeypatch.setattr(discovery, 'SOURCES', {'spacenet': discovery.spacenet})
    monkeypatch.setattr(settings, 'shopping_live_discovery', True)
    monkeypatch.setattr(settings, 'openai_api_key', 'fixture')
    async def choose(message, *args):
        return agent.SearchProducts(mode='new', type_terms=['machine à café', 'cafetière'], budget=300)
    monkeypatch.setattr(agent, 'choose_tool', choose)
    add('MSI Gaming', 'laptop', 199)
    with TestClient(app) as client:
        headers={'Authorization':'Bearer '+create_account(client, 'fresh-coffee@example.com')[1]['token']}
        body = {'message':'Now I need a coffee machine under 300 TND'}
        response = client.post('/shopping/chat/stream', headers=headers, json=body)
        events = [json.loads(line) for line in response.iter_lines()]
        assert events[0] == {'type':'progress','products':[]}
        assert events[1]['type'] == 'progress' and events[1]['products'][0]['verified_this_turn']
        assert events[-1]['type'] == 'result'
        first = events[-1]
        assert [(p['name'], p['product_url'], p['price_tnd']) for p in first['products']] == [('Machine à café Nova', target, '249.900')]
        assert 'Checked retailer pages' in first['reply']
        requests = len(calls)
        again = client.post('/shopping/chat', headers=headers, json=body).json()
        assert again['products'][0]['id'] == first['products'][0]['id'] and len(calls) == requests
        assert first['products'][0]['verified_this_turn'] and not again['products'][0]['verified_this_turn']
    assert target in calls and other not in calls and accessory not in calls and not any('/private' in u for u in calls)


async def test_memory_switch_comparison_general_and_cheaper(monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', 'fixture')
    add('PC Portable Gamer MSI RTX 3050', 'laptop', 1500)
    add('PC Portable Gamer ASUS RTX 4060', 'laptop', 2100)
    add('Casque Logitech sans fil', 'headset', 180)
    add('Casque Logitech filaire', 'headset', 70)
    add('Machine à café Nova', 'discovered', 250)
    add('Machine à café Lux', 'discovered', 500)
    actions = {
        'Which one has the best GPU?': agent.CompareShown(metric='gpu'),
        'Now I need a coffee machine': agent.SearchProducts(mode='new', type_terms=['machine à café']),
        "What's the difference between espresso and capsule machines?": agent.GeneralAnswer(
            answer='An espresso machine brews ground coffee under pressure. A capsule machine uses pre-portioned capsules.'
        ),
    }
    async def choose(message, language, previous, history, shown):
        if message.startswith('Which'):
            assert len(shown) == 2 and history[-1][0] == 'assistant'
        assert message in actions, f'Unexpected provider planning for {message}'
        return actions.pop(message)
    monkeypatch.setattr(agent,'choose_tool',choose)
    cid = None
    for message, names in [
        ('Show me a good gaming laptop', ['PC Portable Gamer MSI RTX 3050','PC Portable Gamer ASUS RTX 4060']),
        ('Which one has the best GPU?', ['PC Portable Gamer ASUS RTX 4060','PC Portable Gamer MSI RTX 3050']),
        ('Actually forget laptops. Show me Logitech wireless headsets', ['Casque Logitech sans fil']),
        ('Something cheaper', []),
        ('Now I need a coffee machine', ['Machine à café Nova','Machine à café Lux']),
        ("What's the difference between espresso and capsule machines?", []),
        ('Show me some under 300 TND', ['Machine à café Nova']),
        ('Which one is cheapest?', ['Machine à café Nova'])]:
        reply = await shopping.reply('test-session', message, cid)
        cid = reply['conversation_id']
        assert [p['name'] for p in reply['products']] == names
    assert not actions


async def test_stale_offer_triggers_refresh_failure_without_cards(monkeypatch):
    url = add('Bouilloire inox', 'discovered', 90, age=60)
    monkeypatch.setattr(settings, 'shopping_live_discovery', True)
    calls = []
    async def denied(criteria, candidates, force):
        calls.append([p['product_url'] for p in candidates])
        return [dict(retailer='spacenet', status='http_429', urls=[])]
    monkeypatch.setattr(shopping, 'discover_products', denied)
    result = await shopping.reply('isolated','bouilloire')
    assert result['products'] == [] and calls == [[url]]
    assert 'Could not refresh: spacenet' in result['reply']


async def test_openrouter_free_tool_request(monkeypatch):
    monkeypatch.setattr(settings,'llm_provider','openrouter')
    monkeypatch.setattr(settings,'openrouter_api_key','test-router-key')
    monkeypatch.setattr(settings,'openai_api_key','test-other-key')
    def respond(request):
        assert request.url.host == 'openrouter.ai' and request.headers['authorization'] == 'Bearer test-router-key'
        data = json.loads(request.content)
        assert data['model'] == 'openrouter/free' and len(data['tools']) == 3
        call = {'id':'call-1','type':'function','function':{'name':'SearchProducts','arguments':json.dumps({'mode':'new','type_terms':['bouilloire']})}}
        return httpx.Response(200,json={'id':'chat-1','model':'openrouter/free','object':'chat.completion','created':1,
                             'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':None,'tool_calls':[call]}}]})
    from langchain_openai import ChatOpenAI
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(llm,'ChatOpenAI',lambda **kwargs: ChatOpenAI(**kwargs,http_async_client=client))
        choice = await agent.choose_tool('Find a kettle','en',{},[],[])
        assert choice.type_terms == ['bouilloire']


async def test_explicit_unknown_subject_overrides_repeated_model_category(monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', 'fixture')
    async def stuck(*args):
        return agent.SearchProducts(mode='refine', category='laptop', type_terms=['pc portable'])
    monkeypatch.setattr(agent, 'choose_tool', stuck)
    _, criteria, issue = await shopping.plan_turn('Now I need a coffee machine', 'en',
        {'category':'laptop','dedicated_gpu':True,'budget':'2500'}, [], [])
    assert issue is None and 'cafetière' in criteria['type_terms']
    assert not {'budget','dedicated_gpu','category'}.intersection(criteria)

def test_mouse_and_gpu_filters():
    add('Pack Clavier et Souris Filaire Jedel', 'discovered', 15)
    add('Souris Optique USB Macro Blanc', 'discovered', 7)
    add('PC Portable RTX 3050', 'laptop', 950)
    add('PC Portable RTX 4060', 'laptop', 1200)
    add('PC Portable Intel Core i5', 'laptop', 600)
    add('Casque Gamer avec éclairage ARGB', 'headset', 70)
    add('Casque Gamer Noir', 'headset', 60)
    assert [p['name'] for p in shopping.search({'type_terms':['mouses'],'budget':'400'})] == ['Souris Optique USB Macro Blanc']
    options = shopping.search({'category':'laptop','gpu_priority':True,'search_terms':['gpu']})
    assert [p['graphics'] for p in options] == ['RTX 4060', 'RTX 3050']
    assert [p['name'] for p in shopping.search({'category':'headset','search_terms':['rgb lighting']})] == ['Casque Gamer avec éclairage ARGB']


async def test_mouse_query_grounds_overly_literal_model_translation(monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', 'fixture')
    add('Souris Optique USB', 'discovered', 10)
    add('Pack Clavier et Souris', 'discovered', 20)

    async def choose(*args):
        return agent.SearchProducts(mode='new', type_terms=["souris d'ordinateur"], budget=400)

    monkeypatch.setattr(agent, 'choose_tool', choose)
    result = await shopping.reply('mouse-session', 'I want to see the mouses that are under 400TND')
    assert [product['name'] for product in result['products']] == ['Souris Optique USB']
