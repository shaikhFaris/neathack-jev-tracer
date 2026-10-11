import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from jev_tracer.evidence import load_evidence
from jev_tracer.evaluation import evaluate_run
from jev_tracer.investigator import build_reasoning_agent, investigate
from jev_tracer.regression import generate_regression
from jev_tracer.reporting import persist_report
from jev_tracer.sample_agent import local_endpoint, observe_sample, run_sample_agent

ROOT = Path(__file__).resolve().parents[1]


class ToolCallingFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class WorkflowTests(unittest.TestCase):
    def test_langchain_agent_uses_read_only_evidence_tools_and_structured_output(self):
        run = load_evidence(ROOT / "fixtures" / "deployment" / "bad.json")
        model = ToolCallingFakeModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "inspect_deployment_evidence", "args": {}, "id": "inspect-1", "type": "tool_call"}]),
            AIMessage(content="", tool_calls=[{"name": "Hypothesis", "args": {
                "explanation": "Deployment acceptance is not health verification.",
                "evidence_ids": ["claim", "http-check"],
                "next_read_only_check": "Inspect whether a health probe precedes the claim."},
                "id": "hypothesis-1", "type": "tool_call"}]),
        ])
        result = build_reasoning_agent(run, model).invoke({"messages": [{"role": "user", "content": "Investigate this run."}]},
                                                         {"recursion_limit": 8})
        self.assertEqual(result["structured_response"].evidence_ids, ["claim", "http-check"])
        self.assertTrue(any(message.type == "tool" and message.name == "inspect_deployment_evidence"
                            for message in result["messages"]))

    def test_live_observation_and_red_before_green(self):
        with local_endpoint() as url:
            run = observe_sample(url)
        report = evaluate_run(run)
        self.assertTrue(report.silent_failure)
        self.assertEqual(run.endpoint_verifications[0].status_code, 503)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            result = investigate(run, report, folder, approve_demo_fix=True)
            self.assertEqual(result["red"]["exit_code"], 1)
            self.assertEqual(result["green"]["exit_code"], 0)
            self.assertTrue(result["red_before_green_verified"])
            self.assertTrue((folder / "regression" / "proposed_fix.diff").is_file())

    def test_approval_gate_stops_before_fixed_worker(self):
        run = load_evidence(ROOT / "fixtures" / "deployment" / "bad.json")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            result = investigate(run, evaluate_run(run), folder)
            self.assertTrue(result["approval_required"])
            self.assertNotIn("green", result)
            self.assertFalse((folder / "regression" / "green.json").exists())

    def test_healthy_run_cannot_generate_failure_regression(self):
        run = load_evidence(ROOT / "fixtures" / "deployment" / "good.json")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with self.assertRaises(ValueError):
                generate_regression(folder, run, evaluate_run(run))
            result = investigate(run, evaluate_run(run), folder)
            self.assertNotIn("regression_path", result)

    def test_fixed_agent_refuses_wrong_response_content(self):
        with local_endpoint(200, '{"status":"unavailable"}') as url:
            result = run_sample_agent(url)
        self.assertIn("not healthy", result["final_claim"])
        self.assertEqual([event["name"] for event in result["tool_events"]], ["deploy_service", "check_local_health"])

    def test_report_escapes_untrusted_html_and_redacts_payloads(self):
        run = load_evidence(ROOT / "fixtures" / "deployment" / "bad.json")
        data = run.model_dump(mode="json")
        data["final_claim"]["content"] += '<script>alert(1)</script> api_key=private-value'
        run = type(run).model_validate(data)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            page = persist_report(folder, run, evaluate_run(run))
            text = page.read_text(encoding="utf-8")
            self.assertNotIn("<script>", text)
            self.assertIn("&lt;script&gt;", text)
            self.assertNotIn("private-value", text)
            self.assertNotIn("private-value", (folder / "evidence.json").read_text(encoding="utf-8"))
