import asyncio, time
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from backend.services.llm import chat_model
from backend.config import settings
from backend.schemas import Requirements
from backend.services.selection import prepare_selection, trusted_choice, selected_proposal
from agent.state import AgentState
from agent.stop_rules import consume
from agent.prompts import EXTRACT
from mcp_server.client import invoke

async def parse_openai(message):
    model=chat_model(timeout=30)
    result=await model.with_structured_output(Requirements,include_raw=True,method='function_calling').ainvoke([
        ('system',EXTRACT),('human',message)])
    if result['parsed'] is None: raise ValueError('request_extraction_failed')
    usage=result['raw'].usage_metadata or {}
    cost=None
    if settings.input_cost_per_million is not None and settings.output_cost_per_million is not None:
        cost=(usage.get('input_tokens',0)*settings.input_cost_per_million+usage.get('output_tokens',0)*settings.output_cost_per_million)/1e6
    return result['parsed'],usage.get('total_tokens',0),cost

def build_graph(checkpointer, mcp_session, parser=parse_openai, caller=None):
    async def call(s,name,args):
        for attempt in range(settings.max_tool_retries+1):
            consume(s,name,args)
            t=time.perf_counter()
            try:
                result=await (caller(name,args) if caller else invoke(mcp_session,name,args))
                s['trajectory'][-1].update(ok=True,latency=time.perf_counter()-t)
                return result
            except (TimeoutError,ConnectionError,OSError) as exc:
                s['trajectory'][-1].update(ok=False,error=type(exc).__name__,retry=attempt)
                if attempt==settings.max_tool_retries: raise
                await asyncio.sleep(0.05*(attempt+1))
            except Exception as exc:
                s['trajectory'][-1].update(ok=False,error=str(exc)[:200])
                raise
    def guarded(fn):
        async def run(s):
            try: return await fn(s)
            except Exception as exc:
                s.update(status='error',stop_reason=str(exc)[:300])
                return s
        return run
    @guarded
    async def parse_request(s):
        consume(s,'parse_request')
        r,tokens,cost=await parser(s['message'])
        s.update(requirements=r.model_dump(mode='json'),total_tokens=tokens,estimated_cost=cost,status='running')
        return s
    @guarded
    async def validate_request(s):
        r=Requirements.model_validate(s['requirements'])
        if r.quantity is None: r.quantity=1
        s['requirements']=r.model_dump(mode='json')
        missing=[k for k in ('category','budget') if getattr(r,k) is None]
        if missing or r.other_preferences:
            s.update(status='clarification',stop_reason='missing_or_unsupported_requirements',
                explanation='Please specify '+', '.join(missing or ['supported technical requirements'])+'. Unsupported constraints: '+r.other_preferences)
        return s
    @guarded
    async def search_products(s):
        s['offers']=await call(s,'search_products',dict(requirements=s['requirements']))
        if not s['offers']: s.update(status='no_feasible_offer',stop_reason='no_eligible_fresh_offer')
        return s
    @guarded
    async def compare_offers(s):
        s['comparison']=await call(s,'compare_offers',dict(offer_ids=[o['id'] for o in s['offers']],requirements=s['requirements']))
        if not s['comparison']: s.update(status='no_feasible_offer',stop_reason='no_eligible_fresh_offer')
        return s
    @guarded
    async def check_budget(s):
        s['budget_result']=await call(s,'check_budget',dict(offer_id=s['selected_offer_id'],quantity=s['requirements']['quantity'],budget=s['requirements']['budget']))
        if not s['budget_result']['valid']: s.update(status='no_feasible_offer',stop_reason='budget_exceeded')
        return s
    @guarded
    async def generate_recommendations(s):
        consume(s,'generate_recommendations')
        s['selection']=prepare_selection(s['session_id'],s['requirements'])
        s['comparison']=s['selection']['recommendations']
        s['explanation']='Review the estimated best fit and alternatives, then select a product and quantity. Selection is not purchase approval.'
        return s
    async def human_selection(s):
        interrupt({'kind':'product_selection',**s['selection']})
        try:
            choice=trusted_choice(s['selection']['id'],s['session_id'])
            consume(s,'human_selection')
            s['requirements']=choice['requirements']; s['selected_offer_id']=choice['offer_id']
        except Exception as exc:
            s.update(status='error',stop_reason=str(exc)[:300])
        return s
    @guarded
    async def generate_proposal(s):
        consume(s,'generate_proposal')
        s['proposal']=selected_proposal(s['selection']['id'],s['session_id'],s['email'])
        return s
    async def human_approval(s):
        # Interrupt before any step increment: LangGraph replays this node on resume.
        interrupt({'kind':'purchase_approval',**s['proposal']})
        from backend.database import Session
        from backend.models import Proposal
        with Session() as db:
            p=db.get(Proposal,s['proposal']['id'])
            if not p or p.user_session_id!=s['session_id'] or p.status not in ('approved','ordered'):
                s.update(status='rejected',stop_reason='human_rejected_or_unapproved')
                return s
        try: consume(s,'human_approval')
        except RuntimeError: s.update(status='error',stop_reason='step_limit')
        return s
    @guarded
    async def create_order(s):
        s['order']=await call(s,'create_purchase_order',dict(proposal_id=s['proposal']['id'],idempotency_key=s['proposal']['id']))
        return s
    @guarded
    async def send_confirmation(s):
        try:
            s['email_result']=await call(s,'send_confirmation_email',dict(order_id=s['order']['id']))
        except Exception as exc:
            s['email_result']=dict(status='pending',error=type(exc).__name__)
        s['order']={**s['order'],'email_status':s['email_result']['status']}
        s.update(status='success',stop_reason='sandbox_order_created')
        return s
    async def finalize(s):
        return s
    graph=StateGraph(AgentState)
    nodes=[parse_request,validate_request,search_products,compare_offers,generate_recommendations,human_selection,check_budget,generate_proposal,human_approval,create_order,send_confirmation,finalize]
    names=['parse_request','validate_request','search_products','compare_offers','generate_recommendations','human_selection','check_budget','generate_proposal','human_approval','create_order','send_confirmation','finalize']
    for name,fn in zip(names,nodes): graph.add_node(name,fn)
    graph.add_edge(START,names[0])
    for current,next_name in zip(names[:-1],names[1:]):
        graph.add_conditional_edges(current,lambda s,n=next_name: 'finalize' if s.get('status') in ('error','clarification','no_feasible_offer','rejected') else n)
    graph.add_edge('finalize',END)
    return graph.compile(checkpointer=checkpointer)
