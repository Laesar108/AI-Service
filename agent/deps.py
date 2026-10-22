"""단계 13 — 의존성 주입.

노드 함수가 클로저의 지역 변수에 암묵적으로 기대지 않도록, 감독자가 쓰는 모든
의존성을 SupervisorDeps 하나에 명시적으로 담아 functools.partial로 각 노드에 주입한다.
파일을 어디로 옮겨도, 단위 테스트에서 모의 의존성으로 교체해도 동작이 같다.
"""
from dataclasses import dataclass
from typing import Any


@dataclass
class SupervisorDeps:
    fast_llm: Any        # 단계 10a — 분류/계획 등 짧은 단계용 저비용 모델
    quality_llm: Any     # 단계 10a — 최종 답변 작성용 고품질 모델
    mcp: Any             # 단계 2  — MCP 표준 입출력 클라이언트 (도구/리소스/프롬프트)
    session_store: Any   # 단계 12 — SQLite 세션 저장소
    version: str         # 단계 14 — SUPERVISOR_VERSION (v1/v2)
    # [RAG 연결] 기존 의존성 주입을 확장해 실행 모드와 검색기를 노드에 전달한다.
    # 테스트에서는 실제 임베딩/API 대신 대체 검색기를 주입할 수 있다.
    research_mode: str = "keyword"  # 실행 모드: keyword, rag, multi. 서버에서는 multi를 선택한다.
    retriever: Any = None
