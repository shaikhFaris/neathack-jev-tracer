"""Bounded per-request Neatlogs -> redaction -> Jevals monitoring."""

import hashlib
import html
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from jevals.backends import Backend

from .neatlogs import CollectionError, NeatlogsClient
from .neatlogs_requests import NeatlogsContextClient, TraceContext, TraceMetadata, TracePageResponse, trace_page
from .redaction import redact
from .reporting import write_json
from .request_evaluation import RequestEvaluation, evaluate_request
from .request_evidence import InvariantObservation, RequestPolicy
from .request_normalizer import normalize_request

logger = logging.getLogger(__name__)


class RequestSource(Protocol):
    project_id: str
    known_secrets: tuple[str, ...]

    def page(self, **filters) -> TracePageResponse: ...
    def context(self, trace_id: str) -> TraceContext: ...
    def close(self) -> None: ...


class LiveRequestSource:
    def __init__(self):
        self.api = NeatlogsClient.from_env()
        try:
            self.context_client = NeatlogsContextClient.from_env()
            actor = self.context_client.call_read_tool("whoami", {})
            if actor.get("project_id") != self.api.project_id:
                raise CollectionError("Public API and full-context credentials must be bound to the same Neatlogs project.")
        except Exception:
            self.api.close()
            if hasattr(self, "context_client"):
                self.context_client.close()
            raise
        self.project_id = self.api.project_id
        self.known_secrets = (self.api.token, self.context_client.api_key)

    def page(self, **filters):
        return trace_page(self.api, **filters)

    def context(self, trace_id):
        return self.context_client.context(trace_id)

    def close(self):
        self.context_client.close()
        self.api.close()


class RecordedRequestSource:
    """Synthetic/recorded documented response bundle for offline verification."""
    def __init__(self, path: Path):
        value = json.loads(path.read_text(encoding="utf-8"))
        self.project_id = value["project_id"]
        self.known_secrets = ()
        self.pages = [TracePageResponse.model_validate(page) for page in value["trace_pages"]]
        self.contexts = {trace: TraceContext.model_validate(context) for trace, context in value["contexts"].items()}

    def page(self, **filters):
        cursor = filters.get("cursor")
        index = 0
        if cursor:
            indices = [i + 1 for i, page in enumerate(self.pages) if page.data.page.nextCursor == cursor]
            if len(indices) != 1 or indices[0] >= len(self.pages):
                raise CollectionError("Recorded bundle cursor is unavailable.")
            index = indices[0]
        return self.pages[index]

    def context(self, trace_id):
        if trace_id not in self.contexts:
            raise CollectionError("Recorded bundle has no context for this trace.")
        return self.contexts[trace_id]

    def close(self):
        pass


def artifact_key(trace_id):
    return hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:24]


