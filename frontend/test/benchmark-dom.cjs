/* Run with Node 22+ and jsdom 27 available in NODE_PATH.
 * node frontend/test/benchmark-dom.cjs BASELINE_DIST OPTIMIZED_DIST [trials]
 */
const assert = require('node:assert/strict');
const { readFileSync, readdirSync } = require('node:fs');
const { join } = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');

const [baseline, optimized, trialArgument] = process.argv.slice(2);
const trials = Number(trialArgument ?? 10);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const emptyCart = { items: [], total_tnd: '0' };
const sidebar = [{ id: 'fast', title: 'Fast history' }, { id: 'slow', title: 'Slow history' }];

function bundle(dist) {
  const assets = join(dist, 'assets');
  const name = readdirSync(assets).find(file => file.endsWith('.js'));
  return readFileSync(join(assets, name), 'utf8');
}

function observeStages(window, predicates) {
  const times = Array(predicates.length).fill(null);
  const sample = () => {
    // Use one timestamp for conditions satisfied by the same DOM commit.
    const at = performance.now();
    for (const [index, predicate] of predicates.entries()) {
      if (times[index] === null && predicate()) times[index] = at;
    }
    return times.every(time => time !== null);
  };
  if (sample()) return Promise.resolve(times);
  return new Promise((resolve, reject) => {
    const observer = new window.MutationObserver(() => {
      if (!sample()) return;
      observer.disconnect();
      clearTimeout(timer);
      resolve(times);
    });
    const timer = setTimeout(() => {
      observer.disconnect();
      reject(Error('DOM fixture stage timed out'));
    }, 3000);
    observer.observe(window.document, { childList: true, subtree: true, attributes: true, characterData: true });
  });
}

function card(id) {
  const product = {
    id, group_id: 'group-' + id, name: 'Logitech G305 Édition ' + id,
    supplier: 'mytek', brand: 'Logitech', product_url: 'https://example.invalid/product/' + id,
    image_url: 'https://example.invalid/img/' + id, price_tnd: '149.000', stock_status: 'in_stock',
    scraped_at: '2026-11-23T12:00:00', verified_this_turn: true,
    specifications: { Connectivity: 'Wireless', Color: 'Black', Sensor: 'HERO' },
    description: 'Fixture', rating: 4.6, rating_count: 97,
  };
  return { ...product, offers: [
    { ...product, difference_tnd: '0' },
    { ...product, id: id + 1000, supplier: 'spacenet', difference_tnd: '10' },
  ] };
}

function messages(cards) {
  return Array.from({ length: cards / 12 }, (_, turn) => [
    { id: turn * 2 + 1, role: 'user', content: 'Gaming mice under 400 TND', products: [] },
    { id: turn * 2 + 2, role: 'assistant', content: 'Fixture transcript', products: Array.from({ length: 12 }, (_, j) => card(turn * 12 + j + 1)) },
  ]).flat();
}

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
}

function stream(body, opts, version, mode, firstTurn) {
  const products = messages(12)[1].products;
  const result = { conversation_id: 'fast', reply: 'Fixture final', products, cart: emptyCart };
  const progressFrame = version === '2'
    ? { type: 'progress', version: 2, sequence: 1, upserts: products, removed_ids: [], order: products.map(p => p.group_id) }
    : { type: 'progress', products };
  const resultFrame = version === '2'
    ? { type: 'result', version: 2, sequence: 2, data: result }
    : { type: 'result', ...result };
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      const bytes = encoder.encode(JSON.stringify(progressFrame) + '\n');
      const boundary = bytes.indexOf(0xc3) + 1;
      const timers = [
        setTimeout(() => controller.enqueue(bytes.slice(0, boundary)), 25),
        setTimeout(() => controller.enqueue(bytes.slice(boundary)), 30),
        setTimeout(() => {
          if (mode === 'retry' && firstTurn) return;
          controller.enqueue(encoder.encode(JSON.stringify(resultFrame) + '\n'));
          controller.close();
        }, 65),
      ];
      opts.signal?.addEventListener('abort', () => {
        timers.forEach(clearTimeout);
        controller.error(opts.signal.reason);
      }, { once: true });
    },
  }), { headers: { 'Content-Type': 'application/x-ndjson' } });
}

