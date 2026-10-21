import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from agent.chains import model_chain
from agent.deps import SupervisorDeps
from agent.graph import build_graph
from agent.policies import deterministic_errors
from agent.researchers import build_researcher
from agent.retrieval import RunbookRetriever
from agent.telemetry import price
from eval.run_evaluation import FixtureMCP
from eval.validate_dataset import validate

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/"eval/fixtures/v1"
DATA=validate(FIX)


class LLM:
    def __init__(self, bad_reference=False):
        self.calls=[]
        self.bad_reference=bad_reference

    def _chat(self, system, user, json_mode=False):
        self.calls.append((system,user,json_mode))
        if "전문 조사 에이전트" in system:
            evidence=json.loads(user)["evidence"]
            result={k:[] for k in ("observations","hypotheses","conditions","unknowns")}
            result["evidence_ids"]=["invented"] if self.bad_reference else list(dict.fromkeys(e["id"] for e in evidence))[:3]
        elif json_mode:
            result={"passed":True,"errors":[]}
        else:
            return "1) 모의 관측. 2) 원인 미확정. 3) 자료 추가 확인.",{"usd":0,"tokens":12}
        return json.dumps(result),{"usd":0,"tokens":15}


class Retriever:
    async def search(self, queries):
        return [{"id":"cpu-spike","incident_type":"cpu-spike","source":"cpu-spike.md",
                 "version":"v1","score":0.8,"text":(FIX/"runbooks/cpu-spike.md").read_text()}]


class Embeddings:
    model="test"
    def __init__(self): self.batches=[]
    async def embed(self, texts):
        self.batches.append(texts)
        return [[1,0] if "alpha" in s else [0,1] for s in texts]


class LCELTests(unittest.IsolatedAsyncioTestCase):
    async def test_literal_json_braces_and_usage_preserved_single_call(self):
        llm=LLM()
        result=await model_chain(llm,'literal {"a":1}',"test").ainvoke({"input":"{untrusted}"})
        self.assertEqual(len(llm.calls),1)
        self.assertEqual(llm.calls[0][:2],('literal {"a":1}',"{untrusted}"))
        self.assertEqual(result["usage"]["tokens"],12)

    async def test_malformed_json_rejected(self):
        class Broken(LLM):
            def _chat(self,*a,**kw): return '{"passed":true',{"usd":0}
        with self.assertRaises(json.JSONDecodeError):
            await model_chain(Broken(),"test","reviewer",json_output=True).ainvoke({"input":"x"})

    async def test_bad_worker_citation_fails_closed(self):
        result=await build_researcher("metrics",LLM(True)).ainvoke({
            "query":"q","evidence":[{"id":"monitoring:a","data":{"cpu":90}}]})
        self.assertEqual(result["finding"]["status"],"failed")

    async def test_missing_worker_evidence_skips_model(self):
        llm=LLM()
        result=await build_researcher("logs",llm).ainvoke({"query":"q","evidence":[]})
        self.assertEqual(llm.calls,[])
        self.assertEqual(result["finding"]["status"],"skipped")


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, case, mode="multi", retriever=None):
        llm=LLM();mcp=FixtureMCP(DATA,case,FIX)
        graph=build_graph(SupervisorDeps(llm,llm,mcp,None,"v2",mode,retriever or Retriever()))
        result=await graph.ainvoke({"query":case["turns"][0],"history":[],"events":[],"findings":[]})
        return result,llm,mcp

    async def test_parallel_workers_merge_all_results_and_skip_absent_logs(self):
        state,llm,_=await self.execute(DATA["cases"][3])
        self.assertEqual({f["role"] for f in state["findings"]},{"metrics","logs","runbook"})
        self.assertEqual(state["synthesis"]["incomplete_roles"],[])
        specialists=[json.loads(u)["evidence"] for s,u,j in llm.calls if "전문 조사 에이전트" in s]
        self.assertEqual(len(specialists),2)
        self.assertEqual(len(state["retrieval"]),1)

    async def test_provided_logs_activate_log_researcher(self):
        state,_,_=await self.execute(DATA["cases"][11])
        logs=next(f for f in state["findings"] if f["role"]=="logs")
        self.assertEqual(logs["status"],"completed")

    async def test_rag_only_does_not_call_specialists(self):
        state,llm,_=await self.execute(DATA["cases"][0],"rag")
        self.assertFalse(any("전문 조사 에이전트" in s for s,_,_ in llm.calls))
        self.assertEqual(state["retrieval"][0]["incident_type"],"cpu-spike")

    async def test_retrieval_failure_is_explicit(self):
        class Broken:
            async def search(self,q): raise ValueError("unavailable")
        state,_,_=await self.execute(DATA["cases"][0],retriever=Broken())
        self.assertTrue(any("검색 실패" in s for s in state["missing_evidence"]))
        self.assertEqual(next(f for f in state["findings"] if f["role"]=="runbook")["status"],"skipped")

    async def test_unknown_target_bypasses_retriever_and_workers(self):
        state,llm,mcp=await self.execute(DATA["cases"][7])
        self.assertEqual(llm.calls,[]);self.assertEqual(mcp.calls,[])
        self.assertIn("billing-service",state["final"])


class VectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistence_changes_deletion_and_no_query_answer_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);corpus=root/"docs";corpus.mkdir()
            (corpus/"a.md").write_text("alpha procedure")
            (corpus/"b.md").write_text("beta procedure")
            emb=Embeddings()
            retriever=RunbookRetriever(corpus,root/"v.sqlite",emb,top_k=2,threshold=.1,margin=.2)
            await retriever.ensure_index()
            await retriever.ensure_index()
            self.assertEqual(len(emb.batches),1)
            self.assertEqual([d["id"] for d in await retriever.search(["alpha"])],["a"])
            second=RunbookRetriever(corpus,root/"v.sqlite",emb)
            await second.ensure_index()
            self.assertEqual(len(emb.batches),2)  # No re-embedding persisted documents.
            (corpus/"b.md").write_text("beta changed")
            await second.ensure_index()
            self.assertEqual(emb.batches[-1],["beta changed"])
            (corpus/"b.md").unlink()
            await second.ensure_index()
            with second.connect() as db: self.assertEqual(db.execute("select count(*) from vectors").fetchone()[0],1)

    async def test_multi_query_dedup_and_abstention(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/"a.md").write_text("alpha")
            r=RunbookRetriever(root,root/"v.sqlite",Embeddings(),threshold=.5)
            self.assertEqual(len(await r.search(["alpha","alpha"])),1)
            self.assertEqual(await r.search(["beta"]),[])


class AccountingTests(unittest.TestCase):
    def test_cache_discount_and_unknown_model(self):
        u={"input_tokens":1000,"cached_input_tokens":500,"output_tokens":100}
        self.assertAlmostEqual(price("gpt-4o",u),.002875)
        self.assertIsNone(price("unknown",u))

    def test_negation_false_positive_regression(self):
        text="메모리 사용률이 낮으나, 메모리 관련 원인을 완전히 배제할 수 있는 근거는 아니다"
        self.assertEqual(deterministic_errors(text,[]),[])

    def test_unknown_final_citation_rejected(self):
        self.assertTrue(deterministic_errors("관측 [ID: monitoring:invented]",[]))


if __name__=="__main__": unittest.main()
