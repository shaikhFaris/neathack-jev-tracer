"""Local, redacted evidence artifacts and a self-contained review page."""

import html
import json
from pathlib import Path

from .evidence import AgentRunEvidence
from .evaluation import EvaluationReport
from .redaction import redact


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    content = json.dumps(redact(value), indent=2, ensure_ascii=False) + "\n"
    # Replace only after the whole document is written.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def persist_report(folder: Path, run: AgentRunEvidence, report: EvaluationReport) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "evidence.json", run)
    write_json(folder / "evaluation.json", report)
    render_report(folder, run, report)
    return folder / "report.html"


def render_report(folder: Path, run: AgentRunEvidence, report: EvaluationReport, investigation=None) -> None:
    escape = lambda value: html.escape(str(value), quote=True)
    safe = redact(run.model_dump(mode="json"))
    rows = "".join(
        f"<tr><td>{escape(check.name)}</td><td class='{escape(check.outcome.value)}'>{escape(check.outcome.value)}</td>"
        f"<td>{escape(check.explanation)}</td><td>{escape(', '.join(check.evidence_ids))}</td></tr>"
        for check in report.checks
    )
    probes = safe["endpoint_verifications"]
    observed = "; ".join(f"HTTP {p['status_code']} · {p['response_body']}" for p in probes) or "No independent observation"
    artifact_links = "".join(
        f"<li><strong>{escape(key)}</strong>: <code>{escape(value)}</code></li>"
        for key, value in redact(report.evidence_refs).items()
    )
    extras = ""
    if investigation is not None:
        verified = investigation.get("red_before_green_verified", False)
        pending = investigation.get("approval_required", False)
        status = "Regression failed before the fix and passed after it." if verified else "Pre-fix failure reproduced. Proposed fix awaits review." if pending else "Evidence analysis completed."
        links = ('<p><a href="regression/test_deployment_regression.py">Generated regression</a> · '
                 '<a href="regression/proposed_fix.diff">Proposed sample fix</a> · '
                 '<a href="regression/red.json">Pre-fix test output</a></p>') if investigation.get("regression_path") else ""
        if verified:
            links += '<p><a href="regression/green.json">Post-fix test output</a> · <a href="root_cause.json">Root-cause evidence</a></p>'
        extras = (f"<div class='card'><h2>Investigation and regression</h2><p>{escape(status)}</p>{links}"
                  f"<p class='muted'>{escape(investigation.get('reproduction_scope', ''))}</p></div>"
                  f"<details><summary>Investigation details</summary><pre>{escape(json.dumps(redact(investigation), indent=2))}</pre></details>")
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jev Tracer · {escape(run.run_id)}</title>
<style>
body{{margin:0;background:#0d1424;color:#e8edf6;font:16px system-ui,sans-serif}}main{{max-width:1100px;margin:40px auto;padding:0 24px}}
h1{{font-size:32px;margin-bottom:8px}}h2{{margin-top:32px}}.muted{{color:#a5b5cc}}.card{{background:#182338;padding:22px;border-radius:14px;margin:16px 0}}
.pass{{color:#67e8a6}}.fail{{color:#ff9696}}.insufficient_evidence{{color:#ffd278}}table{{width:100%;border-collapse:collapse}}th,td{{padding:14px;text-align:left;border-bottom:1px solid #344057}}
pre,code{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#9acbff}}blockquote{{margin:0;border-left:3px solid #809aff;padding-left:18px}}
@media(max-width:700px){{table{{font-size:12px}}th,td{{padding:7px}}main{{padding:0 14px}}}}
</style></head><body><main>
<p class="muted">JEV TRACER · AGENT RELIABILITY</p><h1>Deployment evidence review</h1>
<p class="muted">Run {escape(run.run_id)} · Trace {escape(run.trace_id)} · {escape(run.environment)}</p>
<div class="card"><h2 class="{escape(report.outcome.value)}">{escape(report.outcome.value.upper().replace('_', ' '))}</h2>
<p>{'Independently corroborated silent failure.' if report.silent_failure else 'No corroborated silent failure identified.'}</p>
<p class="muted">A pass means no failure detected by these checks; it is not proof of overall correctness.</p></div>
<div class="card"><h2>Agent claim</h2><blockquote>{escape(safe['final_claim']['content'])}</blockquote>
<h2>Independent endpoint observation</h2><pre>{escape(observed)}</pre>
<p>Expected HTTP {run.endpoint_expectation.expected_status} with <code>{escape(safe['endpoint_expectation']['expected_body_contains'])}</code>.</p></div>
<h2>Checks</h2><table><thead><tr><th>Check</th><th>Outcome</th><th>Evidence</th><th>References</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Jevals</h2><p>{escape(report.jevals_mode)}</p><pre>{escape(json.dumps(redact(report.jevals_results), indent=2))}</pre>
<p>{escape(report.jevals_error or '')}</p>{extras}<h2>Artifact provenance</h2><ul>{artifact_links}</ul>
<p><a href="evidence.json">Normalized evidence</a> · <a href="evaluation.json">Evaluation JSON</a></p>
</main></body></html>"""
    (folder / "report.html").write_text(document, encoding="utf-8")