def render_request(folder: Path, run, report: RequestEvaluation):
    esc = lambda value: html.escape(str(value), quote=True)
    rows = "".join(f"<tr><td>{esc(check.name)}</td><td>{esc(check.outcome.value)}</td><td>{esc(check.explanation)}</td></tr>"
                   for check in report.checks)
    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Request reliability · {esc(run.trace_id)}</title><style>body{{background:#101a2b;color:#edf2fb;font:16px system-ui;margin:40px auto;max-width:1100px;padding:20px}}a{{color:#9fcaff}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #33435b}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}</style></head><body>
<a href="../../index.html">All requests</a><h1>{esc(report.outcome.value.upper().replace('_',' '))}</h1>
<p>Trace: {esc(run.trace_id)} · Workflow: {esc(run.workflow_name)}</p>
<p>Suspected silent failure: {esc(report.suspected_silent_failure)} · Corroborated failure: {esc(report.corroborated_failure)}</p>
<h2>User request</h2><pre>{esc(redact(run.user_task))}</pre><h2>Final answer</h2><pre>{esc(redact(run.final_answer))}</pre>
<h2>Checks</h2><table><tr><th>Check</th><th>Outcome</th><th>Explanation</th></tr>{rows}</table>
<h2>Jev results</h2><p>{esc(report.jevals_mode)}</p><pre>{esc(json.dumps(redact(report.jevals_results), indent=2))}</pre>
<p>{esc(report.jevals_error or '')}</p><p>Missing or unverifiable evidence is not proof of a bug. A pass means no issue detected by these checks.</p>
<p><a href="evidence.json">Evidence</a> · <a href="evaluation.json">Evaluation</a> · <a href="jev-input.json">Selected redacted Jev input</a></p></body></html>"""
    (folder / "report.html").write_text(document, encoding="utf-8")


def render_index(folder: Path, state: dict, summary: dict):
    esc = lambda value: html.escape(str(value), quote=True)
    rows = []
    for trace_id, item in state["requests"].items():
        target = f"requests/{artifact_key(trace_id)}/report.html" if item.get("has_report") else f"requests/{artifact_key(trace_id)}/collection-error.json"
        rows.append(f"<tr><td><a href='{target}'>{esc(trace_id)}</a></td><td>{esc(item['outcome'])}</td><td>{esc(item.get('suspected_silent_failure', False))}</td><td>{esc(item.get('jevals_mode', 'not evaluated'))}</td></tr>")
    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Jev Tracer · Requests</title>
<style>body{{background:#101a2b;color:#edf2fb;font:16px system-ui;margin:40px auto;max-width:1100px;padding:20px}}a{{color:#9fcaff}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #33435b}}table{{width:100%;border-collapse:collapse}}pre{{white-space:pre-wrap}}</style></head><body>
<h1>Agent request reliability</h1><p>Neatlogs evidence evaluated per request with deterministic checks and Jev.</p>
<pre>{esc(json.dumps(redact(summary), indent=2))}</pre><table><tr><th>Trace</th><th>Outcome</th><th>Suspected silent failure</th><th>Jev mode</th></tr>{''.join(rows)}</table>
<p>Model concerns require corroboration. Mock results validate plumbing only. Pending or incomplete requests are not marked healthy.</p></body></html>"""
    (folder / "index.html").write_text(document, encoding="utf-8")


def scan_requests(source: RequestSource, folder: Path, backend: str | Backend,
                  policy: RequestPolicy | None = None, policies_by_workflow: dict | None = None,
                  invariants_by_trace: dict[str, tuple[InvariantObservation, ...]] | None = None,
                  max_requests=50, max_pages=20, cursor=None, from_time=None,
                  to_time=None, workflow_name=None, session_id=None, force=False) -> dict:
    if not 1 <= max_requests <= 1000 or not 1 <= max_pages <= 100:
        raise ValueError("Use max_requests 1–1000 and max_pages 1–100.")
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"schema_version": "1.0", "project_id": source.project_id, "requests": {}}
    if state.get("schema_version") != "1.0" or state.get("project_id") != source.project_id or not isinstance(state.get("requests"), dict):
        raise ValueError("Monitor checkpoint must match this project and schema; use a separate output directory.")
    policy = policy or RequestPolicy()
    policies_by_workflow = policies_by_workflow or {}
    invariants_by_trace = invariants_by_trace or {}
    pages, processed, skipped, pending, failures = 0, 0, 0, 0, 0
    visited_cursors = set()
    seen_traces = set()
    has_more, next_cursor = True, cursor
    while has_more and pages < max_pages and processed < max_requests:
        logger.info("Fetching trace page %d", pages + 1)
        page = source.page(limit=min(50, max_requests - processed), cursor=next_cursor,
                           from_time=from_time, to_time=to_time, workflow_name=workflow_name, session_id=session_id)
        pages += 1
        logger.info("Page %d contains %d traces", pages, len(page.data.traces))
        for metadata in page.data.traces:
            if metadata.traceId in seen_traces:
                skipped += 1
                continue
            seen_traces.add(metadata.traceId)
            if metadata.finalizationStatus != "finalized" or metadata.completedAt is None:
                pending += 1
                logger.info("Trace %s: awaiting finalization", artifact_key(metadata.traceId))
                continue  # Never checkpoint an unfinished request as evaluated.
            request_policy = RequestPolicy.model_validate({**policy.model_dump(),
                                  **policies_by_workflow.get(metadata.workflowName, {})})
            invariants = invariants_by_trace.get(metadata.traceId, ())
            fingerprint = hashlib.sha256(json.dumps({
                "metadata": metadata.model_dump(mode="json"),
                "policy": request_policy.model_dump(mode="json"),
                "invariants": [item.model_dump(mode="json") for item in invariants],
                "backend": backend if isinstance(backend, str) else backend.name,
                "pipeline_revision": "request-monitor-1.0",
            }, sort_keys=True).encode()).hexdigest()
            previous = state["requests"].get(metadata.traceId)
            if not force and previous and previous.get("fingerprint") == fingerprint and not previous.get("retry_required"):
                skipped += 1
                logger.debug("Trace %s: already evaluated", artifact_key(metadata.traceId))
                continue
            processed += 1
            output = folder / "requests" / artifact_key(metadata.traceId)
            output.mkdir(parents=True, exist_ok=True)
            write_json(output / "metadata.json", metadata.model_dump(mode="json"))
            try:
                logger.info("Trace %s: collecting full context", artifact_key(metadata.traceId))
                context = source.context(metadata.traceId)
                run = normalize_request(metadata, context, request_policy, invariants, source.known_secrets)
                logger.info("Trace %s: normalized %d spans and %d tool calls; evaluating", artifact_key(metadata.traceId), len(run.spans), len(run.tools))
                report = evaluate_request(run, backend)
                logger.info("Trace %s: outcome=%s suspected_silent_failure=%s", artifact_key(metadata.traceId), report.outcome.value, report.suspected_silent_failure)
                if report.jevals_error:
                    logger.warning("Trace %s: Jev unavailable; evaluation remains inconclusive", artifact_key(metadata.traceId))
                write_json(output / "evidence.json", run)
                write_json(output / "evaluation.json", report)
                from .request_evaluation import request_sample

                try:
                    selected, _ = request_sample(run)
                except ValueError:
                    selected = {"error": "Selected model evidence exceeded its configured size limit."}
                write_json(output / "jev-input.json", selected)
                render_request(output, run, report)
                state["requests"][metadata.traceId] = {
                    "fingerprint": fingerprint, "outcome": report.outcome.value,
                    "suspected_silent_failure": report.suspected_silent_failure,
                    "corroborated_failure": report.corroborated_failure, "jevals_mode": report.jevals_mode,
                    "retry_required": bool(report.jevals_error) or any("count differs" in gap for gap in run.evidence_gaps),
                    "has_report": True,
                }
            except (ValueError, OSError, RuntimeError) as exc:
                failures += 1
                logger.error("Trace %s: collection or normalization failed (%s)", artifact_key(metadata.traceId), type(exc).__name__)
                # Do not echo unknown payloads or credentials from validation exceptions.
                write_json(output / "collection-error.json", {"trace_id": metadata.traceId,
                    "outcome": "insufficient_evidence", "error_type": type(exc).__name__,
                    "explanation": "Request collection or normalization failed; retry after checking credentials/schema."})
                state["requests"][metadata.traceId] = {
                    "fingerprint": fingerprint, "outcome": "insufficient_evidence",
                    "retry_required": True, "has_report": False,
                }
            write_json(state_path, state)
            if processed >= max_requests:
                break
        has_more = page.data.page.hasMore and page.data.page.nextCursor is not None
        next_cursor = page.data.page.nextCursor if has_more else None
        if next_cursor in visited_cursors:
            raise CollectionError("Neatlogs repeated a pagination cursor; collection stopped to avoid a loop.")
        if next_cursor:
            visited_cursors.add(next_cursor)
    summary = {
        "project_id": source.project_id, "processed": processed, "already_evaluated": skipped,
        "pending_finalization": pending, "collection_failures": failures, "pages": pages,
        "has_more": has_more, "next_cursor": next_cursor,
        "from": from_time, "to": to_time,
        "outcomes": {outcome: sum(item["outcome"] == outcome for item in state["requests"].values())
                     for outcome in ("pass", "fail", "insufficient_evidence")},
        "report": str(folder / "index.html"),
    }
    write_json(state_path, state)
    write_json(folder / "summary.json", summary)
    render_index(folder, state, summary)
    logger.info("Scan complete: processed=%d skipped=%d pending=%d collection_failures=%d", processed, skipped, pending, failures)
    return summary
