"""고정 입력으로 실제 LLM을 평가한다. 비밀 정보는 로컬에서 불러오되 결과에 직렬화하지 않는다."""
import argparse
import ast
import asyncio
import copy
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.validate_dataset import validate


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def prompt_function():
    tree = ast.parse((ROOT / 'eval/baseline/mcp_server.py').read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'incident_analysis_prompt')
    fn.decorator_list = []
    namespace = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<frozen-prompt>', 'exec'), namespace)
    return namespace['incident_analysis_prompt']


# [고정 평가 환경] 동일한 질문/관측/매뉴얼을 공급하는 평가 전용 MCP 대체 구현이다.
# 난수·네트워크·의도적 도구 장애를 제외하므로 브라우저 전체 지연 측정과 구분한다.
class FixtureMCP:
    def __init__(self, dataset, case, fixture):
        self.dataset, self.case, self.fixture = dataset, case, fixture
        self.calls = []
        self.prompt = prompt_function()

    async def find_prompt(self, name):
        if name != 'incident_analysis_prompt':
            raise ValueError('Unknown prompt')
        return 'fixture'

    async def get_prompt(self, name, arguments, server='fixture'):
        return self.prompt(**arguments)

    async def find_resource_template(self, name):
        if name != 'get_incident_runbook':
            raise ValueError('Unknown resource')
        return 'fixture', 'runbook://incident-response/{incident_type}'

    async def call_tool(self, name, arguments, server='fixture'):
        system = arguments['system_name']
        if name == 'get_monitoring_status':
            result = copy.deepcopy(self.dataset['monitoring'].get(system, {
                'system_name': system, 'status': 'unknown', 'cpu_percent': 0.0,
                'memory_percent': 0.0, 'last_checked': self.dataset['observed_at'],
                'alerts': [f"System '{system}' not found in monitoring"]}))
            result.update(self.case['monitoring_overrides'].get(system, {}))
        elif name == 'get_diagnostic_evidence':
            result = {'system_name': system, 'data_kind': 'synthetic'}
            for kind, evidence in self.case['additional_evidence'].items():
                result[kind] = {'availability': evidence['availability'],
                    'records': [r for r in evidence['records'] if r['system_name'] == system]}
        else:
            raise ValueError('Unknown tool')
        self.calls.append({'tool': name, 'arguments': arguments, 'result': copy.deepcopy(result)})
        return result

    async def read_resource(self, uri, server='fixture'):
        topic = uri.rsplit('/', 1)[-1]
        allowed = {p.stem for p in (self.fixture / 'runbooks').glob('*.md')}
        result = ((self.fixture / 'runbooks' / f'{topic}.md').read_text()
                  if topic in allowed else f'# Runbook Not Found\n\nNo runbook for incident type: {topic}')
        self.calls.append({'resource': uri, 'result': result})
        return result


def safe(value):
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()
                if str(k).lower() not in ('self', 'deps', 'fn', 'on_token', 'api_key', 'authorization', 'headers')}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    if isinstance(value, str):
        for secret in SECRETS:
            value = value.replace(secret, '[REDACTED]')
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return f'<{type(value).__name__}>'


SECRETS = []


# [Before 보존] 중간 개선판이 아닌 최초 코드만 로드한다. 보존 파일은 수정하지 않는다.
def frozen_modules():
    """원본 파일을 수정하지 않고 불러오며 공통 의존성을 연결한다."""
    graph = load_module("agent.original_graph", ROOT/"eval/baseline/graph.py")
    llm = load_module("agent.original_openai", ROOT/"eval/baseline/openai_llm.py")
    return graph, llm


