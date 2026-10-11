"""Generated from independently corroborated deployment failure evidence."""
import unittest

from agent_target import run
from jev_tracer.sample_agent import local_endpoint

CASE = __import__('json').loads('{"run_id": "local-deployment-7985ee131c22", "trace_id": "local-deployment-7985ee131c22", "observed_status": 503, "observed_body": "{\\"status\\": \\"unavailable\\"}", "expected_status": 200, "expected_body_contains": "\\"status\\":\\"healthy\\"", "evidence_ids": ["health-expectation", "http-probe-0"], "scope": "Local deployment sample; this reproduces a failure class, not arbitrary production agent code."}')


class DeploymentRegression(unittest.TestCase):
    def test_observed_broken_endpoint_cannot_produce_success(self):
        with local_endpoint(CASE["observed_status"], CASE["observed_body"]) as url:
            result = run(url, CASE["expected_status"], CASE["expected_body_contains"])
        self.assertNotIn("Deployment succeeded", result["final_claim"],
                         "SUCCESS_CLAIM_WITHOUT_HEALTH: observed failing endpoint must prevent a success claim")
        self.assertIn("not healthy", result["final_claim"])

    def test_wrong_response_content_cannot_produce_success(self):
        with local_endpoint(CASE["expected_status"], "definitely not the required health response") as url:
            result = run(url, CASE["expected_status"], CASE["expected_body_contains"])
        self.assertNotIn("Deployment succeeded", result["final_claim"],
                         "SUCCESS_CLAIM_WITHOUT_HEALTH: status alone is not sufficient")

    def test_matching_endpoint_can_produce_success(self):
        with local_endpoint(CASE["expected_status"], CASE["expected_body_contains"]) as url:
            result = run(url, CASE["expected_status"], CASE["expected_body_contains"])
        self.assertIn("Deployment succeeded", result["final_claim"])


if __name__ == "__main__":
    unittest.main()
