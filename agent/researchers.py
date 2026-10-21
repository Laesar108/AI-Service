"""Three compiled specialist subgraphs plus an explicit Supervisor join."""
from typing import TypedDict
import time
from langgraph.graph import StateGraph, START, END
from .chains import invoke_json
from .prompts import RESEARCH
from .telemetry import STAGE


class WorkerState(TypedDict, total=False):
    query: str
    evidence: list[dict]
    finding: dict


# [PDF 3] 단일 조사 역할을 metrics/logs/runbook의 컴파일된 전문 서브그래프로 분리했다.
def build_researcher(role, llm):
    async def prepare(state):
        # Tool outputs are bounded upstream, and only this worker's evidence is sent.
        return {"evidence": state.get("evidence", [])}

    async def analyze(state):
        evidence = state["evidence"]
        refs = [e["id"] for e in evidence]
        result = {"role": role, "status": "skipped", "observations": [],
                  "hypotheses": [], "conditions": [], "unknowns": ["분석 자료 미제공"],
                  "evidence_ids": refs}
        start = time.perf_counter()
        if evidence:
            try:
                # [토큰 최적화] 전문 호출만 240토큰으로 제한하고 길이/형식/근거 ID를 검사한다.
                # 잘림이나 위반은 분석 실패로 남기며 반복 호출로 비용을 계속 늘리지 않는다.
                value, _ = await invoke_json(llm, RESEARCH.format(role=role),
                    {"question": state["query"], "evidence": evidence}, f"researcher.{role}",
                    max_tokens=240)
                fields = ("observations","hypotheses","conditions","unknowns","evidence_ids")
                if not isinstance(value, dict) or not all(
                    isinstance(value.get(k), list) and len(value[k]) <= 3
                    and all(isinstance(s,str) for s in value[k]) for k in fields):
                    raise ValueError("Invalid researcher schema")
                if not set(value["evidence_ids"]) <= set(refs):
                    raise ValueError("Unknown evidence reference")
                if value["observations"] or any(len(value[k]) > 1 or
                        any(len(s) > 90 for s in value[k])
                        for k in ("hypotheses", "conditions", "unknowns")):
                    raise ValueError("Unbounded researcher output")
                result.update(value, status="completed")
            except Exception as exc:
                result.update(status="failed", unknowns=[f"분석 미완료: {type(exc).__name__}"])
        result["seconds"] = round(time.perf_counter()-start, 4)
        return {"finding": result}

    graph = StateGraph(WorkerState)
    graph.add_node("prepare", prepare)
    graph.add_node("analyze", analyze)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "analyze")
    graph.add_edge("analyze", END)
    return graph.compile(name=f"{role}_researcher")


# [입력 최적화] 각 전문가에게 필요한 자료만 전달해 불필요한 문맥과 중복 입력을 줄인다.
def select_evidence(state, role):
    selected = []
    has_runbooks = any(c["source"] == "runbook" and not c.get("error") for c in state["collected"])
    for item in state["collected"]:
        if item.get("error"):
            continue
        source, data = item["source"], item["data"]
        if role == "metrics" and source == "monitoring":
            selected.append({"id": item["id"], "data": data})
        elif role == "runbook" and source == "runbook":
            selected.append({"id": item["id"], "data": data})
        elif role == "runbook" and has_runbooks and source == "monitoring":
            selected.append({"id":item["id"], "data":data})
        elif source == "evidence":
            kinds = ("cpu_series",) if role == "metrics" else (("logs","deployments") if role == "logs" else ())
            for kind in kinds:
                part = data.get(kind, {})
                if part.get("availability") == "provided" and part.get("records"):
                    selected.append({"id": item["id"], "kind": kind, "data": part})
    return selected


def worker_node(role, llm):
    subgraph = build_researcher(role, llm)

    async def run(state):
        selected = state.get("research_selection", {}).get(role, {"enabled": True})
        # [선택적 호출] 그래프 노드는 지나가더라도 선택되지 않은 역할은 모델을 호출하지 않는다.
        if not selected["enabled"]:
            return {"findings": [{"role": role, "status": "skipped",
                "reason": "전문 분석 불필요: RAG와 원문 검증으로 처리", "seconds": 0.0}]}
        result = await subgraph.ainvoke({"query":state["query"],
                                        "evidence":select_evidence(state, role)})
        return {"findings": [result["finding"]]}
    return run


