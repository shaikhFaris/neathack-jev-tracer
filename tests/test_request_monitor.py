import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from jevals.backends import MockBackend

from jev_tracer.evidence import Outcome
from jev_tracer.monitor import LiveRequestSource, RecordedRequestSource, scan_requests
from jev_tracer.neatlogs import CollectionError, NeatlogsClient
from jev_tracer.neatlogs_requests import NeatlogsContextClient, TraceContext, TraceMetadata, trace_page
from jev_tracer.request_evaluation import evaluate_request, request_sample
from jev_tracer.request_evidence import AgentRequestEvidence, InvariantObservation, RequestPolicy
from jev_tracer.request_normalizer import normalize_request

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "fixtures" / "requests" / "neatlogs-bundle.json"
PROJECT = "00000000-0000-0000-0000-000000000001"


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.source = RecordedRequestSource(BUNDLE)
        self.metadata = self.source.pages[0].data.traces[0]
        self.context = self.source.context(self.metadata.traceId)

    def normalized(self, **kwargs):
        return normalize_request(self.metadata, self.context, **kwargs)

    def test_normalizer_recovers_request_tool_schemas_and_lineage(self):
        run = self.normalized()
        self.assertEqual(run.user_task, "Check order 123 and report its actual shipping status.")
        self.assertEqual(run.final_answer, "Order 123 has shipped.")
        self.assertEqual(len(run.messages), 4)
        self.assertEqual(run.tools[0].arguments, {"order_id": 123})
        self.assertEqual(run.tools[0].agent_call_id, "call-1")
        self.assertEqual(run.tools[0].result["status"], "shipped")
        self.assertEqual(run.tool_definitions[0]["function"]["name"], "lookup_order")
        self.assertEqual(run.spans[1].parent_span_id, self.context.root_span.span_id)
        self.assertFalse(run.evidence_gaps)
        self.assertNotIn("Private system prompt", run.model_dump_json())

    def test_cross_trace_context_is_rejected(self):
        self.context.trace_id = "wrong-trace"
        with self.assertRaises(CollectionError):
            self.normalized()

    def test_span_count_discrepancy_and_missing_timestamps_remain_gaps(self):
        self.context.span_count = 99
        self.context.root_span.children[0].start_time = None
        run = self.normalized()
        self.assertTrue(any("count differs" in gap for gap in run.evidence_gaps))
        self.assertTrue(any("timestamp missing" in gap for gap in run.evidence_gaps))

    def test_unknown_root_payload_is_not_invented_as_a_task(self):
        self.context.root_span.input = {"private_customer_payload": "sensitive"}
        self.context.root_span.output = {"unrecognized": "anything"}
        self.context.root_span.children[0].input = None
        self.context.root_span.children[0].output = None
        run = self.normalized()
        self.assertEqual(run.user_task, "")
        self.assertEqual(run.final_answer, "")
        backend = MockBackend()
        result = evaluate_request(run, backend)
        self.assertEqual(result.outcome, Outcome.INSUFFICIENT_EVIDENCE)
        self.assertEqual(len(backend.calls), 0)

    def test_custom_root_paths_recover_task_and_claim(self):
        self.context.root_span.input = {"task": "Check this order."}
        self.context.root_span.output = {"final": "The order shipped."}
        self.context.root_span.children[0].input = None
        run = self.normalized(policy=RequestPolicy(task_path=("task",), final_path=("final",)))
        self.assertEqual(run.user_task, "Check this order.")
        self.assertEqual(run.final_answer, "The order shipped.")

    def test_actual_root_answer_takes_precedence_over_an_internal_llm_draft(self):
        self.context.root_span.output = "Actual returned final answer."
        self.context.root_span.children[0].output = {"choices": [{"message": {"role": "assistant", "content": "Internal draft."}}]}
        run = self.normalized()
        self.assertEqual(run.final_answer, "Actual returned final answer.")

    def test_trace_ids_cannot_escape_artifact_directory(self):
        from jev_tracer.monitor import artifact_key
        self.assertRegex(artifact_key("../../private"), r"^[a-f0-9]{24}$")

    def test_tool_retries_keep_distinct_execution_ids(self):
        original = self.context.root_span.children[1]
        retry = original.model_copy(deep=True)
        retry.span_id = "retry-span"
        self.context.root_span.children.append(retry)
        self.metadata.toolCallsCount = 2
        self.metadata.spansCount = self.context.span_count = 4
        run = self.normalized()
        self.assertEqual(len({tool.call_id for tool in run.tools}), 2)
        self.assertEqual({tool.agent_call_id for tool in run.tools}, {"call-1"})

    def test_relevant_logs_and_retrieval_are_selected_but_debug_logs_are_not_forwarded(self):
        from jev_tracer.neatlogs_requests import ContextSpan

        for name, kind, payload, attributes in [
            ("warning", "LOG", "Endpoint is unavailable", {"log.level": "warning"}),
            ("debug", "LOG", "Private debug dump", {"log.level": "debug"}),
            ("documents", "RETRIEVER", "Relevant retrieved evidence", {}),
        ]:
            self.context.root_span.children.append(ContextSpan(
                span_id=name, span_type=kind, name=name, status="success",
                start_time=self.metadata.startedAt, input=payload if kind == "LOG" else "query",
                output=payload if kind == "RETRIEVER" else None, metadata=attributes))
        self.metadata.spansCount = self.context.span_count = 6
        run = self.normalized()
        sample, _ = request_sample(run)
        rendered = json.dumps(sample)
        self.assertIn("Endpoint is unavailable", rendered)
        self.assertIn("Relevant retrieved evidence", rendered)
        self.assertNotIn("Private debug dump", rendered)

    def test_each_request_uses_one_batched_jevals_call(self):
        backend = MockBackend(default_noul=0.99)
        report = evaluate_request(self.normalized(), backend)
        self.assertEqual(len(backend.calls), 1)
        names = set(report.jevals_results["results"])
        self.assertTrue({"grounded", "used_tool_result", "false_success_claim", "request_completion",
                         "tool_choice", "tool_arguments_supported", "loop_detection", "indirect_injection"} <= names)
        self.assertEqual(report.jevals_mode, "mock (plumbing only)")
        self.assertEqual(report.outcome, Outcome.INSUFFICIENT_EVIDENCE)
        self.assertNotIn("Private system prompt", json.dumps(backend.calls[0]["state"]))

    def test_known_good_real_interface_passes_and_negative_judgment_is_suspicious(self):
        # Fake backend named real only tests mapping; this is not model accuracy validation.
        backend = MockBackend(default_noul=0.99, answers={"false_success_claim.q": 0.01, "indirect_injection.q": 0.01})
        backend.name = "test-real-interface"
        report = evaluate_request(self.normalized(), backend)
        self.assertEqual(report.outcome, Outcome.PASS)
        self.assertFalse(report.suspected_silent_failure)
        backend.answers["false_success_claim.q"] = 0.99
        report = evaluate_request(self.normalized(), backend)
        self.assertEqual(report.outcome, Outcome.INSUFFICIENT_EVIDENCE)
        self.assertTrue(report.suspected_silent_failure)
        self.assertFalse(report.corroborated_failure)

    def test_independent_invariant_is_required_to_corroborate(self):
        value = json.loads((ROOT / "fixtures" / "requests" / "observations.json").read_text())["request-false_success"]
        invariants = tuple(InvariantObservation.model_validate(item) for item in value)
        metadata = self.source.pages[1].data.traces[0]
        run = normalize_request(metadata, self.source.context(metadata.traceId), invariants=invariants)
        report = evaluate_request(run, "mock")
        self.assertEqual(report.outcome, Outcome.FAIL)
        self.assertTrue(report.corroborated_failure)
        self.assertTrue(report.suspected_silent_failure)

    def test_required_external_state_not_replaced_by_tool_claim(self):
        run = self.normalized(policy=RequestPolicy(require_independent_verification=True))
        result = evaluate_request(run, "mock")
        self.assertEqual(result.outcome, Outcome.INSUFFICIENT_EVIDENCE)
        self.assertTrue(any(check.name == "independent_verification" for check in result.checks))

    def test_budget_excess_is_deterministic_not_model_proof(self):
        run = self.normalized(policy=RequestPolicy(max_cost_usd=0.001))
        result = evaluate_request(run, "mock")
        self.assertEqual(result.outcome, Outcome.FAIL)
        self.assertTrue(result.corroborated_failure)

    def test_backend_error_cannot_mark_a_request_healthy(self):
        with patch("jev_tracer.request_evaluation.jevaluate", side_effect=RuntimeError("secret server details")):
            result = evaluate_request(self.normalized(), "vercel")
        self.assertEqual(result.outcome, Outcome.INSUFFICIENT_EVIDENCE)
        self.assertNotIn("secret server details", result.jevals_error)

    def test_model_input_bounds_are_explicit(self):
        self.context.root_span.output["messages"][-1]["content"] = "x" * 6000
        sample, limited = request_sample(self.normalized())
        self.assertTrue(limited)
        self.assertTrue(sample["request_limits"]["model_input_limited"])
        self.assertLess(len(sample["request_final"]), 4100)

    def test_checkpoint_deduplicates_but_pending_traces_are_not_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = scan_requests(self.source, folder, "mock")
            second = scan_requests(self.source, folder, "mock")
            self.assertEqual(first["processed"], 2)
            self.assertEqual(second["processed"], 0)
            self.assertEqual(second["already_evaluated"], 2)
            self.assertEqual(second["pending_finalization"], 1)
            state = json.loads((folder / "state.json").read_text())
            self.assertNotIn("request-pending", state["requests"])
            self.assertTrue((folder / "index.html").exists())

    def test_force_re_evaluates_completed_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            scan_requests(self.source, Path(directory), "mock")
            result = scan_requests(self.source, Path(directory), "mock", force=True)
            self.assertEqual(result["processed"], 2)

    def test_model_errors_are_retried_after_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("jev_tracer.request_evaluation.jevaluate", side_effect=RuntimeError("unavailable")):
                scan_requests(self.source, Path(directory), "mock")
            result = scan_requests(self.source, Path(directory), "mock")
            self.assertEqual(result["processed"], 2)

    def test_checkpoint_cannot_cross_project(self):
        with tempfile.TemporaryDirectory() as directory:
            scan_requests(self.source, Path(directory), "mock")
            self.source.project_id = "other-project"
            with self.assertRaises(ValueError):
                scan_requests(self.source, Path(directory), "mock")


