import asyncio
import json
import unittest
from unittest.mock import patch

from agent.selection import research_selection
from agent.researchers import build_researcher
from agent.chains import model_chain
from agent.policies import deterministic_errors
from tests import test_rag_multi as fixtures
DATA, LLM, Retriever = fixtures.DATA, fixtures.LLM, fixtures.Retriever


class SelectiveTests(unittest.IsolatedAsyncioTestCase):
    execute = fixtures.GraphTests.execute

    async def test_simple_incident_uses_no_specialist_llm(self):
        state, llm, _ = await self.execute(DATA["cases"][0])
        self.assertEqual(state["synthesis"], {})
        self.assertTrue(all(f["status"] == "skipped" for f in state["findings"]))
        self.assertFalse(any("전문 조사 에이전트" in s for s, _, _ in llm.calls))
        self.assertEqual(state["validation_status"], "passed")

    async def test_series_selects_only_metrics(self):
        state, _, _ = await self.execute(DATA["cases"][10])
        self.assertEqual([f["role"] for f in state["findings"] if f["status"] == "completed"], ["metrics"])

    async def test_real_logs_select_only_logs(self):
        state, _, _ = await self.execute(DATA["cases"][11])
        self.assertEqual([f["role"] for f in state["findings"] if f["status"] == "completed"], ["logs"])

    async def test_multiple_candidates_escalate_runbook_only(self):
        class Ambiguous(Retriever):
            async def search(self, queries):
                docs = await super().search(queries)
                return docs + [{**docs[0], "id": "other", "incident_type": "other"}]
        state, _, _ = await self.execute(DATA["cases"][0], retriever=Ambiguous())
        self.assertEqual([f["role"] for f in state["findings"] if f["status"] == "completed"], ["runbook"])

    async def test_runbook_can_start_while_metrics_still_running(self):
        metrics_started, runbook_started = asyncio.Event(), asyncio.Event()
        class Coordinated(Retriever):
            async def search(self, queries):
                await metrics_started.wait()
                return await super().search(queries)
        async def analyze(llm, system, payload, name, **kwargs):
            if name == "researcher.metrics":
                metrics_started.set()
                await runbook_started.wait()
            elif name == "researcher.runbook":
                runbook_started.set()
            return {"observations": [], "hypotheses": [], "conditions": [], "unknowns": [],
                    "evidence_ids": [payload["evidence"][0]["id"]]}, {}
        with patch("agent.researchers.invoke_json", analyze):
            state, _, _ = await asyncio.wait_for(
                self.execute(DATA["cases"][3], retriever=Coordinated()), timeout=3)
        self.assertTrue(metrics_started.is_set() and runbook_started.is_set())
        self.assertEqual(len(state["findings"]), 3)
        self.assertEqual(len({f["role"] for f in state["findings"]}), 3)


class PolicyTests(unittest.TestCase):
    def test_grouped_citations_validate_each_id(self):
        evidence = [{"id": "monitoring:a", "source": "monitoring", "target": "a", "data": {}},
                    {"id": "runbook:b", "source": "runbook", "target": "b", "data": ""}]
        self.assertEqual(deterministic_errors("관측 [ID: monitoring:a, runbook:b]", evidence), [])
        self.assertTrue(deterministic_errors("관측 [monitoring:a, runbook:unknown]", evidence))

    def test_conflicting_observations_enable_metrics_and_runbook(self):
        state = {"targets": ["a"], "collected": [{"source": "monitoring", "data": {
            "system_name": "a", "status": "healthy", "cpu_percent": 95}}]}
        policy = research_selection(state)
        self.assertTrue(policy["metrics"]["enabled"])
        self.assertTrue(policy["runbook"]["enabled"])
        self.assertFalse(policy["logs"]["enabled"])


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_budget_reaches_adapter(self):
        class BudgetLLM:
            def _chat(self, system, user, json_mode=False, max_tokens=None):
                self.budget = max_tokens
                return '{}', {}
        llm = BudgetLLM()
        await model_chain(llm, 'json', 'test', json_output=True, max_tokens=240).ainvoke({"input": "{}"})
        self.assertEqual(llm.budget, 240)

    async def test_verbose_specialist_output_is_not_forwarded(self):
        class Verbose(LLM):
            def _chat(self, *a, **kw):
                return json.dumps({"observations": [], "hypotheses": ["a"*91],
                    "conditions": [], "unknowns": [], "evidence_ids": ["m"]}), {}
        result = await build_researcher("metrics", Verbose()).ainvoke({
            "query": "q", "evidence": [{"id": "m", "data": {}}]})
        self.assertEqual(result["finding"]["status"], "failed")
