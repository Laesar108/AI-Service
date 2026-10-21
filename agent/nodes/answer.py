from ..reporting import write_report, call_json, REVIEW_SYSTEM, fallback_report, evidence_payload
from ..policies import deterministic_errors
from .intake import event


async def write_node(state,deps):
    draft,usage=await write_report(deps.quality_llm,state)
    return {'draft':draft,'last_prompt':usage.get('prompt'),
            'cost_usd':state.get('cost_usd',0)+usage['usd'],
            'events':event(state,'DraftWritten',attempt=state.get('validation_attempts',0)+1,length=len(draft))}


# [검증 개선] 기존 길이·번호 검사에서 근거 ID/수치 검사 + LLM 의미 검토로 확장했다.
# 최초 작성과 한 번의 재작성만 허용하며 검토 오류/형식 실패도 통과로 취급하지 않는다.
async def validate_node(state,deps):
    errors=deterministic_errors(state['draft'],state['collected']);usd=0
    try:
        result,usage=await call_json(deps.quality_llm,REVIEW_SYSTEM,{'question':state['query'],
            'assumption':state.get('assumption',''),'evidence':evidence_payload(state),
            'calculated_conditions':state.get('analysis_summary',{}),
            'missing_evidence':state.get('missing_evidence',[]),'draft':state['draft']})
        usd=usage['usd']
        if not isinstance(result.get('errors'),list) or not all(isinstance(e,str) for e in result['errors']) or type(result.get('passed')) is not bool:
            raise ValueError('Invalid review schema')
        errors+=result['errors']
        if not result['passed'] and not errors:
            errors.append('검증자가 구체적 근거 없이 반려함: 검증 미완료')
    except Exception:
        errors.append('의미 검증을 완료하지 못함')
    attempt=state.get('validation_attempts',0)+1
    status='passed' if not errors else ('exhausted' if attempt>=2 else 'retry')
    return {'validation_attempts':attempt,'validation_status':status,'validation_errors':errors,
            'validation_feedback':'\n'.join(errors),'cost_usd':state.get('cost_usd',0)+usd,
            'events':event(state,'Validation',attempt=attempt,verdict='합격' if not errors else '불합격',status=status,errors=errors)}


# [추가 노드] 검증 한도 초과 시 확인한 관측과 판단 한계를 반환해 강제 합격을 방지한다.
async def fallback_node(state,deps):
    return {'draft':fallback_report(state),'validation_status':'limited',
            'events':event(state,'ValidationLimited',reason='재작성 한도 도달 또는 검증 불가')}


async def finalize_node(state,deps):
    return {'final':state['draft'],'events':event(state,'Finalize',pipeline='incident-grounded',validation_status=state.get('validation_status'))}
