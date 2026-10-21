# 최초 원본과 현재 버전 평가

비교 기준은 **개선 전 최초 원본 ↔ 현재 선택적 멀티에이전트**다. 중간 단계는 비교 기준으로 사용하지 않는다.

## 남겨 둔 자료

- `baseline/`: 최초 `graph.py`, `openai_llm.py`, `mcp_server.py` 원본. 평가 시 수정하지 않는다.
- `fixtures/v1/`: 고정 모의 관측·추가 근거·매뉴얼과 질문별 기준 15개. 기본 질문은 10개다.
- `results/before-live/`: 최초 원본의 기존 실모델 평가 33개 응답과 당시 채점·메타데이터. 파일 내용 그대로 보존했다.
- `results/current/`: 현재 버전의 기존 실모델 평가 48개 응답과 채점, 메타데이터, 요약, 원문, 남은 품질 문제. 기본 질문 33개와 추가 경계 사례 15개로 나뉜다.
- `results/retrieval/`, `retrieval_cases.json`: 현재 검색 설정을 결정한 개발/보류 질문의 검색 평가 자료.
- `run_evaluation.py`, `score_responses.py`, `summarize_results.py`: 실행 → 채점 → 집계 도구를 한 세트로 통합했다.
- `validate_dataset.py`: 고정 데이터 무결성 검사.
- `evaluate_retrieval.py`: 검색 설정 보정에 사용한 도구. 현재 검색 평가 폴더가 있으면 덮어쓰지 않고 중단한다. 단순 성능 비교를 위해 다시 보정할 필요는 없다.

최초 화면 응답은 [Before 기록](../doc/before_responses_2026-10-20.md), 현재 구현 설명은 [개선 보고서](../doc/improvement_report.md), 정리 내역은 [파일 정리 기록](../doc/cleanup_record.md)에 있다.

## 보존 결과를 해석할 때

기존 `before-live`와 `current`는 서로 다른 시점에 실행했고 채점 지침도 다르다. 최초 결과에는 실제 입력/출력 토큰별 비용 계측이 없으므로 기존 `cost_usd`를 현재 `estimated_usd`와 비교하면 안 된다. 현재 폴더의 단독 요약을 최초 대비 개선율로 해석하지 않는다.

현재 결과는 과거 3개 구성 비교에서 **after 응답만 바이트 그대로 추출**한 것이다. 당시 before는 항상 전문 분석을 실행하던 중간 버전이었다. 이 내역은 current 메타데이터의 `original_benchmark_*`에 남겼으며 최초 Before로 바꾸지 않았다. 정리 과정에서 API 재실행이나 재채점은 하지 않았다.

## 앞으로 동일 조건으로 비교하는 방법

프로젝트 루트에서 새 결과 폴더 이름을 사용한다.

```sh
.venv/bin/python eval/validate_dataset.py
.venv/bin/python eval/run_evaluation.py --output eval/results/original-current --extended --repeats 3
.venv/bin/python eval/score_responses.py eval/results/original-current/before eval/results/original-current/after
.venv/bin/python eval/summarize_results.py --input eval/results/original-current
```

`--extended`를 빼면 기본 10개 질문만 실행한다. 이름 기억 질문은 두 턴이라 기본은 구성별 33개, 추가 사례 포함 시 48개 응답이다. `--variants before` 또는 `--variants after`로 한쪽만 실행할 수도 있다. `--case q01`처럼 범위를 제한할 수 있다.

- before는 `baseline/`의 최초 그래프와 LLM 판단 로직을 사용한다. 원본 경로 이동에 따른 시스템 목록만 고정 데이터로 복원한다. API 요청 계측을 위해 현재의 전송 메서드를 공유하며 원본 프롬프트·온도·JSON 모드는 유지한다.
- after는 현재 `agent/` 코드의 `multi` 경로를 사용한다. RAG와 선택적 전문 분석·출력 축소·병렬 실행을 포함한다.
- 질문/반복마다 실행 순서를 번갈아 배치하고 같은 모델·고정 자료를 사용한다. 세션은 각각 분리하며 이름 기억의 두 턴만 이력을 공유한다.
- 모니터링은 고정 모의 데이터다. 모델·임베딩 호출, 토큰 수, 처리 시간은 실제 API에서 측정한다.
- MCP 네트워크와 의도적인 도구 장애 주입은 제외된다. 최초 그래프의 조사 지연은 원본대로 유지한다. 브라우저 전체 응답시간 측정은 아니다.
- 인덱스 생성 비용은 별도 메타데이터에 기록하고 요청 시간에서 제외한다. 질문 임베딩은 요청 측정에 포함한다.
- 입력·출력·캐시 토큰별 비용은 코드에 기록된 단가로 계산한 추정치이며 청구서 금액은 아니다. 평가자 호출 비용은 서비스 처리 비용과 분리한다.
- 채점 지침은 양쪽에 동일하게 적용하지만 자동 채점에는 오판이 있다. 원문과 `manual_quality_notes.json`도 함께 확인한다.

실제 평가/채점 명령은 API 요금이 발생한다. 실행 시에만 허용된 `.env` 설정을 로드하며 키·헤더를 출력하지 않는다. LangSmith 설정은 유지하고 인증 객체는 기록에서 제외한다. 질문·근거·응답은 설정된 추적 서비스에 전송될 수 있다. 데이터 검사와 결과 집계는 `.env`를 읽지 않는다.