async def main(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT/".env")
    SECRETS.extend(v for k,v in os.environ.items()
                   if any(t in k.upper() for t in ("KEY","TOKEN","SECRET","PASSWORD")) and len(v)>7)
    logging.disable(logging.CRITICAL)
    from langsmith import Client, tracing_context
    from agent.openai_llm import OpenAILLM, OpenAIFastLLM, OpenAIQualityLLM
    from agent.deps import SupervisorDeps
    from agent.graph import build_graph
    from agent.retrieval import configured_retriever
    from agent.telemetry import CALLS, STAGE, RATES, PRICING_DATE, PRICING_SOURCE
    frozen_graph, frozen_llm=frozen_modules()
    # 초기의 프롬프트, 판단 로직과 그래프는 유지한다. 동일한 요청 내용으로
    # 입력·출력·캐시 토큰을 계측하기 위해 API 통신 구현만 공유한다.
    class FrozenFast(frozen_llm.OpenAIFastLLM):
        _chat=OpenAILLM._chat
    class FrozenQuality(frozen_llm.OpenAIQualityLLM):
        _chat=OpenAILLM._chat
    dataset=validate(ROOT/"eval/fixtures/v1")
    frozen_graph.KNOWN_SYSTEMS=set(dataset["known_systems"])
    frozen_llm.KNOWN_SYSTEMS=set(dataset["known_systems"])
    cases=dataset["cases"] if args.extended else dataset["cases"][:10]
    if args.case:
        cases=[c for c in cases if c["id"] in args.case]
    if args.repeats < 1 or not cases or len(set(args.variants)) != len(args.variants):
        raise ValueError("Require positive repeats, matching cases and unique variants")
    out=ROOT/args.output;out.mkdir(exist_ok=False,parents=True)
    variants=args.variants
    for variant in variants: (out/variant).mkdir()
    fast_model=os.getenv("OPENAI_FAST_MODEL","gpt-4o-mini")
    quality_model=os.getenv("OPENAI_QUALITY_MODEL","gpt-4o")
    retriever=configured_retriever(ROOT/"eval/fixtures/v1/runbooks")
    # 문서 집합의 해시가 같으면 인덱스를 재사용하며 인덱스 생성 비용은 요청 지연시간에서 제외한다.
    build_calls=[];CALLS.set(build_calls);STAGE.set("index_build")
    if "after" in variants:
        await retriever.ensure_index()
    metadata={
        "started_at":datetime.now(timezone.utc).isoformat(),"variants":variants,
        "models":[fast_model,quality_model],"temperature":0,"repeats":args.repeats,
        "case_ids":[c["id"] for c in cases],"order":"rotated per case and repetition",
        "transport":"fixed in-process MCP; no injected failure or jitter",
        "baseline":"eval/baseline; original graph and LLM behavior, shared metered API transport",
        "comparison":"before=unmodified original; after=current selective RAG multi-agent",
        "pricing":{"date":PRICING_DATE,"source":PRICING_SOURCE,"usd_per_million":RATES,
                   "note":"estimate, not invoice; cached input discounted when API reports it"},
        "retrieval":json.loads((ROOT/"data/retrieval_config.json").read_text()),
        "index_usage":build_calls,
        "dataset_manifest":json.loads((ROOT/"eval/fixtures/v1/manifest.json").read_text()),
        "baseline_hashes":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted((ROOT/"eval/baseline").glob("*.py"))},
        "source_hashes":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted((ROOT/"agent").rglob("*.py"))},
    }
    (out/"metadata.json").write_text(json.dumps(safe(metadata),ensure_ascii=False,indent=2))
    client=Client(hide_inputs=safe,hide_outputs=safe)
    count=0
    try:
        for repetition in range(1,args.repeats+1):
            for index,case in enumerate(cases):
                # [공정 비교] 같은 사례의 실행 순서를 교대로 바꿔 시간대/순서 영향을 완화한다.
                offset=(index+repetition-1)%len(variants)
                for variant in variants[offset:]+variants[:offset]:
                    fast_cls,quality_cls=(FrozenFast,FrozenQuality) if variant=="before" else (OpenAIFastLLM,OpenAIQualityLLM)
                    fast,quality=fast_cls(fast_model,.00015),quality_cls(quality_model,.005)
                    history=[]
                    for turn,query in enumerate(case["turns"],1):
                        calls=[];calls_token=CALLS.set(calls);stage_token=STAGE.set("legacy")
                        mcp=FixtureMCP(dataset,case,ROOT/"eval/fixtures/v1")
                        deps=SupervisorDeps(fast,quality,mcp,None,"v2",
                            research_mode="keyword" if variant=="before" else "multi",
                            retriever=retriever)
                        graph=frozen_graph.build_graph(deps) if variant=="before" else build_graph(deps)
                        state={"query":query,"history":history.copy(),"thread_id":f"{variant}-{case['id']}-{repetition}",
                               "events":[],"cost_usd":0.0,"findings":[]}
                        nodes=[];start=time.perf_counter()
                        record={"variant":variant,"case_id":case["id"],"repeat":repetition,"turn":turn,"query":query,"started_at":start}
                        try:
                            with tracing_context(client=client,tags=["original-current-comparison",variant],
                                                 metadata={"case_id":case["id"],"repeat":repetition}):
                                async for update in graph.astream(state,stream_mode="updates"):
                                    for node,delta in update.items():
                                        nodes.append(node)
                                        if "findings" in delta:
                                            state["findings"]+=delta["findings"]
                                        state.update({k:v for k,v in delta.items() if k!="findings"})
                            record.update(status="ok",final=state.get("final",""),state=state,nodes=nodes)
                            history.extend([{"role":"user","content":query},{"role":"assistant","content":record["final"]}])
                        except Exception as exc:
                            record.update(status="error",error_type=type(exc).__name__,state=state,nodes=nodes,final="")
                        finally:
                            seconds=time.perf_counter()-start
                            CALLS.reset(calls_token);STAGE.reset(stage_token)
                        tool_calls=list(mcp.calls)
                        existing={c.get("resource") for c in tool_calls}
                        for c in state.get("collected",[]):
                            if c.get("source") != "runbook" or not c.get("target"):
                                continue
                            uri="runbook://incident-response/"+c["target"]
                            if c["source"]=="runbook" and uri not in existing:
                                tool_calls.append({"resource":uri,"result":c["data"],"via":"vector_retrieval"})
                        costs=[c["estimated_usd"] for c in calls]
                        record.update(seconds=round(seconds,4),tool_calls=tool_calls,llm_calls=calls,
                            total_tokens=sum(c["usage"]["tokens"] for c in calls),
                            chat_tokens=sum(c["usage"]["tokens"] for c in calls if c["kind"]=="chat"),
                            embedding_tokens=sum(c["usage"]["tokens"] for c in calls if c["kind"]=="embedding"),
                            estimated_usd=sum(costs) if all(c is not None for c in costs) else None)
                        with (out/variant/"responses.jsonl").open("a") as f:
                            f.write(json.dumps(safe(record),ensure_ascii=False)+"\n")
                        count+=1
                        print(f"{variant} {case['id']} r{repetition} t{turn} {record['status']} {seconds:.2f}s tokens={record['total_tokens']}",flush=True)
                        if record["status"]!="ok":
                            raise RuntimeError("Comparison stopped; safe error recorded")
    finally:
        client.flush()
        (out/"completion.json").write_text(json.dumps({"records":count,"finished_at":datetime.now(timezone.utc).isoformat()}))


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--output",required=True)
    p.add_argument("--variants",nargs="+",choices=["before","after"],default=["before","after"])
    p.add_argument("--repeats",type=int,default=3)
    p.add_argument("--extended",action="store_true")
    p.add_argument("--case",action="append")
    try: asyncio.run(main(p.parse_args()))
    except (Exception,KeyboardInterrupt) as exc:
        print("Comparison stopped: "+type(exc).__name__+"; sensitive details suppressed.");sys.exit(1)
