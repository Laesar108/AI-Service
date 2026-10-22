"""근거 기반 장애 대응 그래프: 대상 확인, 근거 확보 여부 점검, 검증 횟수 제한을 수행한다."""
from functools import partial
from langgraph.graph import END, StateGraph
from langsmith import traceable
from .state import AgentState
from .nodes.intake import route_node, target_node, chat_node
from .nodes.research import plan_node, research_node, coverage_node, analysis_node
from .nodes.answer import write_node, validate_node, fallback_node, finalize_node
from .researchers import retrieve_node, worker_node, supervisor_join, runbook_branch
from .selection import select_research_node


# [그래프 구성] 기존 단일 파일의 노드 구현을 분리하고 이 함수에는 연결을 모았다.
# target에서 조사 대상을 확인하고, 전문 서브그래프 합류 후 작성·검증을 수행한다.
def build_graph(deps):
    graph=StateGraph(AgentState)
    nodes={'route':route_node,'target':target_node,'chat_answer':chat_node,
           'plan':plan_node,'research':research_node,'replan_check':coverage_node,
           'analysis':analysis_node,'write':write_node,'validate':validate_node,
           'fallback':fallback_node,'finalize':finalize_node}
    if deps.research_mode != 'keyword':
        nodes.pop('replan_check')
    for name,fn in nodes.items():
        # 의존성에는 인증된 클라이언트가 포함될 수 있으므로 추적 입력에서 제외한다.
        wrapped=traceable(name=name+'_node',process_inputs=lambda x:{k:v for k,v in x.items() if k!='deps'})(fn)
        graph.add_node(name,partial(wrapped,deps=deps))
    graph.set_entry_point('route')
    graph.add_conditional_edges('route',lambda s:'chat_answer' if s['route']=='chat' else 'target',{'chat_answer':'chat_answer','target':'target'})
    graph.add_edge('chat_answer',END)
    # [대상 확인 분기] 미등록/불명확 대상은 확인 안내로 종료해 기본 서비스로 임의 대체하지 않는다.
    graph.add_conditional_edges('target',lambda s:'stop' if s.get('final') else 'plan',{'stop':END,'plan':'plan'})
    graph.add_edge('plan','research')
    if deps.research_mode == 'keyword':
        graph.add_edge('research','replan_check')
        graph.add_conditional_edges('replan_check',lambda s:'research' if any(t['status']=='pending' for t in s['tasks']) else 'analysis',{'research':'research','analysis':'analysis'})
        graph.add_edge('analysis','write')
    else:
        if deps.research_mode not in ('rag','multi'):
            raise ValueError('Unknown research mode')
        if deps.research_mode == 'rag':
            graph.add_node('retrieve',partial(retrieve_node,deps=deps))
            graph.add_edge('research','retrieve')
            graph.add_edge('retrieve','analysis')
            graph.add_edge('analysis','write')
        else:
            # [추가 최적화] 역할 선택 후 지표·로그와 검색/Runbook 분기를 동시에 시작한다.
            # 각 작업자의 호출 조건 검사가 불필요한 LLM 실행을 생략하고, 합류 후 답변을 작성한다.
            graph.add_node('select_research', select_research_node)
            graph.add_edge('research','select_research')
            workers = []
            for role in ('metrics','logs'):
                name = f'{role}_researcher'
                graph.add_node(name,worker_node(role,deps.fast_llm))
                graph.add_edge('select_research',name)
                workers.append(name)
            graph.add_node('runbook_pipeline', runbook_branch(deps))
            graph.add_edge('select_research','runbook_pipeline')
            workers.append('runbook_pipeline')
            graph.add_node('supervisor_join',supervisor_join)
            graph.add_edge(workers,'supervisor_join')
            graph.add_edge('supervisor_join','analysis')
            graph.add_edge('analysis','write')
    graph.add_edge('write','validate')
    # [추가 검증] 기존의 최대 시도 후 강제 통과를 없애고 실패 시 제한 답변으로 전환한다.
    graph.add_conditional_edges('validate',lambda s:{'passed':'finalize','retry':'write','exhausted':'fallback'}[s['validation_status']],{'finalize':'finalize','write':'write','fallback':'fallback'})
    graph.add_edge('fallback','finalize')
    graph.add_edge('finalize',END)
    return graph.compile()
