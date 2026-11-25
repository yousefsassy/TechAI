"""Regressions for the request loop, turn replay and bounded progress."""
import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select

from backend.database import Session
from backend.main import app, shopping_user
from backend.models import ShoppingMessage
from backend.services import shopping
from backend.services.shopping_transactions import ShoppingConflict
from scraping.lookup import join


async def test_idempotent_turn_replay_is_owner_scoped_and_payload_bound():
    payload = ('mice under 400 TND',)
    first, replay = await asyncio.gather(*(
        shopping.reply('first', *payload, turn_id='same') for _ in range(2)
    ))
    assert first['conversation_id'] == replay['conversation_id']
    assert first['reply'] == replay['reply']
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(ShoppingMessage)) == 2

    with pytest.raises(ShoppingConflict):
        await shopping.reply('first', 'Show tablets', turn_id='same')
    other = await shopping.reply('other', *payload, turn_id='same')
    assert other['conversation_id'] != first['conversation_id']


async def test_same_conversation_turns_take_latest_context_and_page_without_gaps():
    first = await shopping.reply('owner', 'mice under 400 TND')
    cid = first['conversation_id']
    replies = await asyncio.gather(*(
        shopping.reply('owner', 'Which one is cheapest?', cid, turn_id=str(n)) for n in range(4)
    ))
    assert {result['conversation_id'] for result in replies} == {cid}

    messages = []
    cursor = None
    while True:
        page = shopping.conversation('owner', cid, limit=4, before_message_id=cursor)
        messages[:0] = page['messages']
        cursor = page['next_before_message_id']
        if not cursor:
            break
    assert len(messages) == 10
    assert [m['id'] for m in messages] == sorted({m['id'] for m in messages})
    with pytest.raises(ValueError):
        shopping.conversation('other', cid, limit=4)


async def test_lookup_replays_committed_progress_and_shares_final_after_disconnect():
    first_preview = asyncio.Event()
    can_finish = asyncio.Event()
    calls = 0
    seen = []
    reports = [{'retailer': 'scoop', 'status': 'checked', 'checked': 1, 'urls': ['product']}]

    async def load(operation):
        nonlocal calls
        calls += 1
        operation.publish('product')
        first_preview.set()
        await can_finish.wait()
        assert not operation.cancelled.is_set()
        return reports

    lead = asyncio.create_task(join({'type': 'mouse'}, False, load))
    await first_preview.wait()
    follower = asyncio.create_task(join({'type': 'mouse'}, False, load, seen.append))
    await asyncio.sleep(0)
    lead.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lead
    can_finish.set()
    assert await follower == reports
    assert calls == 1 and seen == ['product']


async def test_compact_stream_preserves_final_and_replaces_representative(monkeypatch):
    base = {'id': 1, 'group_id': 'mouse', 'price_tnd': '150.000', 'name': 'Souris'}
    replacement = dict(base, id=2, price_tnd='140.000')
    result = {'conversation_id': 'new', 'reply': 'Deux offres', 'products': [replacement], 'cart': {'items': []}}

    async def fake_reply(*_, progress, **__):
        progress([base])
        await asyncio.sleep(0.02)
        progress([replacement])
        return result

    monkeypatch.setattr(shopping, 'reply', fake_reply)
    monkeypatch.setitem(app.dependency_overrides, shopping_user, lambda: {'id': 'owner'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        compact = await client.post('/shopping/chat/stream?stream_version=2', json={'message': 'souris'})
        legacy = await client.post('/shopping/chat/stream', json={'message': 'souris'})
        invalid = await client.post('/shopping/chat/stream?stream_version=2',
                                    json={'message': 'souris', 'conversation_id': 'missing'})
    events = [json.loads(line) for line in compact.text.splitlines()]
    assert [event['sequence'] for event in events] == [1, 2, 3]
    assert events[0]['upserts'] == [base]
    assert events[1]['upserts'] == [replacement]
    assert events[1]['order'] == ['mouse'] and events[1]['removed_ids'] == []
    assert events[-1]['data'] == result
    assert json.loads(legacy.text.splitlines()[-1]) == {'type': 'result', **result}
    assert invalid.status_code == 404


async def test_writer_lock_does_not_block_health_probe(monkeypatch):
    import threading
    from backend.database import engine

    write_started = threading.Event()

    def hold_writer():
        with engine.begin() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            write_started.set()
            threading.Event().wait(0.35)

    holder = asyncio.create_task(asyncio.to_thread(hold_writer))
    assert await asyncio.to_thread(write_started.wait, 1)
    monkeypatch.setitem(app.dependency_overrides, shopping_user, lambda: {'id': 'owner'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        turn = asyncio.create_task(client.post('/shopping/chat', json={'message': 'show mice'}))
        await asyncio.sleep(0.02)
        response = await client.get('/health')
        assert response.status_code == 200
        assert not holder.done() and not turn.done()
        assert (await turn).status_code == 200
    await holder
