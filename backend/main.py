import asyncio, hashlib, json, logging, secrets, uuid
from contextlib import asynccontextmanager
from contextlib import suppress
from itertools import islice
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from sqlalchemy import select
from backend.database import init_db, Session
from backend.config import settings
from backend.models import AgentRun, Order, now
from backend.schemas import ChatInput, ApprovalInput, EmailInput, StructuredInput, SelectionInput, ShoppingChatInput, CartUpdateInput, RegisterInput, LoginInput, MfaInput
from backend.services.orders import approve, order_dict
from backend.services.catalog import list_catalog
from backend.services.selection import select_offer
from backend.services.email import send_confirmation_email
from backend.services import shopping, auth, catalog_refresh
from backend.services import shopping_transactions as shopping_db
from sqlalchemy.exc import IntegrityError
from agent.graph import build_graph
from mcp_server.client import connect

@asynccontextmanager
async def lifespan(app):
    init_db()
    async with connect() as mcp:
        async with AsyncSqliteSaver.from_conn_string(str(Path(settings.database_url.removeprefix('sqlite:///')).with_suffix('.checkpoints.db'))) as saver:
            app.state.graph=build_graph(saver,mcp)
            app.state.saver=saver; app.state.mcp=mcp
            app.state.lock=asyncio.Lock()
            task = asyncio.create_task(catalog_refresh.loop()) if settings.catalog_auto_refresh else None
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError): await task
app=FastAPI(title='TechStock AI',lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173','http://127.0.0.1:5173'],
                   allow_methods=['GET','POST','PUT'], allow_headers=['Authorization','Content-Type'])

def session_id(authorization: str=Header(default='')):
    if not authorization.startswith('Bearer ') or len(authorization)<40: raise HTTPException(401,'Session required')
    return hashlib.sha256(authorization[7:].encode()).hexdigest()

async def shopping_user(authorization: str=Header(default='')):
    token = authorization[7:] if authorization.startswith('Bearer ') else ''
    try:
        user = await shopping_db.read(auth.authenticated_user, token)
    except shopping_db.ShoppingBusy as exc:
        raise shopping_conflict(exc) from exc
    if not user: raise HTTPException(401,'Sign in with your authenticator to continue')
    return user

@app.post('/shopping/auth/register')
def shopping_register(body:RegisterInput):
    try: return auth.register(body.email,body.password)
    except (ValueError,IntegrityError) as exc: raise HTTPException(409,'Account already exists') from exc

@app.post('/shopping/auth/login')
def shopping_login(body:LoginInput):
    result = auth.login(body.email,body.password)
    if result is None: raise HTTPException(401,'Invalid credentials or temporarily locked account')
    return result

@app.post('/shopping/auth/verify')
def shopping_verify(body:MfaInput):
    result = auth.verify(body.challenge,body.code)
    if result is None: raise HTTPException(401,'Invalid, expired, or already used code')
    return result

@app.post('/shopping/auth/email/register')
def shopping_email_register(body:RegisterInput):
    try: return auth.register_email(body.email,body.password)
    except IntegrityError as exc: raise HTTPException(409,'Account already exists. Sign in instead.') from exc
    except ValueError as exc:
        detail=str(exc)
        raise HTTPException(409 if 'already exists' in detail else 503,detail) from exc

@app.post('/shopping/auth/email/login')
def shopping_email_login(body:LoginInput):
    try: result=auth.login_email(body.email,body.password)
    except ValueError as exc: raise HTTPException(429 if 'wait one minute' in str(exc) else 503,str(exc)) from exc
    if not result: raise HTTPException(401,'Invalid credentials or temporarily locked account')
    return result

@app.post('/shopping/auth/email/verify')
def shopping_email_verify(body:MfaInput):
    result=auth.verify_email(body.challenge,body.code)
    if not result: raise HTTPException(401,'Invalid, expired, or already used code')
    return result

@app.get('/shopping/auth/me')
def shopping_me(user=Depends(shopping_user)):
    return user

@app.post('/shopping/auth/logout')
def shopping_logout(authorization: str=Header(default=''),user=Depends(shopping_user)):
    auth.logout(authorization[7:])
    return {'status':'signed_out'}

