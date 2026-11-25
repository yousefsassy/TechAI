import React, { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowUp, Plus, ShoppingBag, Menu, X, ExternalLink, Trash2, Sparkles, ChevronRight, Clock3 } from './icons.jsx';
import { tr } from './i18n.js';
import { readChatStream } from './chatStream.js';
import './style.css';
import './auth.css';
import './i18n.css';

const API = import.meta.env.VITE_API_URL || '/api';
const STREAM_VERSION = import.meta.env.VITE_SHOPPING_STREAM_VERSION === '1' ? 1 : 2;
const HISTORY_MESSAGES = 4;
const HISTORY_CARDS = 24;
const EMPTY_CART = { items: [], total_tnd: '0' };
const priceFormatter = new Intl.NumberFormat('en-US', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
const price = value => `${priceFormatter.format(Number(value))} TND`;
const dateFormatters = new Map();

function observationDate(value, locale) {
  if (!value) return '';
  const date = new Date(/(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value + 'Z');
  if (Number.isNaN(date.getTime())) return '';
  if (!dateFormatters.has(locale)) {
    dateFormatters.set(locale, new Intl.DateTimeFormat(locale, {
      year: 'numeric', month: 'numeric', day: 'numeric',
      hour: 'numeric', minute: 'numeric', second: 'numeric',
    }));
  }
  return dateFormatters.get(locale).format(date);
}

function apiError(response, data) {
  const error = new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`);
  error.status = response.status;
  return error;
}

async function request(path, token, options = {}) {
  const response = await fetch(`${API}${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...options.headers } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw apiError(response, data);
  return data;
}

async function streamChat(body, token, signal, onProgress) {
  const response = await fetch(`${API}/shopping/chat/stream?stream_version=${STREAM_VERSION}`, {
    method: 'POST', body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    signal: AbortSignal.any([signal, AbortSignal.timeout(240000)]),
  });
  if (!response.ok) throw apiError(response, await response.json().catch(() => ({})));
  return readChatStream(response.body, onProgress);
}

function makeTurnId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // randomUUID is restricted to secure contexts. getRandomValues also works
  // for local development over plain HTTP on a LAN.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function historyWindow(messages) {
  // This also bounds the first render against servers returning unpaged history.
  let first = messages.length;
  let cards = 0;
  while (first > 0 && messages.length - first < HISTORY_MESSAGES) {
    const count = messages[first - 1].products?.length ?? 0;
    if (cards + count > HISTORY_CARDS && first < messages.length) break;
    cards += count;
    first -= 1;
  }
  return { recent: messages.slice(first), older: messages.slice(0, first) };
}

function LanguageSwitch({ language, setLanguage }) {
  return <div className="language-switch" role="group" aria-label="Language / Langue / اللغة">{[['en','EN'],['fr','FR'],['ar','عربي']].map(([code,label]) => <button key={code} type="button" lang={code} className={language===code?'active':''} onClick={() => setLanguage(code)} aria-pressed={language===code}>{label}</button>)}</div>;
}

const ProductCard = memo(function ProductCard({ product, add, busy, language }) {
  const t = (key, vars) => tr(language, key, vars);
  const collectedAt = observationDate(product.scraped_at, language + '-TN');
  const specs = [product.processor, product.graphics, product.ram_gb && `${product.ram_gb} GB RAM`, product.storage_gb && `${product.storage_gb} GB ${product.storage_type || 'storage'}`].filter(Boolean);
  const extra = Object.entries(product.specifications || {}).slice(0, specs.length ? 1 : 3).map(([k,v]) => `${k}: ${v}`);
  return <article className="product-card">
    <div className="product-photo">{product.image_url ? <a href={product.image_url} target="_blank" rel="noopener noreferrer" aria-label={product.name}><img src={product.image_url} alt={product.name} loading="lazy" /></a> : <ShoppingBag size={28} strokeWidth={1.4} />}</div>
    <div className="product-body"><div className="product-topline"><span>{[product.brand, product.supplier].filter(Boolean).join(' · ')}</span><span>{product.stock_status === 'in_stock' ? t('inStock') : product.stock_status}</span></div>
      {collectedAt && <small className="observed-at">{t(product.verified_this_turn ? 'checkedAt' : 'collectedAt', { date: collectedAt })}</small>}
      <h4 title={product.name}>{product.name}</h4><p className="product-spec" title={[...specs,...extra].join(' · ')}>{[...specs,...extra].join(' · ') || product.description || t('specsUnknown')}</p>
      {product.rating != null && <div className="rating">★ {product.rating} {product.rating_count && <span>({product.rating_count})</span>}</div>}
      {product.offers?.length > 1 && <table className="offer-table">
        <caption>{t('comparePrices')}</caption>
        <thead><tr><th>{t('shop')}</th><th>{t('priceLabel')}</th><th>{t('difference')}</th></tr></thead>
        <tbody>{product.offers.map(offer => <tr key={offer.id}>
          <td><a href={offer.product_url} target="_blank" rel="noopener noreferrer">{offer.supplier}</a>
            <small>{offer.stock_status === 'in_stock' ? t('inStock') : offer.stock_status}</small>
            {offer.scraped_at && <small>{t(offer.verified_this_turn ? 'checkedAt' : 'collectedAt', { date: observationDate(offer.scraped_at, language) })}</small>}</td>
          <td>{price(offer.price_tnd)}</td><td>{Number(offer.difference_tnd) ? '+' + price(offer.difference_tnd) : '—'}</td>
        </tr>)}</tbody></table>}
      <div className="product-bottom"><strong>{price(product.price_tnd)}</strong><div className="product-actions"><button className="add-button" onClick={() => add(product.id)} disabled={busy} aria-label={`${t('add')}: ${product.name}`}><Plus size={17}/></button><a href={product.product_url} target="_blank" rel="noopener noreferrer" aria-label={`${t('retailer')}: ${product.name}`}><ExternalLink size={16}/></a></div></div>
    </div>
  </article>;
});

const ChatMessage = memo(function ChatMessage({ message, add, language }) {
  const t = key => tr(language, key);
  const assistant = message.role === 'assistant';
  return <div className={`message ${message.role}`}>
    <div className="message-avatar">{assistant ? <Sparkles size={16}/> : 'Y'}</div>
    <div className="message-content">
      <div className="message-label">{assistant ? 'TechStock' : t('you')}</div>
      <p>{message.content}</p>
      {message.products?.length > 0 && <div className="results">
        <div className="result-caption">{message.products.length} {t('offers')} <span>· {t('observed')}</span></div>
        <div className="product-grid">
          {message.products.map(p => <ProductCard key={p.group_id ?? p.id} product={p} language={language} add={add}/>)}
        </div>
      </div>}
    </div>
  </div>;
});

function AuthView({ onAuthenticated, language, setLanguage, sessionError }) {
  const t = (key, vars) => tr(language, key, vars);
  const [method, setMethod] = useState('email');
  const [mode, setMode] = useState('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [challenge, setChallenge] = useState('');
  const [secret, setSecret] = useState('');
  const [recovery, setRecovery] = useState(null);
  const [pendingAuth, setPendingAuth] = useState(null);
  const [error, setError] = useState('');
  const [waiting, setWaiting] = useState(false);
  async function submit(e) {
    e.preventDefault(); setError(''); setWaiting(true);
    try {
      if (mode === 'login' || mode === 'register') {
        const path = method === 'email' ? `/shopping/auth/email/${mode}` : `/shopping/auth/${mode}`;
        const data = await request(path, null, { method: 'POST', body: JSON.stringify({ email, password }) });
        setChallenge(data.challenge); setCode(''); setPassword('');
        setSecret(data.secret || ''); setMode(method === 'app' && (mode === 'register' || data.enrollment) ? 'enroll' : 'verify');
      } else if (mode === 'enroll' || mode === 'verify') {
        const path = method === 'email' ? '/shopping/auth/email/verify' : '/shopping/auth/verify';
        const data = await request(path, null, { method: 'POST', body: JSON.stringify({ challenge, code: code.replace(/[\s-]/g, '') }) });
        setCode(''); setChallenge(''); setSecret('');
        if (data.recovery_codes) { setRecovery(data.recovery_codes); setPendingAuth(data); setMode('recovery'); }
        else onAuthenticated(data.token, data.email, true);
      }
    } catch (ex) { setError(ex.message); }
    finally { setWaiting(false); }
  }
  return <div className="auth-layout"><div className="auth-card"><div className="auth-toolbar"><div className="auth-logo"><Sparkles size={23}/></div><LanguageSwitch language={language} setLanguage={setLanguage}/></div>
    {mode === 'recovery' ? <><h1>{t('recoveryTitle')}</h1><p>{t('recoveryHint')}</p><div className="recovery-grid">{recovery?.map(c => <code key={c}>{c}</code>)}</div><button className="auth-submit" onClick={() => onAuthenticated(pendingAuth.token, pendingAuth.email, true)}>{t('saved')}</button></> : <>
      <h1>{t(mode === 'register' ? 'register' : mode === 'enroll' ? 'enroll' : mode === 'verify' ? 'verify' : 'welcomeBack')}</h1>
      <p>{mode === 'enroll' ? t('authSetup') : mode === 'verify' ? method === 'email' ? t('sent', { email }) : t('authCode') : t(method === 'email' ? 'authEmail' : 'authApp')}</p>
      {mode === 'enroll' && <div className="setup-key"><span>{t('setupKey')}</span><code dir="ltr">{secret}</code><button type="button" onClick={() => navigator.clipboard?.writeText(secret)}>{t('copy')}</button></div>}
      <form onSubmit={submit}>{(mode === 'login' || mode === 'register') ? <><label>{t('email')}<input type="email" dir="ltr" required autoComplete="email" value={email} onChange={e => setEmail(e.target.value)}/></label><label>{t('password')}<input type="password" dir="ltr" required minLength={12} autoComplete={mode === 'register' ? 'new-password' : 'current-password'} value={password} onChange={e => setPassword(e.target.value)}/></label>{mode === 'register' && <small>{t('passwordHint')}</small>}</> : <label>{t(method === 'email' ? 'emailCode' : 'appCode')}<input autoFocus required dir="ltr" autoComplete="one-time-code" inputMode="numeric" value={code} onChange={e => setCode(e.target.value)} placeholder="000000"/></label>}
        {(error || sessionError) && <div className="auth-error" role="alert">{error || sessionError}</div>}<button className="auth-submit" type="submit" disabled={waiting}>{t(waiting ? 'wait' : mode === 'register' ? 'register' : mode === 'login' ? 'continue' : 'verifyContinue')}</button></form>
      <button className="auth-switch" onClick={() => { setMode(mode === 'register' ? 'login' : 'register'); setError(''); setChallenge(''); setSecret(''); }}>{t(mode === 'register' ? 'already' : 'newAccount')}</button>
      <button className="auth-switch" onClick={() => { setMethod(method === 'email' ? 'app' : 'email'); setMode('login'); setError(''); setChallenge(''); setSecret(''); }}>{t(method === 'email' ? 'useApp' : 'useEmail')}</button>
    </>}
  </div></div>;
}

function App() {
  const [language, setLanguage] = useState(() => localStorage.getItem('techstock_language') || 'en');
  const t = (key, vars) => tr(language, key, vars);
  const [token, setToken] = useState(() => sessionStorage.getItem('techstock_auth') || '');
  const [account, setAccount] = useState('');
  const [conversations, setConversations] = useState([]);
  const [active, setActive] = useState(null);
  const [messages, setMessages] = useState([]);
  const [olderPage, setOlderPage] = useState({ local: [], cursor: null });
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [cart, setCart] = useState(EMPTY_CART);
  const [input, setInput] = useState('');
  const [pending, setPending] = useState(false);
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState('');
  const [retryTurn, setRetryTurn] = useState(null);
  const [cartOpen, setCartOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const bottom = useRef(null);
  const scrollContainer = useRef(null);
  const scrollAnchor = useRef(null);
  const textarea = useRef(null);
  const focusComposer = useRef(false);
  const activeRef = useRef(null);
  const activeToken = useRef(token);
  const streamRequest = useRef(null);
  const historyRequest = useRef(null);
  const sidebarRevision = useRef(0);
  const cartRevision = useRef(0);
  const cartState = useRef(EMPTY_CART);
  const cartQueue = useRef(Promise.resolve());
  const cartCount = cart.items.reduce((n, item) => n + item.quantity, 0);

  function replaceCart(value) {
    cartState.current = value;
    setCart(value);
  }

  function refreshSidebar(session) {
    const revision = ++sidebarRevision.current;
    return request('/shopping/conversations', session).then(history => {
      if (activeToken.current === session && sidebarRevision.current === revision) setConversations(history);
    });
  }

  useEffect(() => {
    localStorage.setItem('techstock_language', language);
    document.documentElement.lang = language;
    document.documentElement.dir = language === 'ar' ? 'rtl' : 'ltr';
  }, [language]);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    async function loadSession() {
      try {
        const user = await request('/shopping/auth/me', token, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setAccount(user.email);
      } catch (e) {
        if (controller.signal.aborted) return;
        if (e.status === 401) {
          sessionStorage.removeItem('techstock_auth');
          activeToken.current = '';
          setToken('');
          setAccount('');
        }
        setError(tr(localStorage.getItem('techstock_language'), 'apiError', { message: e.message }));
        return;
      }

      const cartVersion = cartRevision.current;
      const basket = request('/shopping/cart', token, { signal: controller.signal }).then(value => {
        if (!controller.signal.aborted && cartRevision.current === cartVersion) replaceCart(value);
      });
      const outcomes = await Promise.allSettled([refreshSidebar(token), basket]);
      if (!controller.signal.aborted && outcomes.some(outcome => outcome.status === 'rejected')) {
        setError(tr(localStorage.getItem('techstock_language'), 'ancillaryError'));
      }
    }
    void loadSession();
    return () => controller.abort();
  }, [token]);

  useLayoutEffect(() => {
    if (scrollAnchor.current !== null && scrollContainer.current) {
      const { height, top } = scrollAnchor.current;
      scrollContainer.current.scrollTop = top + scrollContainer.current.scrollHeight - height;
      scrollAnchor.current = null;
      return;
    }
    bottom.current?.scrollIntoView({ behavior: pending ? 'instant' : 'smooth' });
  }, [messages, pending, preview]);

  useEffect(() => {
    if (!pending && focusComposer.current) {
      focusComposer.current = false;
      textarea.current?.focus();
    }
  }, [pending]);

  function resetHistoryRequest() {
    historyRequest.current?.abort();
    historyRequest.current = null;
    setLoadingOlder(false);
  }

  async function openConversation(id) {
    if (streamRequest.current) return;
    resetHistoryRequest();
    const controller = new AbortController();
    historyRequest.current = controller;
    setError('');
    try {
      const data = await request(`/shopping/conversations/${id}?limit=${HISTORY_MESSAGES}`, token, { signal: controller.signal });
      if (historyRequest.current !== controller) return;
      const history = data.messages.map((message, index) => message.id == null
        ? { ...message, id: `history:${id}:${index}` } : message);
      const { recent, older } = historyWindow(history);
      activeRef.current = id;
      setActive(id);
      setMessages(recent);
      setOlderPage({ local: older, cursor: data.next_before_message_id ?? null });
      setRetryTurn(null);
      setMenuOpen(false);
    } catch (e) {
      if (historyRequest.current === controller) setError(e.message);
    } finally {
      if (historyRequest.current === controller) historyRequest.current = null;
    }
  }

  async function loadOlder() {
    if (!activeRef.current || streamRequest.current || historyRequest.current) return;
    const controller = new AbortController();
    historyRequest.current = controller;
    setLoadingOlder(true);
    setError('');
    try {
      const data = olderPage.local.length
        ? { messages: olderPage.local, next_before_message_id: olderPage.cursor }
        : await request(`/shopping/conversations/${activeRef.current}?limit=${HISTORY_MESSAGES}&before_message_id=${olderPage.cursor}`, token, { signal: controller.signal });
      if (historyRequest.current !== controller) return;
      const { recent, older } = historyWindow(data.messages);
      if (scrollContainer.current && recent.length) {
        scrollAnchor.current = { height: scrollContainer.current.scrollHeight, top: scrollContainer.current.scrollTop };
      }
      setMessages(list => [...recent, ...list]);
      setOlderPage({ local: older, cursor: data.next_before_message_id ?? null });
    } catch (e) {
      if (historyRequest.current === controller) setError(e.message);
    } finally {
      if (historyRequest.current === controller) resetHistoryRequest();
    }
  }

  function newConversation() {
    if (streamRequest.current) return;
    resetHistoryRequest();
    activeRef.current = null;
    setActive(null);
    setMessages([]);
    setOlderPage({ local: [], cursor: null });
    setRetryTurn(null);
    setError('');
    setInput('');
    setMenuOpen(false);
    textarea.current?.focus();
  }

  async function send(value = input, retry = null) {
    const message = value.trim();
    if (!message || streamRequest.current || !token) return;
    resetHistoryRequest();
    const controller = new AbortController();
    streamRequest.current = controller;
    const turn = retry ?? { message, conversation_id: activeRef.current, language, turn_id: makeTurnId() };
    const userId = turn.turn_id + ':user';
    const cartVersion = cartRevision.current;
    setPending(true);
    setError('');
    setRetryTurn(null);
    if (!retry) {
      setInput('');
      setMessages(list => {
        const committed = retryTurn ? list.filter(item => item.id !== retryTurn.turn_id + ':user') : list;
        return [...committed, { id: userId, role: 'user', content: message, products: [] }];
      });
    }
    try {
      const result = await streamChat(turn, token, controller.signal, products => {
        if (streamRequest.current === controller) setPreview(products);
      });
      if (streamRequest.current !== controller) return;
      activeRef.current = result.conversation_id;
      setActive(result.conversation_id);
      setMessages(list => [...list, { id: turn.turn_id + ':assistant', role: 'assistant', content: result.reply, products: result.products }]);
      if (cartRevision.current === cartVersion) {
        replaceCart(result.cart);
      } else {
        // A cart command in the chat may have committed alongside a drawer update.
        cartQueue.current = cartQueue.current.then(async () => {
          if (activeToken.current !== token) return;
          const refreshed = await request('/shopping/cart', token);
          if (activeToken.current === token) replaceCart(refreshed);
        }).catch(() => {});
      }
      setConversations(history => {
        const existing = history.find(item => item.id === result.conversation_id);
        const updated = {
          id: result.conversation_id,
          title: result.conversation_title ?? existing?.title ?? message.slice(0, 70),
          updated_at: result.updated_at ?? new Date().toISOString(),
        };
        return [updated, ...history.filter(item => item.id !== updated.id)];
      });
      void refreshSidebar(token).catch(() => {});
    } catch (e) {
      if (streamRequest.current !== controller) return;
      setRetryTurn(turn);
      setError(controller.signal.aborted ? t('cancelled') : e.message);
    } finally {
      if (streamRequest.current === controller) {
        streamRequest.current = null;
        focusComposer.current = true;
        setPending(false);
        setPreview(null);
      }
    }
  }

  const updateCart = useCallback((id, { quantity, change }) => {
    const session = token;
    cartRevision.current += 1;
    setError('');
    // Server responses include every line. Serializing each mutation keeps
    // deltas based on the last committed quantities and applies snapshots in order.
    cartQueue.current = cartQueue.current.then(async () => {
      if (activeToken.current !== session) return;
      const current = cartState.current.items.find(item => item.offer.id === id)?.quantity ?? 0;
      const next = quantity ?? Math.max(0, current + change);
      if (next === current) return;
      const updated = await request('/shopping/cart', session, { method: 'PUT', body: JSON.stringify({ offer_id: id, quantity: next }) });
      if (activeToken.current === session) replaceCart(updated);
    }).catch(e => {
      if (activeToken.current === session) setError(e.message);
    });
  }, [token]);

  const addProduct = useCallback(id => updateCart(id, { quantity: 1 }), [updateCart]);

  function authenticated(value, email, done) {
    if (!value || !done) return;
    sessionStorage.setItem('techstock_auth', value);
    activeToken.current = value;
    setToken(value);
    setAccount(email);
    setError('');
  }

  function signOut() {
    const session = token;
    activeToken.current = '';
    streamRequest.current?.abort();
    streamRequest.current = null;
    resetHistoryRequest();
    sessionStorage.removeItem('techstock_auth');
    setToken('');
    setAccount('');
    setMessages([]);
    setConversations([]);
    replaceCart(EMPTY_CART);
    setOlderPage({ local: [], cursor: null });
    setRetryTurn(null);
    setPending(false);
    setPreview(null);
    setMenuOpen(false);
    setCartOpen(false);
    setInput('');
    setError('');
    activeRef.current = null;
    setActive(null);
    void request('/shopping/auth/logout', session, { method: 'POST' }).catch(() => {});
  }
  if (!token || !account) return <AuthView onAuthenticated={authenticated} language={language} setLanguage={setLanguage} sessionError={error}/>;
  return <div className="app-shell">
    <aside className={`sidebar ${menuOpen ? 'show' : ''}`}>
      <div className="sidebar-head"><div className="brand-icon"><Sparkles size={20}/></div><span className="brand-name">techstock<span className="brand-dot">.</span></span><button className="mobile-close icon-button" onClick={() => setMenuOpen(false)} aria-label={t('closeMenu')}><X size={18}/></button></div>
      <button className="new-chat" onClick={newConversation}><Plus size={17}/> {t('newChat')}</button>
      <div className="sidebar-section"><span>{t('history')}</span><div className="history">{conversations.map(c => <button key={c.id} className={`history-item ${active === c.id ? 'selected' : ''}`} onClick={() => openConversation(c.id)}><Clock3 size={15}/><span>{c.title}</span></button>)}</div></div>
      <div className="sidebar-footer"><div className="status-dot"/> {t('region')} <button onClick={signOut} title={account}>{t('signOut')}</button></div>
    </aside>
    {menuOpen && <div className="scrim" onClick={() => setMenuOpen(false)}/>}
    <main className="main"><header className="topbar"><div className="topbar-left"><button className="menu-button icon-button" onClick={() => setMenuOpen(true)} aria-label={t('openMenu')}><Menu size={21}/></button><span>{t('assistant')}</span><ChevronRight size={15}/><span className="subtle">{t(active ? 'conversation' : 'newChat')}</span></div><div className="topbar-actions"><LanguageSwitch language={language} setLanguage={setLanguage}/><button className="cart-trigger" onClick={() => setCartOpen(true)}><ShoppingBag size={18}/><span>{t('cart')}</span>{cartCount > 0 && <span className="cart-count">{cartCount}</span>}</button></div></header>
      <div className="chat-scroll" ref={scrollContainer}><div className={`chat-content ${!messages.length ? 'welcome-content' : ''}`}>
        {!messages.length && <section className="welcome"><div className="hero-mark"><Sparkles size={29} strokeWidth={1.7}/></div><div className="eyebrow">{t('eyebrow')}</div><h1>{t('heroA')}<br/><em>{t('heroB')}</em></h1><p>{t('welcome')}</p><div className="suggestions">{t('suggestions').map(s => <button key={s} onClick={() => send(s)}>{s}<ArrowUp size={15}/></button>)}</div></section>}
        {(olderPage.local.length > 0 || olderPage.cursor !== null) && <button type="button" className="older-messages" onClick={loadOlder} disabled={loadingOlder || pending}>{t(loadingOlder ? 'wait' : 'olderMessages')}</button>}
        {messages.map((message, index) => <ChatMessage key={message.id ?? messages.length - index} message={message} add={addProduct} language={language}/>)}
        {pending && <div className="message assistant"><div className="message-avatar"><Sparkles size={16}/></div><div className="message-content"><div className="message-label">TechStock</div><div className="typing"><span/><span/><span/></div><small>{t(preview ? 'checkingRetailers' : 'searching')}</small>{preview?.length > 0 && <div className="product-grid">{preview.map(product => <ProductCard key={product.group_id ?? product.id} product={product} language={language} busy/>)}</div>}</div></div>}<div ref={bottom}/>
      </div></div>
      <div className="composer-wrap">{error && <div className="error-banner" role="alert"><span>{error}</span><div className="error-actions">{retryTurn && <button onClick={() => send(retryTurn.message, retryTurn)}>{t('retryTurn')}</button>}<button onClick={() => setError('')} aria-label={t('dismiss')}><X size={15}/></button></div></div>}<form className="composer" onSubmit={e => { e.preventDefault(); send(); }}><textarea ref={textarea} value={input} rows={1} placeholder={t('input')} onChange={e => setInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }} disabled={pending} aria-label={t('input')}/>{pending ? <button type="button" onClick={() => streamRequest.current?.abort()} aria-label={t('cancelSearch')}><X size={20}/></button> : <button type="submit" disabled={!input.trim() || !token} aria-label={t('send')}><ArrowUp size={20}/></button>}</form><div className="composer-note">{t('footer')}</div></div>
    </main>
    {cartOpen && <><div className="drawer-overlay" onClick={() => setCartOpen(false)}/><aside className="cart-drawer"><div className="drawer-head"><div><small>{t('selection')}</small><h2>{t('shoppingCart')} <span>({cartCount})</span></h2></div><button className="icon-button" onClick={() => setCartOpen(false)} aria-label={t('closeCart')}><X size={20}/></button></div><div className="drawer-list">{!cart.items.length ? <div className="empty-cart"><ShoppingBag size={35} strokeWidth={1.3}/><h3>{t('empty')}</h3><p>{t('emptyHint')}</p></div> : cart.items.map(item => <div className="cart-item" key={item.offer.id}><div className="cart-item-img">{item.offer.image_url && <img src={item.offer.image_url} alt="" loading="lazy"/>}</div><div className="cart-item-info"><strong>{item.offer.name}</strong><small>{item.offer.supplier}</small><b>{price(item.offer.price_tnd)}</b><div className="quantity"><button aria-label={t('decrease')} onClick={() => updateCart(item.offer.id, { change: -1 })}>−</button><span>{item.quantity}</span><button aria-label={t('increase')} onClick={() => updateCart(item.offer.id, { change: 1 })}>+</button></div></div><button className="remove" onClick={() => updateCart(item.offer.id, { quantity: 0 })} aria-label={`${t('remove')} ${item.offer.name}`}><Trash2 size={16}/></button></div>)}</div><div className="drawer-bottom"><div className="total-line"><span>{t('total')}</span><strong>{price(cart.total_tnd)}</strong></div><p>{t('cartNote')}</p>{cart.items.map(item => <a className="retailer-link" key={item.offer.id} href={item.offer.product_url} target="_blank" rel="noopener noreferrer">{t('viewAt')} {item.offer.supplier} <ExternalLink size={15}/></a>)}</div></aside></>}
  </div>;
}

createRoot(document.getElementById('root')).render(<App/>);
