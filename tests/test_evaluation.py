"""Exercise original/current benchmark wiring without credentials or network."""
import asyncio
from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eval.run_evaluation import main, ROOT
from eval.summarize_results import main as summarize


class EvaluationWiringTests(unittest.TestCase):
    def test_original_and_current_records_use_same_meter_and_can_be_summarized(self):
        class Retriever:
            async def ensure_index(self):
                pass

            async def search(self, queries):
                return [{"id": "cpu-spike", "incident_type": "cpu-spike",
                         "source": "cpu-spike.md", "version": "v1", "score": .8,
                         "text": (ROOT/"eval/fixtures/v1/runbooks/cpu-spike.md").read_text()}]

        def post(url, **kwargs):
            payload = kwargs["json"]
            system = payload["messages"][0]["content"]
            if "planning" in system:
                content = json.dumps({"needs_analysis": False, "tasks": [
                    {"agent": "research", "target": "payments-service"}]})
            elif '"route"' in system:
                content = json.dumps({"route": "incident", "needs_analysis": False})
            elif "response_format" in payload:
                content = json.dumps({"passed": True, "errors": []})
            else:
                content = "1) 모의 관측 자료입니다. 2) 원인 미확정. 3) 추가 자료 확인 필요."
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/"comparison"
            args = SimpleNamespace(output=str(output), variants=["before", "after"],
                                   repeats=1, extended=False, case=["q01"])
            with patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test-placeholder",
                    "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"}), \
                 patch("dotenv.load_dotenv", return_value=False), \
                 patch("httpx.post", side_effect=post), \
                 patch("agent.retrieval.configured_retriever", return_value=Retriever()), \
                 patch("langsmith.Client"), \
                 patch("langsmith.tracing_context", side_effect=lambda **kwargs: nullcontext()), \
                 redirect_stdout(io.StringIO()):
                asyncio.run(main(args))
                summarize(SimpleNamespace(input=str(output)))
            metadata = json.loads((output/"metadata.json").read_text())
            self.assertEqual(metadata["variants"], ["before", "after"])
            self.assertIn("eval/baseline/graph.py", metadata["baseline_hashes"])
            for variant in ("before", "after"):
                rows = [json.loads(line) for line in (output/variant/"responses.jsonl").read_text().splitlines()]
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["status"], "ok")
                self.assertGreater(row["total_tokens"], 0)
                self.assertIsNotNone(row["estimated_usd"])
                self.assertTrue(any(c.get("resource") for c in row["tool_calls"]))
                self.assertIn("write", row["nodes"])
            self.assertTrue((output/"summary.json").exists())
            self.assertTrue((output/"responses_readable.md").exists())