# [결과 취합] 생략 결과는 작성 문맥에서 제외하고 분석 실패/상충은 미확정으로 유지한다.
async def supervisor_join(state):
    findings = sorted(state.get("findings", []), key=lambda f: f["role"])
    # Preserve attributed hypotheses; majority agreement is never treated as evidence.
    active = [f for f in findings if f["status"] != "skipped"]
    synthesis = {"specialists": active,
                 "rule": "서로 충돌하는 해석은 미확정으로 병기하고 원문 근거로 검증",
                 "incomplete_roles": [f["role"] for f in active if f["status"] == "failed"]} if active else {}
    events = list(state.get("events", []))
    events += [{"type":"ResearcherCompleted","role":f["role"],"status":f["status"],
                "reason":f.get("reason", "자료 미제공" if f["status"] == "skipped" else "")} for f in findings]
    return {"synthesis": synthesis, "events":events}


# [병렬 경로 개선] 검색→Runbook 분석을 한 서브그래프로 묶어 지표/로그 완료를
# 불필요하게 기다리지 않도록 했다. 최종 작성 전에는 모든 분기의 결과를 합류시킨다.
def runbook_branch(deps):
    """Keep retrieval -> runbook within one branch to avoid a sibling superstep barrier."""
    from .state import AgentState
    from functools import partial

    async def choose(state):
        selection = {k: dict(v) for k, v in state["research_selection"].items()}
        if len(state.get("retrieval", [])) > 1 and not selection["runbook"]["enabled"]:
            selection["runbook"] = {"enabled": True, "reasons": ["복수 검색 후보의 적용성 확인"]}
        return {"research_selection": selection}

    graph = StateGraph(AgentState)
    graph.add_node("retrieve", partial(retrieve_node, deps=deps))
    graph.add_node("select_runbook", choose)
    graph.add_node("runbook_researcher", worker_node("runbook", deps.fast_llm))
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "select_runbook")
    graph.add_edge("select_runbook", "runbook_researcher")
    graph.add_edge("runbook_researcher", END)
    subgraph = graph.compile(name="runbook_pipeline")

    async def run(state):
        # Reset the subgraph reducer input; return only this branch's findings.
        result = await subgraph.ainvoke({**state, "findings": []})
        return {k: result[k] for k in ("collected", "retrieval", "missing_evidence",
                                      "events", "research_selection", "findings")}
    return run


# [PDF 1] 질문과 서비스별 알림을 각각 검색해 복합 질문의 일부 증상이 묻히는 문제를 줄인다.
async def retrieve_node(state, deps):
    metrics = [c["data"] for c in state["collected"] if c["source"] == "monitoring" and not c.get("error")]
    # Separate observation queries keep a composite question from drowning out one symptom.
    queries = [state["query"]] + [
        "관측 알림: " + "; ".join(m.get("alerts", [])) for m in metrics if m.get("alerts")]
    token = STAGE.set("retrieval")
    collected = list(state["collected"])
    gaps = list(state.get("missing_evidence", []))
    records = []
    try:
        if deps.retriever is None:
            raise RuntimeError("Retriever unavailable")
        docs = await deps.retriever.search(queries)
        for d in docs:
            collected.append({"id":f"runbook:{d['id']}", "source":"runbook",
                              "target":d["incident_type"], "data":d["text"],
                              "metadata":{k:v for k,v in d.items() if k!="text"}})
        records = [{k:v for k,v in d.items() if k!="text"} for d in docs]
        if not docs:
            gaps.append("관련 매뉴얼 검색 근거 부족")
    except Exception as exc:
        gaps.append(f"의미 검색 실패: {type(exc).__name__}")
    finally:
        STAGE.reset(token)
    return {"collected":collected, "retrieval":records, "missing_evidence":gaps,
            "events":state.get("events",[])+[{"type":"RunbooksRetrieved",
                "topics":[r["incident_type"] for r in records]}]}