def shopping_conflict(exc):
    if isinstance(exc, shopping_db.ShoppingBusy):
        return HTTPException(503, str(exc), headers={'Retry-After': '1'})
    return HTTPException(409, str(exc))


@app.post('/shopping/chat')
async def shopping_chat(body: ShoppingChatInput, user=Depends(shopping_user)):
    try:
        return await shopping.reply(user['id'], body.message, body.conversation_id, body.language, turn_id=body.turn_id)
    except shopping_db.ShoppingConflict as exc:
        raise shopping_conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


_PREVIEW_FIELDS = (
    'id', 'name', 'brand', 'supplier', 'price_tnd', 'stock_status', 'scraped_at', 'product_url',
    'image_url', 'processor', 'graphics', 'ram_gb', 'storage_gb', 'storage_type', 'rating', 'rating_count',
)


def compact_card(card):
    """Send the fields rendered during the preview."""
    preview = {key: card[key] for key in _PREVIEW_FIELDS if card.get(key) is not None}
    preview['group_id'] = str(card.get('group_id') or card['id'])
    if card.get('verified_this_turn'):
        preview['verified_this_turn'] = True
    has_specs = any(card.get(key) for key in ('processor', 'graphics', 'ram_gb', 'storage_gb'))
    specs = card.get('specifications')
    if specs:
        preview['specifications'] = dict(islice(specs.items(), 1 if has_specs else 3))
    elif not has_specs and card.get('description'):
        preview['description'] = card['description']
    if len(card.get('offers', [])) > 1:
        preview['offers'] = [
            {key: value for key, value in offer.items() if key != 'verified_this_turn' or value}
            for offer in card['offers']
        ]
    return preview


