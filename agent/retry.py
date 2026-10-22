"""단계 9 — 일시적 오류만 대기 시간을 지수적으로 늘리며 재시도.

asyncio.TimeoutError(일시적)만 재시도하고, 그 외(ValueError 등 영구적)는 즉시 전파한다.
"""
import asyncio

from langsmith import traceable


@traceable(name="with_retry", process_inputs=lambda x: {k: v for k, v in x.items() if k not in ("self", "deps", "fn", "on_token")})
async def with_retry(fn, *args, retries: int = 3, base_delay: float = 0.3, **kwargs):
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await fn(*args, **kwargs)
            return result, attempt - 1   # (결과, 재시도 횟수)
        except asyncio.TimeoutError:
            if attempt >= retries:
                raise
            delay = base_delay * (3 ** (attempt - 1))
            await asyncio.sleep(delay)
