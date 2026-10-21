from langgraph.config import get_stream_writer
from ..policies import identify_targets, known_systems
from ..chains import model_chain, invoke_json
from ..prompts import ROUTE, CHAT


def event(state,kind,**kwargs):
    return state.get('events',[])+[{'type':kind,**kwargs}]

# 노드의 시작점
# [PDF 2·7] 분류/대상/대화 노드를 분리했다. 서비스명이 명확하면 분류 LLM 호출을 줄인다.
async def route_node(state,deps):
    targets,unknown,_=identify_targets(state['query'])
    if targets or unknown:
        route='incident';usage={'usd':0}
    else:
        if hasattr(deps.fast_llm, '_chat'):
            result,usage=await invoke_json(deps.fast_llm,ROUTE,{'question':state['query']},'router')
            route=result.get('route') if isinstance(result,dict) else None
            if route not in ('chat','incident'):
                route='incident'  # Ask for a target rather than silently answering an incident as chat.
        else:
            result,usage=deps.fast_llm.route(state['query']);route=result['route']
    return {'route':route,'cost_usd':state.get('cost_usd',0)+usage['usd'],
            'events':event(state,'RouteDecided',route=route)}


# [PDF 7] 대상 누락·미등록 여부를 별도 노드에서 처리하고, 가정은 답변에 명시한다.
async def target_node(state,deps):
    targets,unknown,assumption=identify_targets(state['query'])
    final=''
    if unknown:
        final=f"등록된 모니터링 대상에 {', '.join(unknown)}가 없습니다. 다른 서비스로 대체하지 않았습니다. 정확한 서비스명 또는 연결 여부를 확인해 주세요."
    elif not targets:
        final='조사할 서비스가 지정되지 않았습니다. 정확한 서비스명을 알려주세요. 조회 가능한 대상: '+', '.join(sorted(known_systems()))
    return {'targets':targets,'unknown_targets':unknown,'assumption':assumption,'final':final,
            'events':event(state,'TargetChecked',targets=targets,needs_clarification=bool(final))}


async def chat_node(state,deps):
    writer=get_stream_writer()
    if hasattr(deps.fast_llm, '_chat'):
        history='\n'.join(f"{m['role']}: {m['content']}" for m in state.get('history',[])[-10:])
        result=await model_chain(deps.fast_llm,CHAT,'chat').ainvoke(
            {'input':f"대화 이력:\n{history}\n사용자: {state['query']}"})
        reply,usage=result['value'],result['usage']
        usage['prompt']=result['prompt']
        writer({'type':'AnswerToken','delta':reply})
    else:
        reply,usage=await deps.fast_llm.chat(state['query'],state.get('history',[]),on_token=lambda piece:writer({'type':'AnswerToken','delta':piece}))
    return {'final':reply,'last_prompt':usage.get('prompt'),'cost_usd':state.get('cost_usd',0)+usage['usd'],
            'events':event(state,'Finalize',pipeline='chat-direct')}
