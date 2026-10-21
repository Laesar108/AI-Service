# 현재 개선 내용과 평가 자료

정리일: 2026-10-21. 비교 기준은 **최초 원본 ↔ 현재 버전**이다. 중간 실험 보고서와 스냅샷은 프로젝트 밖 보관 파일로 옮겼다.

## 현재 구현

1. 모듈화: `agent/graph.py`가 흐름을 연결하고 `agent/nodes/`, `state.py`, `policies.py`, `reporting.py`, `retry.py`가 상태·노드·검증·응답·재시도 역할을 나눈다.
2. 대상 확인과 근거 보강: 질문의 조사 대상을 확인하고 관측·로그·배포·시계열 자료를 수집한다. 미제공 자료를 이상 없음으로 간주하지 않도록 가용성을 구분한다.
3. 그래프 검증: 작성 후 근거 ID와 조건 등을 검사하고 필요 시 재작성한다. 검증을 통과하지 못하면 제한 답변으로 전환한다.
4. RAG: 벡터 유사도로 여러 Runbook을 검색하고 선택된 원문과 근거 ID를 작성 단계에 전달한다. 검색 설정과 검색 평가 자료는 유지한다.
5. LangGraph와 LCEL: 분기·합류·서브그래프는 LangGraph로, 프롬프트·모델·파서 연결은 LCEL로 구성한다.
6. 선택적 전문 분석: 지표·로그·Runbook 전문가 중 필요한 역할만 호출한다. 여러 서비스, 시계열, 로그/배포 기록, 구조화된 관측 충돌, 복수 검색 후보를 선택 근거로 사용한다.
7. 토큰과 지연 최적화: 전문 분석은 짧은 구조화 출력으로 제한한다. 지표·로그 분석과 Runbook 검색/분석 분기를 병렬로 실행한 뒤 합류한다.
8. 평가: 동일한 고정 사례로 답변 품질, 대상/검색 범위, 토큰, 추정 비용, 지연시간, 재작성·도구 재시도를 기록한다. 비교 도구는 최초 원본과 현재 코드만 실행하도록 정리했다.

현재 그래프는 [선택적 멀티에이전트](langgraph_selective_multi.mmd), RAG 단독 경로는 [RAG 그래프](langgraph_rag_lcel.mmd), 최초 구조는 [Before 그래프](langgraph_before.mmd)에 있다. 각 전문 역할의 그래프는 `subgraph_metrics.mmd`, `subgraph_logs.mmd`, `subgraph_runbook.mmd`다.

현재 실행 모드는 `RESEARCH_MODE=multi`이며 내부에서 필요한 전문 호출을 선택한다. `rag`와 기존 호환 모드도 유지한다. UI 확인 자료는 [현재 화면](selective_multi_ui.png)에 있다.

## 남겨 둔 측정 결과

- 최초 원본: `eval/results/before-live/`. 기본 질문 33개 응답과 당시 평가 기록을 그대로 보존했다.
- 현재 버전: `eval/results/current/`. 기본 33개와 경계 사례 15개, 총 48개 응답·채점 기록을 보존했다. `responses_readable.md`는 첫 반복 원문이며 `responses.jsonl`에 전체 기록이 있다.
- 검색 평가: `eval/results/retrieval/`. 현재 임계값/검색 개수 설정의 근거를 유지한다.

현재 버전의 기본 33개 응답 측정값은 **77,093토큰, 추정 비용 $0.201955, 응답시간 중앙값 5.2379초, p95 8.4307초**다. 질문 임베딩을 포함하며 평가자 비용은 제외한다. 전문 분석은 18회, 답변 재작성과 도구 재시도는 각 0회다. 장애를 주입하지 않았으므로 재시도 내성을 의미하지 않는다.

이 수치는 현재 버전의 보존 측정값이며 최초 대비 개선율이 아니다. 최초 결과와 실행 시점·비용 계측·채점 지침이 달라, 동일 조건의 최초/현재 비교는 정리한 평가 도구로 다시 실행해야 한다. 이번 파일 정리에서는 API 평가를 실행하지 않았다.

## 남은 품질 문제

기본 장애 질문 21개는 자동 평가에서 근거·조건·행동 적절성 모두 통과했지만, 일부 원문에는 자동 평가가 놓친 오류가 있다.

- 메모리 90.1%만으로 OOM 직전을 단정하거나 낮은 CPU만으로 원인을 배제한 응답.
- 매뉴얼의 CPU 90% 초과를 90% 이상으로 바꾼 응답.
- 중복 배치 사례에서 증설의 전제 확인을 충분히 연결하지 않은 응답.

평가자가 답변 대신 매뉴얼 문장을 인용하는 등 채점 오류도 있었다. `eval/results/current/manual_quality_notes.json`에 현재 버전 관련 항목을 보존했다. 자동 점수를 품질 완성이나 오류 없음으로 해석하지 않는다.

실행·채점·집계 방법은 [평가 안내](../eval/README.md), 정리한 파일과 보관 위치는 [정리 기록](cleanup_record.md)을 참고한다.

## 개선 항목별 코드 확인 위치

