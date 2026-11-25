export async function readChatStream(body, onProgress) {
  if (!body) throw new Error('Search response is empty');
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  let sequence = 0;
  let cards = new Map();

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) throw new Error('Search ended before the final reply');
      buffer += value;
      let newline;
      while ((newline = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === 'error') throw new Error(event.detail ?? event.data?.detail);
        if (event.type === 'result') return event.version === 2 ? event.data : event;
        if (event.type !== 'progress') continue;
        if (event.version !== 2) {
          onProgress(event.products);
          continue;
        }
        if (event.sequence <= sequence) continue;
        sequence = event.sequence;

        // A delta's display order describes the full preview. A coalesced
        // snapshot can supply every displayed card in its upserts.
        const next = new Map(cards);
        for (const id of event.removed_ids) next.delete(id);
        for (const card of event.upserts) next.set(card.group_id, card);
        if (event.order.every(id => next.has(id))) {
          cards = new Map(event.order.map(id => [id, next.get(id)]));
          onProgress([...cards.values()]);
        }
      }
    }
  } finally {
    // The final reply is ready even if the transport is still closing.
    void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
