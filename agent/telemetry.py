"""요청별 계측 정보. 인증 정보나 인증된 클라이언트는 기록하지 않는다."""
from contextvars import ContextVar
import time

# [추가 계측] 요청별 호출 기록을 분리하고 병렬 분기에서도 같은 요청의 사용량을 수집한다.
CALLS = ContextVar("agent_calls", default=None)
STAGE = ContextVar("agent_stage", default="legacy")
PRICING_DATE = "2026-10-20"
PRICING_SOURCE = "https://developers.openai.com/api/docs/models/"
# 100만 토큰당 표준 미국 달러 단가: 입력, 캐시된 입력, 출력 순서.
RATES = {
    "gpt-4o": (2.5, 1.25, 10.0),
    "gpt-4o-mini": (0.15, 0.075, 0.6),
    "text-embedding-3-small": (0.02, 0.02, 0.0),
}


# [비용 비교] 입력·캐시·출력 토큰별 추정 비용이다. 단가/사용량을 모르면 0원 대신 None을 반환한다.
def price(model, usage):
    rates = RATES.get(model)
    if rates is None:
        return None
    incoming = usage.get("input_tokens")
    outgoing = usage.get("output_tokens")
    if incoming is None or outgoing is None:
        return None
    cached = min(incoming, usage.get("cached_input_tokens", 0))
    return ((incoming-cached)*rates[0]+cached*rates[1]+outgoing*rates[2])/1_000_000


# [지연 분석] 호출별 시간과 실행 구간을 기록해 검색/전문 분석의 실제 겹침을 확인한다.
def record(model, usage, seconds, **details):
    calls = CALLS.get()
    if calls is not None:
        stage = STAGE.get()
        if stage == "legacy":
            from langsmith.run_helpers import get_current_run_tree
            run = get_current_run_tree()
            if run is not None:
                stage = run.name
        finished = time.perf_counter()
        calls.append({
            "stage": stage, "model": model, "usage": usage,
            "started_at": finished-seconds, "finished_at": finished,
            "seconds": seconds, "estimated_usd": price(model, usage), **details,
        })
