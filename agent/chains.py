"""LCEL: 프롬프트 → 모델 어댑터 → 파서로 연결하고 결과와 사용량을 함께 유지한다."""
import asyncio
import inspect
import json
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from .telemetry import STAGE


# [추가 LCEL] 프롬프트→모델 어댑터→파서를 공통화하되 출력과 usage를 함께 유지한다.
# 동기 모델 호출은 별도 스레드에서 실행해 병렬 분기의 이벤트 루프를 막지 않도록 한다.
def model_chain(llm, system, name, *, json_output=False, schema=None, max_tokens=None):
    # 부분 변수를 사용해 JSON 예시의 중괄호가 템플릿 변수로 해석되지 않도록 한다.
    prompt = ChatPromptTemplate.from_messages([
        ("system", "{instructions}"), ("human", "{input}")
    ]).partial(instructions=system)

    async def call_model(value):
        messages = value.to_messages()
        instruction, user = messages[0].content, messages[1].content
        token = STAGE.set(name)
        try:
            if not json_output and hasattr(llm, "_chat_stream"):
                text, usage = await llm._chat_stream(instruction, user)
            else:
                kwargs = {}
                parameters = inspect.signature(llm._chat).parameters
                if max_tokens is not None and "max_tokens" in parameters:
                    kwargs["max_tokens"] = max_tokens
                if json_output:
                    if "json_mode" in parameters:
                        kwargs["json_mode"] = True
                    elif "schema" in parameters:
                        kwargs["schema"] = schema or {"type": "object"}
                text, usage = await asyncio.to_thread(llm._chat, instruction, user, **kwargs)
            return {"text": text, "usage": dict(usage),
                    "prompt": f"[SYSTEM]\n{instruction}\n\n[USER]\n{user}"}
        finally:
            STAGE.reset(token)

    # [검증 개선] 잘린 JSON을 정상 결과로 보정하지 않고 예외로 전달한다.
    def parse_output(response):
        # JSON을 엄격히 파싱해 검토 결과가 잘린 경우 정상 응답으로 통과시키지 않는다.
        value = json.loads(response["text"]) if json_output else response["text"]
        return {"value": value, "usage": response["usage"], "prompt": response["prompt"]}

    return (prompt | RunnableLambda(call_model, name=f"{name}.model")
            | RunnableLambda(parse_output, name=f"{name}.parse")).with_config(run_name=name)


async def invoke_json(llm, system, payload, name, *, max_tokens=None):
    result = await model_chain(llm, system, name, json_output=True, max_tokens=max_tokens).ainvoke(
        {"input": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))})
    return result["value"], result["usage"]
