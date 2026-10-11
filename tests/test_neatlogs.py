import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from jev_tracer.evidence import load_evidence
from jev_tracer.neatlogs import (
    CollectionError, NeatlogsClient, PayloadResponse, collect_run,
    load_plan, normalize_payloads, probe_endpoint,
)

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "00000000-0000-0000-0000-000000000001"


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_plan(ROOT / "fixtures" / "neatlogs" / "collection-plan.json")
        self.raw = json.loads((ROOT / "fixtures" / "neatlogs" / "payloads.json").read_text(encoding="utf-8"))

    def client(self, handler):
        client = NeatlogsClient("known-private-token", PROJECT, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def payloads(self):
        return {key: PayloadResponse.model_validate(value) for key, value in self.raw.items()}

    def test_get_only_collection_with_auth_correlation_and_redaction(self):
        requests = []
        self.raw["deployment-span"]["data"]["fields"]["output"]["value"] = '{"status":"accepted","api_key":"known-private-token"}'
        def handler(request):
            requests.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["Authorization"], "Bearer known-private-token")
            self.assertEqual(request.headers["x-project-id"], PROJECT)
            self.assertEqual(request.url.params["fields"], "input,output,error")
            span = request.url.path.split("/")[-2]
            return httpx.Response(200, json=self.raw[span])
        run = collect_run(self.client(handler), self.plan)
        self.assertEqual(len(requests), 2)  # Shared task/final span fetched once.
        self.assertEqual(run.final_claim.content, "Deployment succeeded. The service is healthy and ready to use.")
        self.assertNotIn("known-private-token", run.model_dump_json())
        self.assertEqual(run.endpoint_verifications, ())
        self.assertIn("requestId=", run.tool_executions[0].provenance.artifact_ref)

    def test_unavailable_claim_remains_a_gap(self):
        field = self.raw["agent-span"]["data"]["fields"]["output"]
        field.update(value=None, available=False)
        run = normalize_payloads(self.plan, self.payloads())
        self.assertTrue(run.evidence_gaps)
        self.assertNotIn("succeeded", run.final_claim.content)

    def test_truncated_tool_payload_remains_explicit(self):
        field = self.raw["deployment-span"]["data"]["fields"]["output"]
        field.update(value='{"status":', truncated=True)
        run = normalize_payloads(self.plan, self.payloads())
        self.assertTrue(run.tool_executions[0].result_truncated)

    def test_correlation_mismatch_is_rejected(self):
        self.raw["agent-span"]["data"]["traceId"] = "other-trace"
        with self.assertRaises(CollectionError):
            normalize_payloads(self.plan, self.payloads())
        with self.assertRaises(CollectionError):
            self.client(lambda _: httpx.Response(200, json=self.raw["agent-span"])).payload(self.plan.trace_id, "agent-span")

    def test_auth_failure_is_not_retried_or_echoed(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(403, text="private server error")
        with self.assertRaisesRegex(CollectionError, "HTTP 403") as context:
            self.client(handler).payload(self.plan.trace_id, "agent-span")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private", str(context.exception))

    def test_transient_failure_has_bounded_retries(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "999"})
        with patch("jev_tracer.neatlogs.time.sleep") as sleep:
            with self.assertRaises(CollectionError):
                self.client(handler).payload(self.plan.trace_id, "agent-span")
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 2])

    def test_redirect_and_unknown_schema_are_rejected(self):
        for response in [httpx.Response(302, headers={"Location": "https://other.example"}),
                         httpx.Response(200, json={"unexpected": "payload"})]:
            with self.subTest(status=response.status_code), self.assertRaises(CollectionError):
                self.client(lambda _: response).payload(self.plan.trace_id, "agent-span")

    def test_credentials_cannot_be_sent_to_other_host(self):
        with self.assertRaises(CollectionError):
            NeatlogsClient("private-token", PROJECT, base_url="https://evil.example/api/v1/public")

    def test_probe_requires_explicit_host_and_does_not_follow_redirects(self):
        run = load_evidence(ROOT / "fixtures" / "deployment" / "good.json")
        with self.assertRaises(CollectionError):
            probe_endpoint(run, ())
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://other.example"})
        with patch("jev_tracer.neatlogs.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 11, 10, 0, 6, tzinfo=timezone.utc)
            observed = probe_endpoint(run, ("127.0.0.1",), httpx.MockTransport(handler))
        self.assertEqual(len(calls), 1)
        self.assertEqual(observed.endpoint_verifications[-1].status_code, 302)

    def test_probe_records_transport_errors(self):
        run = load_evidence(ROOT / "fixtures" / "deployment" / "good.json")
        def handler(request):
            raise httpx.ConnectError("sensitive error", request=request)
        with patch("jev_tracer.neatlogs.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 11, 10, 0, 6, tzinfo=timezone.utc)
            observed = probe_endpoint(run, ("127.0.0.1",), httpx.MockTransport(handler))
        probe = observed.endpoint_verifications[-1]
        self.assertIsNone(probe.status_code)
        self.assertEqual(probe.transport_error, "ConnectError")
