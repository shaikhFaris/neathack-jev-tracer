"""Normalized, already-redacted deployment evidence; no collection or evaluation."""

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    model_validator,
)

NonEmpty = Annotated[str, Field(min_length=1, pattern=r"\S")]
HttpStatus = Annotated[int, Field(strict=True, ge=100, le=599)]


class Outcome(StrEnum):
    """Shared vocabulary for future evaluation results and fixture labels."""

    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provenance(EvidenceModel):
    evidence_id: NonEmpty
    source: Literal["conversation", "tool_execution", "acceptance_criteria", "independent_http_probe"]
    timestamp: AwareDatetime
    trace_id: NonEmpty
    span_id: NonEmpty | None = None
    artifact_ref: NonEmpty


class MessageEvidence(EvidenceModel):
    role: Literal["user", "assistant"]
    content: NonEmpty
    provenance: Provenance

    @model_validator(mode="after")
    def check_source(self) -> Self:
        if self.provenance.source != "conversation":
            raise ValueError("Messages must come from conversation evidence")
        return self


class ToolExecution(EvidenceModel):
    call_id: NonEmpty
    name: NonEmpty
    # JSON text avoids assuming every provider uses the same argument/result schema.
    arguments_json: NonEmpty
    result_json: NonEmpty | None = None
    result_available: bool = Field(strict=True)
    result_truncated: bool = Field(default=False, strict=True)
    provenance: Provenance

    @model_validator(mode="after")
    def check_result(self) -> Self:
        if self.provenance.source != "tool_execution":
            raise ValueError("Tool executions must come from tool evidence")
        if self.result_available != (self.result_json is not None):
            raise ValueError("Result availability must match the presence of result data")
        if self.result_truncated and not self.result_available:
            raise ValueError("An unavailable result cannot be marked truncated")
        # Truncated results may contain incomplete JSON; arguments must be complete.
        json.loads(self.arguments_json)
        if self.result_json is not None and not self.result_truncated:
            json.loads(self.result_json)
        return self


class EndpointExpectation(EvidenceModel):
    url: HttpUrl
    method: Literal["GET"] = "GET"
    expected_status: HttpStatus
    expected_body_contains: NonEmpty
    provenance: Provenance

    @model_validator(mode="after")
    def check_source(self) -> Self:
        if self.provenance.source != "acceptance_criteria":
            raise ValueError("Endpoint expectations must come from acceptance criteria")
        return self


class EndpointVerification(EvidenceModel):
    """Independent observations, not assertions copied from an agent/tool claim."""

    expectation_id: NonEmpty
    url: HttpUrl
    method: Literal["GET"] = "GET"
    status_code: HttpStatus | None = None
    response_body: str | None = None
    body_truncated: bool = Field(default=False, strict=True)
    transport_error: NonEmpty | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def check_observation(self) -> Self:
        if self.provenance.source != "independent_http_probe":
            raise ValueError("Endpoint verification requires independent probe provenance")
        if (self.status_code is None) == (self.transport_error is None):
            raise ValueError("Record either an HTTP response or a transport error")
        if self.status_code is None and (self.response_body is not None or self.body_truncated):
            raise ValueError("A transport error cannot include an HTTP response body")
        if self.body_truncated and self.response_body is None:
            raise ValueError("A truncated body must include the available body text")
        return self


class AgentRunEvidence(EvidenceModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: NonEmpty
    trace_id: NonEmpty
    environment: Literal["synthetic", "local", "sandbox", "production"]
    task: MessageEvidence
    final_claim: MessageEvidence
    tool_executions: tuple[ToolExecution, ...] = ()
    endpoint_expectation: EndpointExpectation
    endpoint_verifications: tuple[EndpointVerification, ...] = ()
    evidence_gaps: tuple[NonEmpty, ...] = ()

    @model_validator(mode="after")
    def check_correlations(self) -> Self:
        if self.task.role != "user" or self.final_claim.role != "assistant":
            raise ValueError("Task must be a user message and final claim an assistant message")
        items = [self.task, self.final_claim, self.endpoint_expectation,
                 *self.tool_executions, *self.endpoint_verifications]
        ids = [item.provenance.evidence_id for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("Evidence IDs must be unique within a run")
        if any(item.provenance.trace_id != self.trace_id for item in items):
            raise ValueError("Every evidence item must correlate to the run trace")
        call_ids = [tool.call_id for tool in self.tool_executions]
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("Tool call IDs must be unique within a run")
        expectation = self.endpoint_expectation
        for probe in self.endpoint_verifications:
            if probe.expectation_id != expectation.provenance.evidence_id:
                raise ValueError("Probe references an unknown expectation")
            if probe.url != expectation.url or probe.method != expectation.method:
                raise ValueError("Probe target must match the endpoint expectation")
            if probe.provenance.timestamp < expectation.provenance.timestamp:
                raise ValueError("Acceptance criteria must be recorded before verification")
        return self


def load_evidence(path: str | Path) -> AgentRunEvidence:
    """Load local normalized evidence. This does not redact raw logs or call services."""
    evidence_path = Path(path)
    try:
        return AgentRunEvidence.model_validate_json(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Cannot read evidence file: {evidence_path}") from exc
    except ValidationError as exc:
        # Omit input values: validation diagnostics should not echo evidence payloads.
        problems = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'run'}: {error['type']}"
            for error in exc.errors(include_input=False, include_context=False)
        )
        raise ValueError(f"Invalid evidence file {evidence_path}: {problems}") from None