아래는 기본 파일에서 변경하거나 추가한 기능의 위치다. 코드의 `[PDF 1]`, `[PDF 2·3]` 등은 과제 개선 예시 번호이며, `[추가 최적화]`, `[검증 개선]` 등의 주석은 예시 외 작업의 목적을 설명한다. 주석은 기존 동작 또는 문제, 변경 방식, 기대 효과와 필요한 한계를 함께 기록했다.

1. **RAG:** [retrieval.py](../agent/retrieval.py)의 문서 임베딩·SQLite 저장·유사도 검색을 추가했다. [researchers.py](../agent/researchers.py)의 `retrieve_node`에서 질문과 관측 알림별 검색 결과를 합친다. 검색어별 `top_k=1`이어도 여러 검색어의 결과를 합쳐 다중 Runbook을 얻을 수 있다.
2. **모듈화:** [graph.py](../agent/graph.py)는 연결을 담당하고, [intake.py](../agent/nodes/intake.py), [research.py](../agent/nodes/research.py), [answer.py](../agent/nodes/answer.py)로 노드를 분리했다. [state.py](../agent/state.py)에 근거·검증·전문 분석 상태를 모았다.
3. **Research 멀티에이전트화:** [researchers.py](../agent/researchers.py)의 `build_researcher`, `worker_node`, `supervisor_join`에 전문 서브그래프 생성·호출·합류를 구현했다. 전문가의 의견을 원문 근거보다 우선하지 않도록 구성했다.
4. **Human-in-the-loop:** 미구현이다. 자동 Reviewer와 Fallback은 사람 승인 노드가 아니다.
5. **대화 Context 요약:** 미구현이다. 전문 분석 출력 제한과 입력 축소를 대화 요약 저장으로 설명하지 않는다.
6. **평가:** [run_evaluation.py](../eval/run_evaluation.py)에 최초/현재 고정 입력 실행, [score_responses.py](../eval/score_responses.py)에 LLM 평가, [summarize_results.py](../eval/summarize_results.py)에 사용량·지연·품질 집계를 구현했다. [evaluate_retrieval.py](../eval/evaluate_retrieval.py)의 검색 구성요소 평가와 최초 전체 그래프 비교는 별개의 실험이다.
7. **하드코딩 개선:** [intake.py](../agent/nodes/intake.py)의 `target_node`와 [policies.py](../agent/policies.py)의 `identify_targets`가 임의 기본 서비스 대체를 방지한다. 로그인→auth-service 가정 등 일부 규칙은 남아 있어 부분 적용이다.
8. **데이터 보강:** [mcp_server.py](../mcp_server.py)의 `get_diagnostic_evidence`를 추가하고 [research.py](../agent/nodes/research.py)에서 제공 여부를 수집한다. 고정 사례와 자료의 무결성은 [validate_dataset.py](../eval/validate_dataset.py)에서 검사한다. JSON 데이터에는 주석을 삽입하지 않아 표준 형식과 기존 평가 입력을 보존했다.

### 추가 개선 코드

- **LCEL:** [chains.py](../agent/chains.py)에 프롬프트→모델→파서를 공통화하고, [reporting.py](../agent/reporting.py)에서 원문 근거와 사용량을 유지하도록 했다.
- **답변/검증 강화:** [prompts.py](../agent/prompts.py), [policies.py](../agent/policies.py), [answer.py](../agent/nodes/answer.py)에 사실·가설 구분, 조건 검토, 복수 근거 ID 처리와 검증 실패 시 제한 답변을 구현했다.
- **선택적 호출:** [selection.py](../agent/selection.py)와 [researchers.py](../agent/researchers.py)에서 자료 기반 역할 선택과 LLM 호출 생략을 처리한다.
- **토큰/병렬 최적화:** [researchers.py](../agent/researchers.py)에서 역할별 입력·출력을 제한하고 검색→Runbook 분석을 독립 분기로 묶었다. [openai_llm.py](../agent/openai_llm.py)의 출력 제한은 이를 요청한 전문 호출에만 적용한다.
- **계측:** [telemetry.py](../agent/telemetry.py)와 [openai_llm.py](../agent/openai_llm.py)에 입력/출력/캐시 토큰별 추정 비용과 호출 구간을 기록한다. 기존 가상 단가 `usage.usd`와 비교용 `estimated_usd`를 구분한다.
- **실행/화면 연결:** [server.py](../server.py)에서 검색기를 주입하고, [web_chat.html](../web_chat.html)에서 대상 확인·검색·분석 생략·검증 실패 상태를 표시한다.

### 이번 주석 작업의 범위

2026-10-21에 위 코드 23개 파일에 설명 주석만 추가했다. 프롬프트 문자열·실행 로직은 변경하지 않았다. 최초 기준 코드, 고정 데이터, 과거 평가 결과는 수정하지 않았다. 주석 추가로 현재 소스의 파일 해시는 과거 평가 메타데이터의 해시와 달라질 수 있지만, 기존 측정 당시의 메타데이터는 그대로 유지한다. 이번 작업에서 성능 평가를 다시 실행한 것은 아니다.
