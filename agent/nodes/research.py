import asyncio
import time
from ..policies import topics_for, evidence_gaps
from ..retry import with_retry
from .intake import event


# [PDF 2·8] LLM 자유 계획 대신 확인된 대상별 지표·추가 근거 조회를 코드로 구성한다.
# 추가 근거는 로그/배포/시계열의 제공 여부를 포함하며 미제공을 정상 판정으로 바꾸지 않는다.
async def plan_node(state,deps):
    tasks=[]
    for target in state['targets']:
        for kind in ('monitoring','evidence'):
            tasks.append({'id':f'{target}:{kind}','kind':kind,'target':target,'status':'pending'})
    return {'tasks':tasks,'collected':[],'replan_rounds':0,'needs_analysis':len(state['targets'])>1,
            'research_plan':[{'role':r,'condition':condition} for r,condition in
                             [('metrics','관측 자료 제공'),('logs','로그 또는 배포 자료 제공'),('runbook','관련 매뉴얼 검색 성공')]],
            'events':event(state,'PlanCreated',task_count=len(tasks),targets=state['targets'])}


# [자료 수집 개선] 대상·자료별 조회를 병렬 처리하고 근거 ID와 오류 상태를 함께 저장한다.
async def research_node(state,deps):
    tasks=[dict(t) for t in state['tasks']]
    async def one(task):
        retries=0
        try:
            if task['kind']=='runbook':
                server,template=await deps.mcp.find_resource_template('get_incident_runbook')
                data=await deps.mcp.read_resource(template.format(incident_type=task['target']),server=server)
                if 'Runbook Not Found' in data:
                    raise ValueError('Missing runbook')
            else:
                tool='get_monitoring_status' if task['kind']=='monitoring' else 'get_diagnostic_evidence'
                data,retries=await with_retry(deps.mcp.call_tool,tool,{'system_name':task['target']})
            return {'id':f"{task['kind']}:{task['target']}",'source':task['kind'],'target':task['target'],'data':data,'retries':retries}
        except Exception as exc:
            # Do not put raw exception strings (possibly URLs/credentials) in reports or traces.
            return {'id':f"{task['kind']}:{task['target']}",'source':task['kind'],'target':task['target'],'data':{},'error':type(exc).__name__,'retries':retries}
    pending=[t for t in tasks if t['status']=='pending']
    start=time.perf_counter();results=await asyncio.gather(*(one(t) for t in pending))
    events=list(state.get('events',[]));retry=state.get('retry_count',0)
    for task,result in zip(pending,results):
        task['status']='failed' if result.get('error') else 'done';retry+=result['retries']
        events.append({'type':'TaskCompleted','source':task['kind'],'target':task['target'],'retries':result['retries'],'error':result.get('error')})
    all_collected=state.get('collected',[])+results
    return {'tasks':tasks,'collected':all_collected,'retry_count':retry,
            'missing_evidence':evidence_gaps(all_collected,state['targets']),
            'parallel_elapsed':round(time.perf_counter()-start,3),'events':events}


# [호환 경로] keyword 모드의 보강된 규칙 검색이다. 현재 rag/multi 모드는 벡터 검색을 사용한다.
async def coverage_node(state,deps):
    collected=state.get('collected',[]);tasks=list(state['tasks'])
    attempted={t['target'] for t in tasks if t['kind']=='runbook'}
    needed=[]
    for c in collected:
        if c['source']=='monitoring' and not c.get('error'):
            needed.extend(topics_for(c['data']))
    missing=sorted(set(needed)-attempted)
    rounds=state.get('replan_rounds',0)
    if missing and rounds<1:
        tasks += [{'id':'runbook:'+topic,'kind':'runbook','target':topic,'status':'pending'} for topic in missing]
    else:
        missing=[]
    return {'tasks':tasks,'replan_rounds':rounds+bool(missing),
            'missing_evidence':evidence_gaps(collected,state['targets']),
            'events':event(state,'ReplanCheck',is_sufficient=not missing,target=', '.join(missing),round=rounds)}


# [판단 개선] 임계값 비교를 모델의 문장 생성과 분리해 계산 결과로 전달한다.
# CPU 표본이 모두 높더라도 이 계산만으로 관측 범위 밖의 지속이나 원인을 확정하지 않는다.
async def analysis_node(state,deps):
    # Deterministic comparisons are derived facts, never a new source of causal evidence.
    systems={}
    for target in state['targets']:
        metric=next((c['data'] for c in state['collected']
                     if c['source']=='monitoring' and c['target']==target and not c.get('error')), {})
        diagnostic=next((c['data'] for c in state['collected']
                         if c['source']=='evidence' and c['target']==target and not c.get('error')), {})
        samples=diagnostic.get('cpu_series',{}).get('records',[])
        systems[target]={
            'memory_restart_rule_applicable':any(c['source']=='runbook' and c['target']=='memory-leak'
                                                for c in state['collected']),
            'memory_at_least_90': metric.get('memory_percent',-1)>=90 if metric else None,
            'cpu_above_90': metric.get('cpu_percent',-1)>90 if metric else None,
            'cpu_series_sample_count':len(samples),
            'all_cpu_samples_above_90':all(r.get('cpu_percent',-1)>90 for r in samples) if samples else None,
            'cpu_observation_times':[r.get('observed_at') for r in samples],
            'logs_provided':diagnostic.get('logs',{}).get('availability')=='provided',
        }
    summary={'systems':systems,
             'causal_limit':'수치와 증상 비교만으로 근본 원인 또는 공통 원인 부재를 확정할 수 없음'}
    return {'analysis_summary':summary,
            'events':event(state,'AnalysisCompleted',length=len(systems),
                           note='서비스별 임계값 및 제공된 시계열 표본 비교')}
