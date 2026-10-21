"""Grounded answer payloads; all model calls go through LCEL."""
import json
from .chains import model_chain, invoke_json
from .prompts import WRITE as WRITE_SYSTEM, REVIEW as REVIEW_SYSTEM


async def call_json(llm, system, payload):
    return await invoke_json(llm, system, payload, "reviewer")


# [근거 보존] 전문 분석의 결론만 전달하지 않고 작성자·Reviewer에게 원문과 근거 ID를 제공한다.
def evidence_payload(state):
    # Keep raw evidence available to the Reviewer; specialist opinions cannot replace it.
    return [{"id": c.get("id", f"{c['source']}:{c['target']}"),
             "source": c["source"], "target": c["target"],
             "data": c["data"], **({"error":c["error"]} if c.get("error") else {})}
            for c in state.get("collected", [])]


# [LCEL·입력 최적화] 공통 체인으로 작성하며 비어 있는 전문 분석 필드와 시간 정보는 제외한다.
# 모의 데이터 표시와 대상 가정은 코드에서 붙여 모델의 누락 가능성을 줄인다.
async def write_report(llm, state):
    if not hasattr(llm, "_chat"):
        return fallback_report(state), {"usd":0, "tokens":0}
    synthesis={**state.get("synthesis", {})}
    if synthesis:
        synthesis["specialists"]=[{k:v for k,v in f.items() if k!="seconds" and v not in ([], "")}
                                 for f in synthesis.get("specialists",[])]
    payload = {"question":state["query"], "assumption":state.get("assumption", ""),
               "evidence":evidence_payload(state), "missing":state.get("missing_evidence", []),
               "calculations":state.get("analysis_summary", {}),
               "specialist_analysis":synthesis,
               "review_feedback":state.get("validation_feedback", "")}
    result = await model_chain(llm, WRITE_SYSTEM, "writer").ainvoke(
        {"input":json.dumps(payload, ensure_ascii=False, separators=(",", ":"))})
    prefix = "※ 실습용 모의 데이터에 근거한 답변입니다.\n"
    if state.get("assumption"):
        prefix += state["assumption"] + "\n"
    usage = result["usage"]
    usage["prompt"] = result["prompt"]
    return prefix + result["value"], usage


# [검증 실패 대응] 검증되지 않은 원인/조치 대신 확보한 관측과 추가 확인 필요사항을 안내한다.
def fallback_report(state):
    facts=[]
    for c in state.get('collected',[]):
        if c['source']!='monitoring' or c.get('error'):
            continue
        d=c['data']
        facts.append(f"{d['system_name']}: 상태 {d['status']}, CPU {d.get('cpu_percent')}%, 메모리 {d.get('memory_percent')}%. 알림: {'; '.join(d.get('alerts',[])) or '없음'}.")
    return ('1) 모의 데이터 관측: '+(' '.join(facts) or '확보한 모니터링 자료가 없습니다.')+'\n'+state.get('assumption','')+
            '\n2) 검증을 완료하지 못해 원인과 조치를 확정하지 않습니다. '+
            '; '.join(state.get('missing_evidence',[]))+
            '\n3) 서비스별 로그·시간별 지표·배포 이력과 의존 관계를 추가 확인하세요. 재시작·증설·삭제·롤백은 운영 조건과 영향 확인 후 판단해야 합니다.')
