"""Repeatable planner routing and wire-size measurements with no external calls.

Run from the repository with python -m eval.planner_benchmark. To compare an
older source tree, run the same file from that tree with PYTHONPATH set to it.
"""
import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from time import perf_counter
from unittest.mock import patch

import httpx
from langchain_openai import ChatOpenAI

from backend.config import settings
from backend.services import llm, shopping, shopping_agent as agent


@dataclass(frozen=True)
class Case:
    message: str
    language: str
    expected: dict
    provider: object
    previous: dict = field(default_factory=dict)
    contextual: bool = False


def search(category, term, **criteria):
    return agent.SearchProducts(mode='new', category=category, type_terms=[term], **criteria)


def cases():
    headset = {'category': 'headset', 'brand': 'logitech'}
    mouse = {'type_terms': ['souris'], 'budget': '400', 'exclusive_budget': True}
    previous = {'category': 'keyboard', 'budget': '500'}
    french = {'category': 'headset', 'preference': 'bluetooth', 'budget': '200', 'exclusive_budget': True}
    arabic = {'category': 'laptop', 'budget': '2500', 'exclusive_budget': True}
    return [
        Case('Find laptops under 3000 TND', 'en', {'category': 'laptop', 'budget': '3000', 'exclusive_budget': True},
             search('laptop', 'pc portable', budget=3000)),
        Case('Montre-moi un casque Bluetooth sous 200 TND', 'fr', french,
             search('headset', 'casque', budget=200, search_terms=['bluetooth'])),
        Case('Logitech headsets', 'en', headset, search('headset', 'casque', brand='logitech'), previous),
        Case('Logitech wireless headsets', 'en', {**headset, 'preference': 'wireless'},
             search('headset', 'casque', brand='logitech', search_terms=['wireless'])),
        Case('اعرض لي حاسوب محمول تحت ٢٥٠٠ دينار', 'ar', arabic,
             search('laptop', 'pc portable', budget=2500)),
        Case('Show me RGB keyboards under 300 TND', 'en',
             {'category': 'keyboard', 'budget': '300', 'preference': 'rgb', 'exclusive_budget': True},
             search('keyboard', 'clavier', budget=300, search_terms=['rgb'])),
        Case('Show me Samsung tablets', 'en', {'category': 'tablet', 'brand': 'samsung'},
             search('tablet', 'tablette', brand='samsung')),
        Case('Show me gaming mice under 400 TND', 'en', mouse, None),
        Case('Show me some under 300 TND', 'en', {'category': 'keyboard', 'budget': '300', 'exclusive_budget': True},
             None, previous),
        Case('Which one is the cheapest?', 'en', {'metric': 'price'}, agent.CompareShown(metric='price'), previous, True),
        Case('Which one is the least expensive?', 'en', {'metric': 'price'},
             agent.CompareShown(metric='price'), previous, True),
        Case('Quel est le moins cher ?', 'fr', {'metric': 'price'},
             agent.CompareShown(metric='price'), previous, True),
        Case('What is the total key travel of a membrane keyboard?', 'en', {'action': 'general'},
             agent.GeneralAnswer(answer='Key travel depends on the keyboard design.')),
        Case('Find a coffee machine', 'en', {'type_terms': ['machine à café']},
             search(None, 'machine à café')),
        Case('What do you know about the second item?', 'en', {'metric': 'features', 'positions': [2]},
             agent.CompareShown(metric='features', positions=[2]), previous, True),
    ]


def labels(action, criteria, issue):
    if issue:
        return {'issue': True}
    if isinstance(action, agent.CompareShown):
        return {'metric': action.metric, 'positions': action.positions}
    if isinstance(action, agent.GeneralAnswer):
        return {'action': 'general'}
    output = dict(criteria)
    if not output.get('preference') and len(output.get('search_terms', [])) == 1:
        output['preference'] = output['search_terms'][0]
    return output


async def route_sample():
    samples = []
    rows = cases()
    by_prompt = {item.message: item for item in rows}
    calls = Counter()

    async def simulated_provider(message, *args):
        calls[message] += 1
        fixture = by_prompt[message].provider
        if fixture is None:
            raise AssertionError(f'unexpected provider call for {message}')
        return fixture

    shown = [{'id': 40, 'name': 'A', 'price_tnd': '400.000'}, {'id': 16, 'name': 'B', 'price_tnd': '250.000'}]
    failed = []
    with patch.object(agent, 'choose_tool', simulated_provider), patch.object(settings, 'llm_provider', 'openai'),          patch.object(settings, 'openai_api_key', 'benchmark'):
        for _ in range(5):
            start = perf_counter()
            for case in rows:
                action, criteria, issue = await shopping.plan_turn(
                    case.message, case.language, case.previous, [], shown if case.contextual else [],
                )
                result = labels(action, criteria, issue)
                if any(result.get(key) != value for key, value in case.expected.items()):
                    failed.append({'input': case.message, 'expected': case.expected, 'actual': result})
            samples.append(round((perf_counter() - start) * 1000, 3))
    return {'turns': len(rows) * len(samples), 'intent_pass_count': len(rows) * len(samples) - len(failed),
            'model_calls': sum(calls.values()), 'model_calls_by_prompt': dict(calls),
            'suite_ms': samples, 'failures': failed}


async def wire_sample():
    sizes = []
    durations = []

    def respond(request):
        sizes.append(len(request.content))
        tool = {'name': 'CompareShown', 'arguments': '{"metric":"ram","positions":[2,4]}'}
        return httpx.Response(200, json={
            'id': 'benchmark', 'model': 'openrouter/free', 'object': 'chat.completion', 'created': 1,
            'choices': [{'index': 0, 'finish_reason': 'tool_calls',
                         'message': {'role': 'assistant', 'content': None,
                                     'tool_calls': [{'id': 'fixture', 'type': 'function', 'function': tool}]}}],
        })

    shown = [dict(name=f'Gaming PC {index}', description='x' * 500, price_tnd='1700.000',
                  supplier='test', ram_gb=16, graphics='GeForce RTX 4060 Ti', storage_gb=512,
                  specifications={f'raw-{i}': 'example field contents ' * 8 for i in range(18)})
             for index in range(12)]
    history = [('user' if i % 2 else 'assistant', 'A synthetic message with no user data. ' * 20)
               for i in range(12)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        def model(**kwargs):
            return ChatOpenAI(**kwargs, http_async_client=client)

        with patch.object(llm, 'ChatOpenAI', model), patch.object(settings, 'llm_provider', 'openrouter'),              patch.object(settings, 'openrouter_api_key', 'benchmark'):
            for _ in range(30):
                start = perf_counter()
                action = await agent.choose_tool('Compare the second and fourth items', 'en',
                                                 {'category': 'laptop'}, history, shown)
                durations.append((perf_counter() - start) * 1000)
                assert action.positions == [2, 4]
    return {'requests': len(sizes), 'bytes_min': min(sizes), 'bytes_max': max(sizes),
            'mock_median_ms_after_first': round(median(durations[1:]), 3)}


async def main():
    print(json.dumps({'routing': await route_sample(), 'full_history_wire': await wire_sample()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
