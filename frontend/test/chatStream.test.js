import assert from 'node:assert/strict';
import test from 'node:test';
import { readChatStream } from '../src/chatStream.js';

function fragmented(events) {
  const bytes = new TextEncoder().encode(events.map(event => JSON.stringify(event) + '\n').join(''));
  return new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      controller.close();
    },
  });
}

test('reads split Unicode, reorders groups, retains offer IDs, and trusts the final frame', async () => {
  const a = { group_id: 'a', id: 52, name: 'سماعة' };
  const b = { group_id: 'b', id: 91, name: 'Écouteurs' };
  const cheaper = { ...a, id: 76 };
  const final = { products: [b], reply: 'تم' };
  const views = [];
  const result = await readChatStream(fragmented([
    { type: 'heartbeat', version: 2 },
    { type: 'progress', version: 2, sequence: 1, upserts: [a, b], removed_ids: [], order: ['a', 'b'] },
    { type: 'progress', version: 2, sequence: 2, upserts: [cheaper], removed_ids: [], order: ['b', 'a'] },
    { type: 'progress', version: 2, sequence: 1, upserts: [a], removed_ids: [], order: ['a'] },
    { type: 'progress', version: 2, sequence: 3, upserts: [], removed_ids: ['a'], order: ['b'] },
    { type: 'result', version: 2, sequence: 4, data: final },
  ]), view => views.push(view));
  assert.deepEqual(views, [[a, b], [b, cheaper], [b]]);
  assert.deepEqual(result, final);
});

test('ignores an unrecoverable delta and accepts a later coalesced snapshot', async () => {
  const product = { group_id: 'a', id: 52 };
  const views = [];
  await readChatStream(fragmented([
    { type: 'progress', version: 2, sequence: 1, upserts: [], removed_ids: [], order: ['a'] },
    { type: 'progress', version: 2, sequence: 3, upserts: [product], removed_ids: [], order: ['a'] },
    { type: 'result', version: 2, sequence: 4, data: { products: [] } },
  ]), view => views.push(view));
  assert.deepEqual(views, [[product]]);
});

test('handles the legacy flat stream, including empty results', async () => {
  const final = { type: 'result', products: [], reply: 'No matches' };
  const views = [];
  const result = await readChatStream(fragmented([{ type: 'progress', products: [] }, final]), view => views.push(view));
  assert.deepEqual(result, final);
  assert.deepEqual(views, [[]]);
});

test('surfaces terminal errors and missing final replies', async () => {
  await assert.rejects(readChatStream(fragmented([{ type: 'error', detail: 'Search failed' }]), () => {}), /Search failed/);
  await assert.rejects(readChatStream(fragmented([]), () => {}), /before the final reply/);
});

test('returns the final reply without waiting for network cancellation', async () => {
  const source = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('{"type":"result","reply":"Done"}\n'));
    },
    cancel() {
      return new Promise(() => {});
    },
  });

  const result = await readChatStream(source, () => {});
  assert.equal(result.reply, 'Done');
});
