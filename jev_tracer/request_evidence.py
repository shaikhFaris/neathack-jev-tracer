"""General evidence for one agent request, independent of the deployment demo."""

from typing import Any, Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from .evidence import EvidenceModel, NonEmpty


class RequestSource(EvidenceModel):
    source: Literal["neatlogs", "acceptance_criteria", "independent_verification"]
    trace_id: NonEmpty
    span_id: NonEmpty | None = None
    timestamp: AwareDatetime
    artifact_ref: NonEmpty


class RequestMessage(EvidenceModel):
    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[dict[str, JsonValue], ...] = ()
    tool_call_id: str | None = None
    provenance: RequestSource


class RequestTool(EvidenceModel):
    call_id: NonEmpty
    agent_call_id: str | None = None
    name: NonEmpty
    arguments: JsonValue
    result: JsonValue = None
    result_available: bool = False
    result_truncated: bool = False
    error: str | None = None
    provenance: RequestSource


class RequestSpan(EvidenceModel):
    span_id: NonEmpty
    parent_span_id: str | None
    name: str
    kind: str
    status: str
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    input_available: bool = False
    output_available: bool = False
    input_truncated: bool = False
    output_truncated: bool = False
    error: str | None = None
    model: str | None = None
    log_level: str | None = None
    log_message: str | None = None
    retrieval_context: str | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    provenance: RequestSource


class InvariantObservation(EvidenceModel):
    """Read-only external observation supplied independently of the agent trace."""
    name: NonEmpty
    expected: JsonValue
    observed: JsonValue
    provenance: RequestSource

    @model_validator(mode="after")
    def require_independent_source(self):
        if self.provenance.source != "independent_verification":
            raise ValueError("Business invariants require independent verification provenance")
        return self


class RequestPolicy(EvidenceModel):
    # Trusted owner-supplied expectations, never instructions extracted from trace text.
    acceptance_criteria: tuple[NonEmpty, ...] = ()
    behavior_instructions: tuple[NonEmpty, ...] = ()
    require_independent_verification: bool = False
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_duration_ms: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)
    # Workflow-specific root payload paths for applications without chat messages.
    task_path: tuple[str | int, ...] = ()
    final_path: tuple[str | int, ...] = ()


class AgentRequestEvidence(EvidenceModel):
    schema_version: Literal["request-1.0"] = "request-1.0"
    trace_id: NonEmpty
    session_id: str | None
    workflow_name: str | None
    status: str
    finalization_status: str | None
    started_at: AwareDatetime
    completed_at: AwareDatetime | None
    collected_at: AwareDatetime
    duration_ms: float = Field(ge=0)
    total_cost_usd: float = Field(ge=0)
    total_tokens: int = Field(ge=0)
    messages: tuple[RequestMessage, ...] = ()
    tools: tuple[RequestTool, ...] = ()
    tool_definitions: tuple[dict[str, JsonValue], ...] = ()
    spans: tuple[RequestSpan, ...] = ()
    invariants: tuple[InvariantObservation, ...] = ()
    policy: RequestPolicy = Field(default_factory=RequestPolicy)
    evidence_gaps: tuple[str, ...] = ()
    provenance: RequestSource

    @model_validator(mode="after")
    def require_trace_correlation(self):
        for item in [*self.messages, *self.tools, *self.spans, *self.invariants, self]:
            if item.provenance.trace_id != self.trace_id:
                raise ValueError("Every request evidence item must correlate to the trace")
        ids = [span.span_id for span in self.spans]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate span IDs in request evidence")
        call_ids = [tool.call_id for tool in self.tools]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Duplicate tool call IDs in request evidence")
        return self

    @property
    def final_answer(self) -> str:
        return next((message.content for message in reversed(self.messages)
                     if message.role == "assistant" and message.content and not message.tool_calls), "")

    @property
    def user_task(self) -> str:
        return next((message.content for message in reversed(self.messages)
                     if message.role == "user" and message.content), "")
