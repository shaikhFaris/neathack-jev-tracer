"""Generate an invariant-based regression from corroborated deployment evidence.

Only a trusted local template is executed. Trace content cannot select commands,
Python code, imports, or filesystem destinations.
"""

import difflib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .evidence import AgentRunEvidence
from .evaluation import EvaluationReport
from .redaction import redact
from .reporting import write_json

ROOT = Path(__file__).resolve().parents[1]

REGRESSION_TEMPLATE = '''"""Generated from independently corroborated deployment failure evidence."""
import unittest

from agent_target import run
from jev_tracer.sample_agent import local_endpoint

CASE = __CASE__


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
'''


def target_source(verify: bool) -> str:
    return ('from jev_tracer.sample_agent import run_sample_agent\n\n'
            'def run(url, expected_status, body_contains):\n'
            '    return run_sample_agent(url, expected_status, body_contains, '
            f'verify_endpoint={verify!r})\n')


def generate_regression(folder: Path, run: AgentRunEvidence, report: EvaluationReport) -> Path:
    if report.run_id != run.run_id or report.trace_id != run.trace_id:
        raise ValueError("Evaluation report must correlate to the reproduced run.")
    if not report.silent_failure:
        raise ValueError("Regression generation requires a corroborated silent deployment failure.")
    check = next(c for c in report.checks if c.name == "endpoint_health")
    probe = next(p for p in run.endpoint_verifications if p.provenance.evidence_id in check.evidence_ids)
    if probe.status_code is None or probe.response_body is None or probe.body_truncated:
        raise ValueError("Reproduction requires a complete recorded HTTP response.")
    case = {
        "run_id": run.run_id, "trace_id": run.trace_id,
        "observed_status": probe.status_code, "observed_body": probe.response_body,
        "expected_status": run.endpoint_expectation.expected_status,
        "expected_body_contains": run.endpoint_expectation.expected_body_contains,
        "evidence_ids": list(check.evidence_ids),
        "scope": "Local deployment sample; this reproduces a failure class, not arbitrary production agent code.",
    }
    folder.mkdir(parents=True, exist_ok=True)
    # JSON encoded inside a Python string literal, never executable trace content.
    encoded = repr(json.dumps(redact(case), ensure_ascii=True))
    source = REGRESSION_TEMPLATE.replace("__CASE__", f"__import__('json').loads({encoded})")
    path = folder / "test_deployment_regression.py"
    path.write_text(source, encoding="utf-8")
    write_json(folder / "case.json", case)
    buggy, fixed = target_source(False), target_source(True)
    (folder / "agent_target.buggy.py").write_text(buggy, encoding="utf-8")
    (folder / "agent_target.fixed.py").write_text(fixed, encoding="utf-8")
    patch = "".join(difflib.unified_diff(buggy.splitlines(True), fixed.splitlines(True),
                                       fromfile="agent_target.py (buggy)", tofile="agent_target.py (proposed fix)"))
    (folder / "proposed_fix.diff").write_text(patch, encoding="utf-8")
    return path


def run_regression(folder: Path, fixed: bool) -> dict:
    """Run only generated local template tests in a temporary worker directory."""
    # Never execute a user-provided test file: regenerate from the saved typed case.
    case = json.loads((folder / "case.json").read_text(encoding="utf-8"))
    source = REGRESSION_TEMPLATE.replace("__CASE__", f"__import__('json').loads({repr(json.dumps(case))})")
    with tempfile.TemporaryDirectory(prefix="jev-tracer-regression-") as temporary:
        worker = Path(temporary)
        (worker / "test_deployment_regression.py").write_text(source, encoding="utf-8")
        (worker / "agent_target.py").write_text(target_source(fixed), encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if not any(word in k.upper() for word in ("TOKEN", "SECRET", "API_KEY", "PASSWORD", "TRACING"))}
        env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                   LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
        try:
            process = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", str(worker), "-p", "test_deployment_regression.py", "-v"],
                cwd=worker, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Local regression worker exceeded its 45-second time limit.") from None
    output = process.stdout + process.stderr
    return redact({"variant": "fixed" if fixed else "buggy", "exit_code": process.returncode,
                   "assertion_failure_reproduced": "SUCCESS_CLAIM_WITHOUT_HEALTH" in output and "AssertionError" in output,
                   "tests_completed": "Ran 3 tests" in output,
                   "output": output})


def verify_red_green(folder: Path, approve_fix: bool = False) -> dict:
    red = run_regression(folder, fixed=False)
    write_json(folder / "red.json", red)
    if red["exit_code"] == 0 or not red["assertion_failure_reproduced"] or not red["tests_completed"]:
        raise RuntimeError("Pre-fix regression did not reproduce the expected assertion failure.")
    result = {"red": red, "approval_required": not approve_fix,
              "fix_scope": "Temporary local sample only; no production code is changed."}
    if approve_fix:
        green = run_regression(folder, fixed=True)
        write_json(folder / "green.json", green)
        if green["exit_code"] != 0 or not green["tests_completed"]:
            raise RuntimeError("Post-fix regression did not pass all generated tests.")
        result.update(green=green, red_before_green_verified=True)
    write_json(folder / "verification.json", result)
    return result