class NeatlogsProtocolTests(unittest.TestCase):
    def setUp(self):
        self.bundle = json.loads(BUNDLE.read_text())

    def test_public_list_uses_documented_filters_and_cursor(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=self.bundle["trace_pages"][0])
        client = NeatlogsClient("read-token", PROJECT, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        page = trace_page(client, limit=25, cursor="opaque", from_time="2026-10-11T00:00:00Z", workflow_name="support-agent")
        self.assertEqual(calls[0].method, "GET")
        self.assertEqual(calls[0].url.params["cursor"], "opaque")
        self.assertEqual(calls[0].url.params["workflowName"], "support-agent")
        self.assertEqual(page.data.page.nextCursor, "opaque-recorded-next")

    def test_mcp_handshake_json_and_sse_context_read(self):
        context = self.bundle["contexts"]["request-good"]
        calls = []
        def handler(request):
            message = json.loads(request.content)
            calls.append(message)
            self.assertEqual(request.url.host, "ingest.neatlogs.com")
            self.assertEqual(request.headers["Authorization"], "Bearer project-key")
            if message["method"] == "initialize":
                return httpx.Response(200, headers={"Mcp-Session-Id": "session-1"}, json={"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}})
            self.assertEqual(request.headers["Mcp-Session-Id"], "session-1")
            if message["method"] == "notifications/initialized":
                return httpx.Response(202)
            self.assertEqual(message["params"]["name"], "get_trace_context")
            value = {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": json.dumps(context)}]}}
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text="event: message\ndata: " + json.dumps(value) + "\n\n")
        client = NeatlogsContextClient("project-key", transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.assertEqual(client.context("request-good").trace_id, "request-good")
        self.assertEqual([call["method"] for call in calls], ["initialize", "notifications/initialized", "tools/call"])

    def test_mutation_tool_and_credentials_to_other_host_are_rejected(self):
        client = NeatlogsContextClient("project-key", transport=httpx.MockTransport(lambda _: self.fail("Unexpected network request")))
        self.addCleanup(client.close)
        with self.assertRaises(CollectionError):
            client.call_read_tool("triage_accept", {})
        with self.assertRaises(CollectionError):
            NeatlogsContextClient("project-key", "https://evil.example/mcp")

    def test_project_binding_is_checked_before_context_collection(self):
        api = NeatlogsClient("read-token", PROJECT, transport=httpx.MockTransport(lambda _: self.fail("Unexpected call")))
        context = unittest.mock.Mock()
        context.call_read_tool.return_value = {"project_id": "different-project"}
        with patch("jev_tracer.monitor.NeatlogsClient.from_env", return_value=api), patch("jev_tracer.monitor.NeatlogsContextClient.from_env", return_value=context):
            with self.assertRaises(CollectionError):
                LiveRequestSource()
        context.close.assert_called_once()
