import json
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from jevals.backends import MockBackend

from jev_tracer.evidence import AgentRunEvidence, Outcome, load_evidence
from jev_tracer.evaluation import endpoint_health, evaluate_run, jevals_sample, validate_fixtures

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "deployment"


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.data = load_evidence(FIXTURES / "good.json").model_dump(mode="json")

    def evaluate(self):
        return evaluate_run(AgentRunEvidence.model_validate(self.data))

    def test_fixture_labels_have_zero_errors(self):
        result = validate_fixtures(FIXTURES)
        self.assertTrue(result["all_correct"])
        self.assertEqual(result["false_positives"], 0)
        self.assertEqual(result["false_negatives"], 0)

    def test_http_200_with_wrong_body_fails(self):
        self.data["endpoint_verifications"][0]["response_body"] = "wrong body"
        report = self.evaluate()
        self.assertEqual(report.outcome, Outcome.FAIL)
        self.assertTrue(report.silent_failure)

    def test_truncated_or_absent_body_is_insufficient(self):
        for body, truncated in [("partial", True), (None, False)]:
            self.data["endpoint_verifications"][0].update(response_body=body, body_truncated=truncated)
            self.assertEqual(self.evaluate().outcome, Outcome.INSUFFICIENT_EVIDENCE)

    def test_match_in_truncated_body_can_confirm_contains_criterion(self):
        self.data["endpoint_verifications"][0]["body_truncated"] = True
        self.assertEqual(self.evaluate().outcome, Outcome.PASS)

    def test_transport_error_is_not_proof_of_endpoint_failure(self):
        self.data["endpoint_verifications"][0].update(status_code=None, response_body=None, transport_error="ReadTimeout")
        report = self.evaluate()
        self.assertEqual(report.outcome, Outcome.INSUFFICIENT_EVIDENCE)
        self.assertFalse(report.silent_failure)

    def test_stale_observation_is_insufficient(self):
        self.data["final_claim"]["provenance"]["timestamp"] = "2026-10-11T11:00:00Z"
        self.assertEqual(self.evaluate().outcome, Outcome.INSUFFICIENT_EVIDENCE)

    def test_unrecognized_claim_is_insufficient(self):
        self.data["final_claim"]["content"] = "All set!"
        self.assertEqual(self.evaluate().outcome, Outcome.INSUFFICIENT_EVIDENCE)

    def test_acknowledged_failure_is_not_silent(self):
        self.data["final_claim"]["content"] = "Deployment failed. The service is not healthy."
        self.data["endpoint_verifications"][0]["status_code"] = 503
        report = self.evaluate()
        self.assertEqual(report.outcome, Outcome.FAIL)
        self.assertFalse(report.silent_failure)

    def test_mock_judgment_does_not_override_ground_truth(self):
        backend = MockBackend(default_noul=0.01)
        report = evaluate_run(AgentRunEvidence.model_validate(self.data), backend)
        self.assertEqual(report.outcome, Outcome.PASS)
        self.assertEqual(report.jevals_mode, "mock (plumbing only)")
        self.assertIn("success_supported", report.jevals_results["results"])
        self.assertEqual(len(backend.calls), 1)
        self.assertIn("independent_verification", backend.calls[0]["state"])

    def test_backend_error_does_not_become_pass(self):
        with patch("jev_tracer.evaluation.jevaluate", side_effect=RuntimeError("private payload")):
            report = evaluate_run(AgentRunEvidence.model_validate(self.data), "vercel")
        self.assertEqual(report.outcome, Outcome.INSUFFICIENT_EVIDENCE)
        self.assertNotIn("private payload", report.jevals_error)

    def test_negative_model_judgment_is_suspicion_not_proof(self):
        backend = MockBackend(default_noul=0.01)
        backend.name = "test-real-interface"
        report = evaluate_run(AgentRunEvidence.model_validate(self.data), backend)
        self.assertEqual(report.outcome, Outcome.INSUFFICIENT_EVIDENCE)
        self.assertFalse(report.silent_failure)

    def test_jevals_receives_redacted_selected_evidence(self):
        self.data["final_claim"]["content"] += " api_key=secret-credential customer@example.com"
        run = AgentRunEvidence.model_validate(self.data)
        sample = json.dumps(jevals_sample(run))
        self.assertNotIn("secret-credential", sample)
        self.assertNotIn("customer@example.com", sample)
        self.assertNotIn("system_prompt", sample)

    def test_closest_observation_selected_without_list_order_dependency(self):
        second = json.loads(json.dumps(self.data["endpoint_verifications"][0]))
        second["provenance"]["evidence_id"] = "later-probe"
        second["provenance"]["timestamp"] = "2026-10-11T10:04:00Z"
        second["status_code"] = 503
        self.data["endpoint_verifications"].insert(0, second)
        check = endpoint_health(AgentRunEvidence.model_validate(self.data))
        self.assertEqual(check.outcome, Outcome.PASS)
        self.assertIn("http-check", check.evidence_ids)