function fixture(source, { size = 12, mode = 'normal' } = {}) {
  const console = new VirtualConsole();
  const errors = [];
  console.on('jsdomError', e => errors.push(e.message));
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://fixture.example/', runScripts: 'outside-only', virtualConsole: console,
  });
  const { window } = dom;
  const { document } = window;
  const calls = [];
  const rows = messages(size);
  let sidebarReads = 0;
  let quantity = 1;
  let turns = 0;
  window.TextDecoderStream = TextDecoderStream;
  window.Element.prototype.scrollIntoView = () => {};
  window.fetch = async (url, opts = {}) => {
    const { pathname, searchParams } = new URL(url, window.location);
    const endpoint = pathname.replace(/^\/api\//, '/');
    const body = opts.body ? JSON.parse(opts.body) : {};
    calls.push({ endpoint, method: opts.method || 'GET', body, params: Object.fromEntries(searchParams) });
    if (endpoint === '/shopping/auth/me') {
      await delay(15);
      return json({ email: 'fixture@example.invalid' });
    }
    if (endpoint === '/shopping/conversations') {
      sidebarReads += 1;
      await delay(20);
      if (mode === 'boot' || (mode === 'sidebar' && sidebarReads > 1)) return json({ detail: 'Fixture history unavailable' }, 503);
      return json(sidebar);
    }
    if (endpoint === '/shopping/cart') {
      await delay(opts.method === 'PUT' ? 60 : 20);
      if (mode !== 'cart') return json(emptyCart);
      if (opts.method === 'PUT') quantity = body.quantity;
      return json({ items: quantity ? [{ offer: card(1), quantity }] : [], total_tnd: String(149 * quantity) });
    }
    if (endpoint.startsWith('/shopping/conversations/')) {
      if (mode === 'history') {
        const id = endpoint.split('/').at(-1);
        // Deliberately ignore the abort signal, so this also verifies the UI's sequence guard.
        await delay(id === 'slow' ? 80 : 10);
        return json({ messages: [{ id: 1, role: 'assistant', content: id, products: [] }] });
      }
      await delay(0);
      const limit = Number(searchParams.get('limit'));
      if (!limit) return json({ messages: rows });
      const before = Number(searchParams.get('before_message_id')) || Infinity;
      const candidates = rows.filter(row => row.id < before);
      const page = candidates.slice(-limit);
      return json({ messages: page, next_before_message_id: candidates.length > limit ? page[0].id : null });
    }
    if (endpoint === '/shopping/chat/stream') {
      turns += 1;
      return stream(body, opts, searchParams.get('stream_version'), mode, turns === 1);
    }
    throw new Error('Unexpected fixture endpoint ' + endpoint);
  };
  window.sessionStorage.setItem('techstock_auth', 'fixture');
  window.eval(source);

  return {
    window, document, calls,
    one: selector => document.querySelector(selector),
    count: selector => document.querySelectorAll(selector).length,
    at: async predicate => (await observeStages(window, [predicate]))[0],
    close() {
      assert.deepEqual(errors, []);
      window.close();
    },
  };
}

function summarize(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const percentile = p => Number(sorted[Math.ceil(p * sorted.length) - 1].toFixed(2));
  return { n: sorted.length, min: percentile(0.01), p50: percentile(0.5), p95: percentile(0.95), max: percentile(1) };
}

async function measure(source, version) {
  const preview = [];
  const final = [];
  const ready = [];
  const readyAfterFinal = [];
  const history = [];
  let historyCards;
  for (let trial = 0; trial <= trials; trial += 1) {
    const chat = fixture(source);
    await chat.at(() => chat.count('.history-item') === 2);
    const start = performance.now();
    chat.one('.suggestions button').click();
    const [previewAt, finalAt, readyAt] = await observeStages(chat.window, [
      () => chat.count('.typing ~ .product-grid .product-card') === 12,
      () => !!chat.one('.results .product-card'),
      () => !!chat.one('.results .product-card') && !chat.one('.composer textarea').disabled,
    ]);
    if (trial) {
      preview.push(previewAt - start);
      final.push(finalAt - start);
      ready.push(readyAt - start);
      readyAfterFinal.push(readyAt - finalAt);
    }
    chat.close();

    const large = fixture(source, { size: 600 });
    await large.at(() => large.count('.history-item') === 2);
    const clickedAt = performance.now();
    large.one('.history-item').click();
    historyCards = version === 'baseline' ? 600 : 24;
    const openedAt = await large.at(() => large.count('.product-card') === historyCards);
    if (trial) history.push(openedAt - clickedAt);
    large.close();
  }
  return {
    preview_ms: summarize(preview), final_ms: summarize(final), ready_ms: summarize(ready),
    ready_after_final_ms: summarize(readyAfterFinal), history_600_ms: summarize(history), history_visible_cards: historyCards,
  };
}

