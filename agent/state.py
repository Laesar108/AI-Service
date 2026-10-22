"""독립적으로 테스트할 수 있는 그래프 노드들이 공유하는 상태 구조."""
from typing import TypedDict, Annotated
import operator


# [공유 상태 정의] 분리된 노드의 공유 상태를 명시했다. 근거 누락·검증·검색·전문 분석 상태를 추가했다.
class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    route: str
    targets: list[str]
    unknown_targets: list[str]
    assumption: str
    needs_analysis: bool
    tasks: list[dict]
    collected: list[dict]
    missing_evidence: list[str]
    analysis_summary: dict
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: str
    final: str
    last_prompt: str
    cost_usd: float
    validation_attempts: int
    validation_feedback: str
    validation_errors: list[str]
    validation_status: str
    events: list[dict]
    # [병렬 합류] 여러 전문가가 동시에 반환한 결과를 덮어쓰지 않고 누적한다.
    findings: Annotated[list[dict], operator.add]
    research_plan: list[dict]
    research_selection: dict
    retrieval: list[dict]
    synthesis: dict
