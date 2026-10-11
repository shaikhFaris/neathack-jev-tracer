import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from jev_tracer.evidence import AgentRunEvidence, Outcome, load_evidence


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "deployment"


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((FIXTURES / "good.json").read_text(encoding="utf-8"))

    def reject(self):
        with self.assertRaises(ValidationError):
            AgentRunEvidence.model_validate(self.data)

    def test_all_labeled_fixtures_load_and_roundtrip(self):
        labels = json.loads((FIXTURES / "labels.json").read_text(encoding="utf-8"))
        self.assertEqual({Outcome(entry["expected_outcome"]) for entry in labels.values()}, set(Outcome))
        for name in labels:
            with self.subTest(name=name):
                run = load_evidence(FIXTURES / name)
                self.assertEqual(run.environment, "synthetic")
                self.assertEqual(AgentRunEvidence.model_validate_json(run.model_dump_json()), run)

    def test_claim_does_not_substitute_for_verification(self):
        run = load_evidence(FIXTURES / "missing_verification.json")
        self.assertIn("succeeded", run.final_claim.content)
        self.assertEqual(run.endpoint_verifications, ())
        self.assertTrue(run.evidence_gaps)

    def test_bad_fixture_preserves_contradictory_evidence(self):
        run = load_evidence(FIXTURES / "bad.json")
        self.assertEqual(run.endpoint_expectation.expected_status, 200)
        self.assertEqual(run.endpoint_verifications[0].status_code, 503)
        self.assertIn("healthy", run.final_claim.content)

    def test_timestamp_requires_timezone(self):
        self.data["task"]["provenance"]["timestamp"] = "2026-10-11T10:00:00"
        self.reject()

    def test_trace_mismatch_is_rejected(self):
        self.data["final_claim"]["provenance"]["trace_id"] = "other-trace"
        self.reject()

    def test_duplicate_evidence_id_is_rejected(self):
        self.data["final_claim"]["provenance"]["evidence_id"] = "task"
        self.reject()

    def test_probe_requires_independent_source(self):
        self.data["endpoint_verifications"][0]["provenance"]["source"] = "tool_execution"
        self.reject()

    def test_probe_must_match_expectation(self):
        for field, value in [("url", "http://127.0.0.1:8765/other"),
                             ("expectation_id", "unknown")]:
            with self.subTest(field=field):
                data = json.loads(json.dumps(self.data))
                data["endpoint_verifications"][0][field] = value
                with self.assertRaises(ValidationError):
                    AgentRunEvidence.model_validate(data)

    def test_invalid_http_status_is_rejected(self):
        for status in [99, 600, True, "200"]:
            with self.subTest(status=status):
                self.data["endpoint_verifications"][0]["status_code"] = status
                self.reject()

    def test_transport_failure_is_representable(self):
        probe = self.data["endpoint_verifications"][0]
        probe.update(status_code=None, response_body=None, transport_error="Connection refused")
        run = AgentRunEvidence.model_validate(self.data)
        self.assertIsNone(run.endpoint_verifications[0].status_code)
        probe["status_code"] = 200
        self.reject()

    def test_missing_and_truncated_tool_results_are_explicit(self):
        tool = self.data["tool_executions"][0]
        tool.update(result_json=None, result_available=False)
        AgentRunEvidence.model_validate(self.data)
        tool["result_available"] = True
        self.reject()
        tool.update(result_json='{"status":', result_truncated=True)
        AgentRunEvidence.model_validate(self.data)

    def test_unknown_fields_are_rejected(self):
        self.data["deployment_succeeded"] = True
        self.reject()

    def test_expectation_predates_probe(self):
        self.data["endpoint_expectation"]["provenance"]["timestamp"] = "2026-10-11T10:01:00Z"
        self.reject()

    def test_loader_errors_do_not_echo_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.json"
            path.write_text('{"private_payload":"sensitive-example"}', encoding="utf-8")
            with self.assertRaises(ValueError) as context:
                load_evidence(path)
            self.assertNotIn("sensitive-example", str(context.exception))
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_evidence(path)
            with self.assertRaisesRegex(ValueError, "Cannot read evidence file"):
                load_evidence(Path(folder) / "missing.json")


if __name__ == "__main__":
    unittest.main()