async function behavior(source, improved) {
  const sidebarFailure = fixture(source, { mode: 'sidebar' });
  await sidebarFailure.at(() => sidebarFailure.count('.history-item') === 2);
  sidebarFailure.one('.suggestions button').click();
  await sidebarFailure.at(() => !sidebarFailure.one('.composer textarea').disabled && !!sidebarFailure.one('.results'));
  await delay(35);
  const retries = sidebarFailure.count('.message.assistant .message-content > p') - 1;
  sidebarFailure.close();

  const boot = fixture(source, { mode: 'boot' });
  await boot.at(() => !!boot.one('.welcome'));
  await delay(35);
  const signedIn = !!boot.one('.welcome');
  boot.close();

  const history = fixture(source, { mode: 'history' });
  await history.at(() => history.count('.history-item') === 2);
  const [fast, slow] = history.document.querySelectorAll('.history-item');
  slow.click();
  fast.click();
  await history.at(() => history.one('.message-content p')?.textContent === 'fast');
  await delay(90);
  const lastHistory = history.one('.message-content p').textContent;
  history.close();

  const cart = fixture(source, { mode: 'cart' });
  await cart.at(() => cart.one('.cart-count')?.textContent === '1');
  cart.one('.cart-trigger').click();
  await cart.at(() => !!cart.one('.quantity button:last-child'));
  const plus = cart.one('.quantity button:last-child');
  plus.click();
  plus.click();
  await cart.at(() => cart.one('.quantity span')?.textContent === (improved ? '3' : '2'));
  const quantities = cart.calls.filter(call => call.method === 'PUT').map(call => call.body.quantity);
  cart.close();

  if (improved) {
    const paged = fixture(source, { size: 120 });
    await paged.at(() => paged.count('.history-item') === 2);
    paged.one('.history-item').click();
    await paged.at(() => paged.count('.product-card') === 24);
    for (const count of [48, 72, 96, 120]) {
      paged.one('.older-messages').click();
      await paged.at(() => paged.count('.product-card') === count);
    }
    assert.equal(paged.one('.older-messages'), null);
    assert.equal(paged.one('.product-card h4').textContent, 'Logitech G305 Édition 1');
    paged.one('.language-switch button[lang="ar"]').click();
    await paged.at(() => paged.document.documentElement.dir === 'rtl');
    paged.close();

    const retry = fixture(source, { mode: 'retry' });
    await retry.at(() => retry.count('.history-item') === 2);
    retry.one('.suggestions button').click();
    await retry.at(() => !!retry.one('[aria-label="Cancel search"]'));
    retry.one('[aria-label="Cancel search"]').click();
    await retry.at(() => !!retry.one('.error-actions button'));
    retry.one('.error-actions button').click();
    await retry.at(() => !!retry.one('.results'));
    const streams = retry.calls.filter(call => call.endpoint === '/shopping/chat/stream');
    assert.equal(streams.length, 2);
    assert.ok(streams[0].body.turn_id);
    assert.equal(streams[0].body.turn_id, streams[1].body.turn_id);
    assert.equal(retry.count('.message.user'), 1);
    retry.close();

    assert.deepEqual({ retries, signedIn, lastHistory, quantities }, { retries: 0, signedIn: true, lastHistory: 'fast', quantities: [2, 3] });
  }
  return { retries, signedIn, lastHistory, quantities };
}

(async () => {
  if (!baseline || !optimized || !Number.isInteger(trials) || trials < 1) {
    throw new Error('Provide baseline and optimized dist directories and an optional positive trial count');
  }
  const before = bundle(baseline);
  const after = bundle(optimized);
  const base = await measure(before, 'baseline');
  const changed = await measure(after, 'optimized');
  for (const [source, improved] of [[before, false], [after, true]]) {
    (improved ? changed : base).behavior = await behavior(source, improved);
  }
  console.log(JSON.stringify({ node: process.version, jsdom: require('jsdom/package.json').version, baseline: base, optimized: changed }, null, 2));
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