@app.post('/shopping/chat/stream')
async def shopping_chat_stream(body: ShoppingChatInput, user=Depends(shopping_user),
                               stream_version: int = Query(default=1, ge=1, le=2)):
    compact = stream_version == 2
    if compact and body.conversation_id:
        try:
            await shopping_db.read(shopping.validate_conversation, user['id'], body.conversation_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    async def events():
        ready = asyncio.Event()
        pending = None
        terminal = None
        sent = {}
        sent_order = []
        sequence = 0

        def progress(products):
            nonlocal pending
            if pending is not None:
                shopping.metrics.increment('previews_coalesced')
            pending = products
            ready.set()

        async def run():
            nonlocal terminal
            try:
                result = await shopping.reply(user['id'], body.message, body.conversation_id,
                                              body.language, progress=progress, turn_id=body.turn_id)
                terminal = dict(type='result', data=result) if compact else dict(type='result', **result)
            except shopping_db.ShoppingConflict as exc:
                terminal = dict(type='error', detail=str(exc), retryable=True)
            except ValueError:
                terminal = dict(type='error', detail='Conversation not found')
            except Exception:
                logging.getLogger(__name__).exception('Shopping chat stream failed')
                terminal = dict(type='error', detail='Shopping chat failed')
            finally:
                ready.set()

        task = asyncio.create_task(run())
        try:
            while True:
                try:
                    await asyncio.wait_for(ready.wait(), timeout=15 if compact else None)
                except TimeoutError:
                    event = dict(type='heartbeat')
                else:
                    ready.clear()
                    cards, pending = pending, None
                    if cards is not None:
                        if not compact:
                            event = dict(type='progress', products=cards)
                        else:
                            current = {card['group_id']: card for card in map(compact_card, cards)}
                            order = list(current)
                            upserts = [card for key, card in current.items() if sent.get(key) != card]
                            removed = [key for key in sent if key not in current]
                            event = (dict(type='progress', upserts=upserts, removed_ids=removed, order=order)
                                     if upserts or removed or order != sent_order else None)
                            sent, sent_order = current, order
                        if terminal is not None:
                            ready.set()
                        if event is None:
                            continue
                    elif terminal is not None:
                        event = terminal
                    else:
                        continue
                if compact:
                    sequence += 1
                    event.update(version=2, sequence=sequence)
                with shopping.metrics.stage('stream_send'):
                    line = json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n'
                    shopping.metrics.increment('stream_bytes', len(line.encode('utf-8')))
                    yield line
                if event['type'] in ('result', 'error'):
                    break
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(events(), media_type='application/x-ndjson',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.get('/shopping/conversations')
def shopping_conversations(user=Depends(shopping_user)):
    return shopping.conversations(user['id'])

@app.get('/shopping/conversations/{conversation_id}')
def shopping_conversation(conversation_id: str, user=Depends(shopping_user),
                          limit: int | None = Query(default=None, ge=1, le=100),
                          before_message_id: int | None = Query(default=None, gt=0)):
    try:
        return shopping.conversation(user['id'], conversation_id, limit=limit, before_message_id=before_message_id)
    except ValueError as exc: raise HTTPException(404,str(exc))

@app.get('/shopping/cart')
def shopping_cart(user=Depends(shopping_user)):
    return shopping.cart(user['id'])

@app.get('/shopping/metrics')
def shopping_metrics(user=Depends(shopping_user)):
    return shopping.metrics.snapshot()

@app.get('/shopping/catalog-status')
def shopping_catalog_status(user=Depends(shopping_user)):
    return catalog_refresh.status()

@app.put('/shopping/cart')
def shopping_cart_update(body:CartUpdateInput,user=Depends(shopping_user)):
    try: return shopping.update_cart(user['id'],body.offer_id,body.quantity)
    except shopping_db.ShoppingConflict as exc: raise shopping_conflict(exc) from exc
    except ValueError as exc: raise HTTPException(409,str(exc))

@app.get('/health')
def health(): return {'status':'ok','mode':'sandbox'}
@app.post('/sessions')
def new_session(): return {'token':secrets.token_urlsafe(32)}

def run_status(result, pending=None):
    if pending:
        if 'human_selection' in pending: return 'awaiting_selection'
        if 'human_approval' in pending: return 'awaiting_approval'
    if result.get('__interrupt__'):
        kind=result['__interrupt__'][0].value.get('kind')
        return 'awaiting_selection' if kind=='product_selection' else 'awaiting_approval'
    return result.get('status','error')


def persist(thread,result,pending=None):
    with Session.begin() as db:
        r=db.get(AgentRun,thread)
        r.status=run_status(result,pending)
        r.steps_executed=result.get('steps',0); r.stop_reason=result.get('stop_reason')
        r.total_tokens=result.get('total_tokens',0); r.estimated_cost=result.get('estimated_cost')
        r.trajectory=result.get('trajectory',[])
        if r.status not in ('awaiting_approval','awaiting_selection'): r.completed_at=now()
    return {k:v for k,v in result.items() if k not in ('__interrupt__','session_id','email','message')} | {'thread_id':thread,'status':r.status}

@app.post('/chat')
async def chat(body:ChatInput,sid=Depends(session_id)):
    thread=uuid.uuid4().hex
    with Session.begin() as db: db.add(AgentRun(id=thread,thread_id=thread,user_session_id=sid))
    async with app.state.lock:
        result=await app.state.graph.ainvoke(dict(message=body.message,email=str(body.email),session_id=sid,steps=0,trajectory=[]),
            config={'configurable':{'thread_id':thread},'recursion_limit':30,'run_id':uuid.UUID(thread)})
    return persist(thread,result)

@app.post('/runs/{thread}/approval')
async def approval(thread:str,body:ApprovalInput,sid=Depends(session_id)):
    async with app.state.lock:
        with Session() as db:
            r=db.get(AgentRun,thread)
            if not r or r.user_session_id!=sid: raise HTTPException(404,'Run not found')
        config={'configurable':{'thread_id':thread},'recursion_limit':30,'run_id':uuid.UUID(thread)}
        state=await app.state.graph.aget_state(config)
        if state.next and 'human_approval' not in state.next: raise HTTPException(409,'Product selection is required before purchase approval')
        if state.values.get('proposal',{}).get('id')!=body.proposal_id: raise HTTPException(409,'Proposal mismatch')
        try: approve(body.proposal_id,sid,body.proposal_hash,body.approve)
        except ValueError as exc: raise HTTPException(409,str(exc))
        if not state.next: return persist(thread,state.values)
        result=await app.state.graph.ainvoke(Command(resume=True),config=config)
        return persist(thread,result)

@app.get('/catalog')
def catalog(sid=Depends(session_id), limit: int | None = Query(default=None, ge=1, le=1000),
            after_id: int | None = Query(default=None, ge=0)):
    return list_catalog(limit=limit, after_id=after_id)
@app.get('/orders')
def orders(sid=Depends(session_id)):
    with Session() as db: return [order_dict(o) for o in db.scalars(select(Order).where(Order.user_session_id==sid)).all()]
@app.post('/orders/{order_id}/retry-email')
def retry_email(order_id:int,sid=Depends(session_id)):
    with Session() as db:
        o=db.get(Order,order_id)
        if not o or o.user_session_id!=sid: raise HTTPException(404,'Order not found')
    return send_confirmation_email(EmailInput(order_id=order_id))
@app.get('/runs')
def runs(sid=Depends(session_id)):
    with Session() as db:
        return [dict(id=r.id,status=r.status,steps=r.steps_executed,stop_reason=r.stop_reason,tokens=r.total_tokens,
            estimated_cost=r.estimated_cost,trajectory=r.trajectory,
            latency=(r.completed_at-r.started_at).total_seconds() if r.completed_at else None)
            for r in db.scalars(select(AgentRun).where(AgentRun.user_session_id==sid)).all()]

@app.post('/structured-request')
async def structured(body:StructuredInput,sid=Depends(session_id)):
    async def parser(_): return body.requirements,0,0.0
    thread=uuid.uuid4().hex
    with Session.begin() as db: db.add(AgentRun(id=thread,thread_id=thread,user_session_id=sid))
    async with app.state.lock:
        graph=build_graph(app.state.saver,app.state.mcp,parser)
        result=await graph.ainvoke(dict(message='User-entered structured request (no model)',email=str(body.email),session_id=sid,steps=0,trajectory=[]),
            config={'configurable':{'thread_id':thread},'recursion_limit':30,'run_id':uuid.UUID(thread)})
    return persist(thread,result)

@app.get('/runs/{thread}/trace')
async def trace_link(thread:str,sid=Depends(session_id)):
    with Session() as db:
        r=db.get(AgentRun,thread)
        if not r or r.user_session_id!=sid: raise HTTPException(404,'Run not found')
    if not settings.langsmith_tracing: return {'url':None,'status':'disabled'}
    try:
        from langsmith import Client
        client=Client(timeout_ms=5000)
        project=await asyncio.to_thread(client.read_project,project_name=settings.langsmith_project)
        result=await client.runs.get_url(thread,project_id=str(project.id),trace_id=thread)
        return {'url':result.url,'status':'available'}
    except Exception:
        return {'url':None,'status':'Trace not indexed or tracing unavailable'}


@app.post('/runs/{thread}/selection')
async def selection(thread:str,body:SelectionInput,sid=Depends(session_id)):
    async with app.state.lock:
        with Session() as db:
            r=db.get(AgentRun,thread)
            if not r or r.user_session_id!=sid: raise HTTPException(404,'Run not found')
        config={'configurable':{'thread_id':thread},'recursion_limit':30,'run_id':uuid.UUID(thread)}
        state=await app.state.graph.aget_state(config)
        if state.values.get('selection',{}).get('id')!=body.selection_id: raise HTTPException(409,'Selection mismatch')
        if 'human_selection' not in state.next and 'proposal' not in state.values:
            raise HTTPException(409,'Run is not awaiting product selection')
        try: select_offer(sid,body)
        except ValueError as exc: raise HTTPException(409,str(exc))
        if 'human_selection' not in state.next:
            return persist(thread,state.values,state.next)
        result=await app.state.graph.ainvoke(Command(resume=True),config=config)
        return persist(thread,result)


@app.get('/runs/{thread}')
async def run_details(thread:str,sid=Depends(session_id)):
    with Session() as db:
        r=db.get(AgentRun,thread)
        if not r or r.user_session_id!=sid: raise HTTPException(404,'Run not found')
    state=await app.state.graph.aget_state({'configurable':{'thread_id':thread}})
    return {k:v for k,v in state.values.items() if k not in ('session_id','email','message')} | {
        'thread_id':thread,'status':run_status(state.values,state.next)}
