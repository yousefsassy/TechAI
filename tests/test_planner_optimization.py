from decimal import Decimal
import json

import httpx
import pytest
from langchain_openai import ChatOpenAI

from backend.config import settings
from backend.database import Session
from backend.models import Offer, Product
from backend.services import llm, shopping, shopping_agent as agent


async def test_local_planner_preserves_explicit_criteria_and_subject_changes(monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', 'fixture')
    calls = []

    async def model(message, *args):
        calls.append(message)
        return agent.GeneralAnswer(answer='A membrane keyboard uses a flexible contact layer.')

    monkeypatch.setattr(agent, 'choose_tool', model)
    previous = {'category': 'laptop', 'brand': 'lenovo', 'budget': '3000'}
    cases = [
        ('Logitech headsets', 'en', {'category': 'headset', 'brand': 'logitech'}, 'budget'),
        ('Montre-moi un casque Bluetooth sous 200 TND', 'fr',
         {'category': 'headset', 'preference': 'bluetooth', 'budget': '200', 'exclusive_budget': True}, 'brand'),
        ('اعرض لي حاسوب محمول تحت ٢٥٠٠ دينار', 'ar',
         {'category': 'laptop', 'budget': '2500', 'exclusive_budget': True}, 'brand'),
        ('Find a laptop for programming up to 2500 TND', 'en',
         {'category': 'laptop', 'budget': '2500', 'sort': 'recommend', 'intended_use': 'programming'}, 'exclusive_budget'),
        ('Find keyboards without RGB', 'en', {'category': 'keyboard', 'forbidden_terms': ['rgb']}, 'preference'),
    ]
    for message, language, expected, removed in cases:
        action, criteria, issue = await shopping.plan_turn(message, language, previous, [], [])
        assert action is None and issue is None
        assert all(criteria[key] == value for key, value in expected.items())
        assert removed not in criteria

    action, criteria, issue = await shopping.plan_turn('Headsets under USD 200', 'en', previous, [], [])
    assert action is None and criteria == previous and 'TND' in issue
    question = 'What is the total key travel of a membrane keyboard?'
    assert not shopping.is_cart_command(question)
    action, _, issue = await shopping.plan_turn(question, 'en', previous, [], [])
    assert isinstance(action, agent.GeneralAnswer) and issue is None
    assert calls == [question]


@pytest.mark.parametrize('message', [
    'Which one is the least expensive?', 'Quel est le moins cher ?', 'أيها الأرخص؟',
])
async def test_price_paraphrases_compare_shown_only(monkeypatch, message):
    monkeypatch.setattr(settings, 'openai_api_key', 'fixture')

    async def unexpected(*args):
        pytest.fail('Price comparison of shown products must run locally')

    monkeypatch.setattr(agent, 'choose_tool', unexpected)
    cards = [{'id': 33, 'name': 'A', 'price_tnd': '100.000'},
             {'id': 12, 'name': 'B', 'price_tnd': '89.000'}]
    action, _, issue = await shopping.plan_turn(message, 'en', {}, [], cards)
    assert issue is None and action.metric == 'price'
    text, compared = agent.comparison(cards, action, 'en')
    assert [card['id'] for card in compared] == [12, 33]
    assert 'B (89.000 TND)' in text




async def test_arabic_gaming_mouse_preserves_strict_budget_and_product_type(monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', 'fixture')

    async def unexpected(*args):
        pytest.fail('Arabic gaming-mouse request should run locally')

    monkeypatch.setattr(agent, 'choose_tool', unexpected)
    names = ('Souris Gamer Logitech G305', 'Souris Gamer Logitech G502', 'Pack Clavier et Souris Gamer')
    with Session.begin() as db:
        for index, (name, price) in enumerate(zip(names, ('399.999', '400.000', '100.000')), start=1):
            product = Product(signature=name, category='accessories', brand='Logitech', name=name, raw_specifications={})
            db.add(product)
            db.flush()
            db.add(Offer(product_id=product.id, supplier_name='spacenet', supplier_product_id=str(index),
                         product_url=f'https://spacenet.tn/mouse/{index}-device.html',
                         price_tnd=Decimal(price), stock_status='in_stock'))

    result = await shopping.reply('test-owner', 'أرني فأرة ألعاب بأقل من ٤٠٠ دينار', language='ar')
    assert [card['name'] for card in result['products']] == [names[0]]



async def test_public_criteria_cache_keeps_request_mutation_private(monkeypatch):
    from backend.services import shopping_intent

    monkeypatch.setattr(shopping_intent, '_PUBLIC_PLANS', shopping_intent.OrderedDict())
    message = 'Find gaming mice under 150 TND'
    _, first, issue = await shopping.plan_turn(message, 'en', {}, [], [])
    assert issue is None
    first['type_terms'].append('private')
    _, second, issue = await shopping.plan_turn(message, 'en', {}, [], [])
    assert issue is None and second['type_terms'] == ['souris']
    assert second['search_terms'] == ['gaming']
    assert len(shopping_intent._PUBLIC_PLANS) == 1

    _, negative, issue = await shopping.plan_turn('Find mice without RGB', 'en', {}, [], [])
    assert issue is None and negative == {'type_terms': ['souris'], 'forbidden_terms': ['rgb']}


async def test_written_card_reference_and_feature_refinement_run_locally(monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', 'fixture')

    async def unexpected(*args):
        pytest.fail('Explicit ordinal comparison and feature refinements must run locally')

    monkeypatch.setattr(agent, 'choose_tool', unexpected)
    for message in ('Compare the second and fourth items', 'Compare le deuxième et le quatrième',
                    'قارن الثاني والرابع'):
        action, _, issue = await shopping.plan_turn(message, 'en', {}, [], [])
        assert issue is None and action.positions == [2, 4]

    previous = {'category': 'keyboard', 'budget': '300', 'preference': 'rgb'}
    _, criteria, issue = await shopping.plan_turn('Without RGB', 'en', previous, [], [])
    assert issue is None and criteria == {'category': 'keyboard', 'budget': '300', 'forbidden_terms': ['rgb']}
    _, switched, issue = await shopping.plan_turn('With RGB', 'en', criteria, [], [])
    assert issue is None and switched == previous


def test_cart_ordinal_uses_displayed_order_and_total_does_not_capture_general_question(monkeypatch):
    mutations = []

    def update(sid, offer_id, quantity, *, db=None):
        mutations.append((sid, offer_id, quantity))
        return {'total_tnd': '15.000'}

    monkeypatch.setattr(shopping, 'update_cart', update)
    for phrase in ('Add the second to cart', 'Ajoute le deuxième au panier', 'أضف الثاني إلى السلة', 'Add product 2 to cart'):
        assert shopping.is_cart_command(phrase)
        assert shopping._cart_command(phrase, 'owner', [81, 57, 204])
    assert mutations == [('owner', 57, 1)] * 4
    assert shopping.is_cart_command('What is my cart total?')
    assert not shopping.is_cart_command('What is the total cost of upgrading laptop memory?')
    assert not shopping.is_cart_command('What types of shopping cart software exist?')


async def test_provider_request_is_bounded_and_excludes_raw_retailer_evidence(monkeypatch):
    monkeypatch.setattr(settings, 'llm_provider', 'openrouter')
    monkeypatch.setattr(settings, 'openrouter_api_key', 'synthetic')
    request_bodies = []

    def respond(request):
        request_bodies.append(request.content)
        action = {'name': 'CompareShown', 'arguments': json.dumps({'metric': 'ram', 'positions': [2, 4]})}
        return httpx.Response(200, json={
            'id': 'fixture', 'model': 'openrouter/free', 'object': 'chat.completion', 'created': 1,
            'choices': [{'index': 0, 'finish_reason': 'tool_calls',
                         'message': {'role': 'assistant', 'content': None,
                                     'tool_calls': [{'id': 'tool-1', 'type': 'function', 'function': action}]}}],
        })

    private = 'retailer raw private marker'
    shown = [{'name': private, 'supplier': private, 'description': private, 'price_tnd': '5.000',
              'specifications': {private: 'x' * 3500}, 'ram_gb': 16, 'graphics': 'GeForce RTX 4060Ti'}
             for _ in range(12)]
    history = [('user', 'سابق ' * 160), ('assistant', private * 250)] * 6
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(llm, 'ChatOpenAI', lambda **kw: ChatOpenAI(**kw, http_async_client=client))
        result = await agent.choose_tool('Compare the second and fourth items', 'en', {'category': 'laptop'}, history, shown)
    assert result.positions == [2, 4]
    assert len(request_bodies) == 1 and len(request_bodies[0]) <= 12 * 1024
    assert private.encode() not in request_bodies[0]
    payload = json.loads(request_bodies[0])
    state = json.loads(payload['messages'][1]['content'])
    assert [row['position'] for row in state['displayed_items']] == list(range(1, 13))
    assert all(row['ram_gb'] == 16 and row['graphics_model'] == 'RTX 4060Ti' for row in state['displayed_items'])
    assert len(state['recent_user_messages']) <= 3
    assert 0 < (payload.get('max_completion_tokens') or payload.get('max_tokens')) <= 768


async def test_unverified_merchant_claim_and_unavailable_positions(monkeypatch):
    monkeypatch.setattr(settings, 'openai_api_key', 'fixture')

    class FakeModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            from types import SimpleNamespace

            return SimpleNamespace(
                tool_calls=[{'name': 'GeneralAnswer', 'args': {'answer': 'This is in stock at 99 TND.'}}],
            )

    monkeypatch.setattr(agent, 'chat_model', lambda **kwargs: FakeModel())
    with pytest.raises(ValueError, match='unverified merchant claim'):
        await agent.choose_tool('What do you know about this?', 'en', {}, [], [])
    text, cards = agent.comparison(
        [{'id': 1, 'name': 'A', 'price_tnd': '100.000'}],
        agent.CompareShown(metric='price', positions=[1, 9]),
        'en',
    )
    assert not cards and 'Which displayed products' in text


async def test_transient_failures_trigger_bounded_provider_cooldown(monkeypatch):
    monkeypatch.setattr(agent, '_GATEWAY_STATE', {})
    monkeypatch.setattr(settings, 'openai_api_key', 'fixture')
    clock = [100.0]
    monkeypatch.setattr(agent, 'monotonic', lambda: clock[0])
    calls = []

    class FailingModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            calls.append(messages)
            raise TimeoutError('fixture transport failure')

    monkeypatch.setattr(agent, 'chat_model', lambda **kwds: FailingModel())
    for _ in range(3):
        with pytest.raises(TimeoutError, match='fixture transport failure'):
            await agent.choose_tool('Explain switch travel', 'en', {}, [], [])
    with pytest.raises(TimeoutError, match='cooling down'):
        await agent.choose_tool('Explain switch travel', 'en', {}, [], [])
    assert len(calls) == 3
    clock[0] += 21
    with pytest.raises(TimeoutError, match='fixture transport failure'):
        await agent.choose_tool('Explain switch travel', 'en', {}, [], [])
    assert len(calls) == 4
