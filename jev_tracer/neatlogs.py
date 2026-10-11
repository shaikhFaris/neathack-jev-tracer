"""Read-only payload collection using the documented Neatlogs Public API contract.

Span IDs, timestamps, and extraction paths are supplied in a reviewed collection
plan. No assumptions about undocumented span-list/trace-export fields are made.
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import AwareDatetime, Field, ValidationError

from .evidence import (
    AgentRunEvidence, EndpointExpectation, EndpointVerification, EvidenceModel,
    MessageEvidence, NonEmpty, Provenance, ToolExecution,
)
from .redaction import redact

DEFAULT_BASE_URL = "https://app.neatlogs.com/api/v1/public"


class CollectionError(ValueError):
    """Sanitized collection failure; never includes credentials or payloads."""


class PayloadField(EvidenceModel):
    available: bool = Field(strict=True)
    truncated: bool = Field(strict=True)
    mimeType: str | None
    value: str | None


class PayloadFields(EvidenceModel):
    input: PayloadField | None = None
    output: PayloadField | None = None
    error: PayloadField | None = None


class PayloadData(EvidenceModel):
    traceId: NonEmpty
    spanId: NonEmpty
    fields: PayloadFields


class PayloadResponse(EvidenceModel):
    success: Literal[True]
    requestId: NonEmpty
    data: PayloadData


class SpanBinding(EvidenceModel):
    span_id: NonEmpty
    timestamp: AwareDatetime
    field: Literal["input", "output"]
    # Exact JSON object keys/list indices, or empty for the whole payload text.
    path: tuple[str | int, ...] = ()


class ToolBinding(EvidenceModel):
    call_id: NonEmpty
    name: NonEmpty
    input: SpanBinding
    output: SpanBinding


class CollectionPlan(EvidenceModel):
    run_id: NonEmpty
    trace_id: NonEmpty
    environment: Literal["local", "sandbox", "production"]
    task: SpanBinding
    final_claim: SpanBinding
    tools: tuple[ToolBinding, ...] = ()
    endpoint_expectation: EndpointExpectation


def load_plan(path) -> CollectionPlan:
    from pathlib import Path

    try:
        return CollectionPlan.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError):
        raise CollectionError("Collection plan is unreadable or invalid; check the example plan.") from None


class NeatlogsClient:
    def __init__(self, token: str, project_id: str, base_url: str = DEFAULT_BASE_URL,
                 transport: httpx.BaseTransport | None = None):
        from uuid import UUID

        if not token or not project_id:
            raise CollectionError("Set NEATLOGS_TOKEN and NEATLOGS_PROJECT_ID locally.")
        try:
            UUID(project_id)
        except ValueError:
            raise CollectionError("NEATLOGS_PROJECT_ID must be a project UUID.") from None
        parts = urlsplit(base_url)
        if parts.scheme != "https" or parts.hostname not in {"app.neatlogs.com", "eu.app.neatlogs.com"} or parts.port not in {None, 443} or parts.username or parts.password or parts.query or parts.fragment or parts.path.rstrip("/") != "/api/v1/public":
            raise CollectionError("Use the documented US or EU Neatlogs Public API base URL.")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.project_id = project_id
        self.client = httpx.Client(
            timeout=10, follow_redirects=False, transport=transport, trust_env=False,
            headers={"Authorization": f"Bearer {token}", "x-project-id": project_id},
        )

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("NEATLOGS_TOKEN", ""), os.environ.get("NEATLOGS_PROJECT_ID", ""),
                   os.environ.get("NEATLOGS_BASE_URL", DEFAULT_BASE_URL))

    def close(self):
        self.client.close()

    def payload(self, trace_id: str, span_id: str) -> PayloadResponse:
        url = f"{self.base_url}/traces/{quote(trace_id, safe='')}/spans/{quote(span_id, safe='')}/payload"
        for attempt in range(3):
            try:
                with self.client.stream("GET", url, params={"fields": "input,output,error"}) as response:
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After", "")
                    if status == 200:
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 1_000_000:
                                raise CollectionError("Neatlogs payload exceeded the 1 MB collection limit.")
                            chunks.append(chunk)
                        try:
                            result = PayloadResponse.model_validate_json(b"".join(chunks))
                        except ValidationError:
                            raise CollectionError("Neatlogs payload did not match the documented response contract.") from None
                        if result.data.traceId != trace_id or result.data.spanId != span_id:
                            raise CollectionError("Neatlogs response trace/span correlation mismatch.")
                        return result
                    if status not in {429, 500, 502, 503, 504}:
                        raise CollectionError(f"Neatlogs read failed with HTTP {status}.")
            except httpx.HTTPError:
                if attempt == 2:
                    raise CollectionError("Neatlogs request failed after bounded retries.") from None
                retry_after = ""
            if attempt < 2:
                try:
                    delay = max(0, min(float(retry_after), 2))
                except ValueError:
                    delay = 0.1 * 2 ** attempt
                time.sleep(delay)
        raise CollectionError("Neatlogs read failed after bounded retries.")


def _extract(payload: PayloadResponse, binding: SpanBinding) -> tuple[Any, bool, bool]:
    field = getattr(payload.data.fields, binding.field)
    if field is None or not field.available or field.value is None:
        return None, False, False
    if not binding.path:
        return field.value, True, field.truncated
    if field.truncated:
        return None, False, True
    try:
        value = json.loads(field.value)
        for key in binding.path:
            value = value[key]
        return value, True, False
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None, False, False


def normalize_payloads(plan: CollectionPlan, payloads: dict[str, PayloadResponse],
                       known_secrets: tuple[str, ...] = ()) -> AgentRunEvidence:
    gaps = []

    def extract(binding):
        payload = payloads.get(binding.span_id)
        if payload is None:
            raise CollectionError("A planned span payload was not collected.")
        if payload.data.traceId != plan.trace_id or payload.data.spanId != binding.span_id:
            raise CollectionError("Recorded payload correlation mismatch.")
        return _extract(payload, binding)

    def provenance(binding, source, evidence_id):
        return Provenance(evidence_id=evidence_id, source=source, timestamp=binding.timestamp,
                          trace_id=plan.trace_id, span_id=binding.span_id,
                          artifact_ref=f"neatlogs://traces/{quote(plan.trace_id, safe='')}/spans/{quote(binding.span_id, safe='')}?requestId={quote(payloads[binding.span_id].requestId, safe='')}")

    def message(binding, role, evidence_id):
        value, available, truncated = extract(binding)
        if not available or truncated or not isinstance(value, str) or not value.strip():
            gaps.append(f"{evidence_id} payload is missing, truncated, or not text at the configured path.")
            value = f"[Unavailable {evidence_id} evidence]"
        return MessageEvidence(role=role, content=value,
                               provenance=provenance(binding, "conversation", evidence_id))

    task = message(plan.task, "user", "task")
    claim = message(plan.final_claim, "assistant", "claim")
    tools = []
    for index, tool in enumerate(plan.tools):
        arguments, arguments_available, arguments_truncated = extract(tool.input)
        result, available, truncated = extract(tool.output)
        # Never convert an unknown argument payload into an assumed successful call.
        if not arguments_available or arguments_truncated:
            gaps.append(f"Tool {index} arguments could not be extracted.")
            arguments = {}
        def json_text(value):
            if isinstance(value, str):
                try:
                    json.loads(value)
                except json.JSONDecodeError:
                    return json.dumps(value)
                return value
            return json.dumps(value)
        tools.append(ToolExecution(
            call_id=tool.call_id, name=tool.name, arguments_json=json_text(arguments),
            result_json=(str(result) if truncated else json_text(result)) if available else None,
            result_available=available, result_truncated=truncated if available else False,
            provenance=provenance(tool.output, "tool_execution", f"tool-{index}"),
        ))
    return AgentRunEvidence.model_validate(redact({
        "run_id": plan.run_id, "trace_id": plan.trace_id, "environment": plan.environment,
        "task": task.model_dump(mode="json"), "final_claim": claim.model_dump(mode="json"),
        "tool_executions": [tool.model_dump(mode="json") for tool in tools],
        "endpoint_expectation": plan.endpoint_expectation.model_dump(mode="json"),
        "evidence_gaps": gaps,
    }, known_secrets))


def collect_run(client: NeatlogsClient, plan: CollectionPlan) -> AgentRunEvidence:
    bindings = [plan.task, plan.final_claim]
    for tool in plan.tools:
        bindings.extend([tool.input, tool.output])
    payloads = {span_id: client.payload(plan.trace_id, span_id)
                for span_id in dict.fromkeys(binding.span_id for binding in bindings)}
    return normalize_payloads(plan, payloads, (client.token,))


def probe_endpoint(run: AgentRunEvidence, allowed_hosts: tuple[str, ...],
                   transport: httpx.BaseTransport | None = None) -> AgentRunEvidence:
    """Explicit read-only GET, never redirects. Targets come from reviewed criteria."""
    url = str(run.endpoint_expectation.url)
    parts = urlsplit(url)
    if parts.hostname not in allowed_hosts or parts.username or parts.password:
        raise CollectionError("Endpoint host must be explicitly allowed and URL credentials are forbidden.")
    status, body, truncated, error = None, None, False, None
    try:
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False, transport=transport) as client:
            with client.stream("GET", url) as response:
                status = response.status_code
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 65536:
                        truncated = True
                        break
                body = b"".join(chunks)[:65536].decode("utf-8", errors="replace")
    except httpx.HTTPError as exc:
        status, body, truncated, error = None, None, False, type(exc).__name__
    probe = EndpointVerification(
        expectation_id=run.endpoint_expectation.provenance.evidence_id, url=url,
        status_code=status, response_body=body, body_truncated=truncated, transport_error=error,
        provenance=Provenance(evidence_id=f"http-probe-{len(run.endpoint_verifications)}",
                              source="independent_http_probe", timestamp=datetime.now(timezone.utc),
                              trace_id=run.trace_id, artifact_ref=f"http-observation://{run.run_id}/{len(run.endpoint_verifications)}"),
    )
    data = run.model_dump(mode="json")
    data["endpoint_verifications"].append(probe.model_dump(mode="json"))
    return AgentRunEvidence.model_validate(redact(data))
