"""Discover requests through the Public API and read documented full MCP context.

No autonomous agent is used for collection. MCP calls are restricted to
connection checks and get_trace_context; mutation tools are never exposed.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError

from .neatlogs import CollectionError, NeatlogsClient


class ExternalModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TraceMetadata(ExternalModel):
    traceId: str
    sessionId: str
    workflowName: str | None
    traceName: str | None
    status: str
    finalizationStatus: Literal["pending", "finalized", "dlq"] | None
    startedAt: AwareDatetime
    completedAt: AwareDatetime | None
    ingestedAt: AwareDatetime
    durationMs: float = Field(ge=0)
    spansCount: int = Field(ge=0)
    llmCallsCount: int = Field(ge=0)
    toolCallsCount: int = Field(ge=0)
    totalCostUsd: float = Field(ge=0)
    totalTokens: int = Field(ge=0)
    framework: str | None
    tags: list[str]


class TracePage(ExternalModel):
    hasMore: bool
    nextCursor: str | None
    limit: int = Field(ge=1, le=50)


class TracePageData(ExternalModel):
    traces: list[TraceMetadata]
    page: TracePage


class TracePageResponse(ExternalModel):
    success: Literal[True]
    requestId: str
    data: TracePageData


class ContextSpan(ExternalModel):
    span_id: str
    span_type: str
    name: str
    status: str
    start_time: AwareDatetime | None = None
    end_time: AwareDatetime | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    input: JsonValue = None
    output: JsonValue = None
    error: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    children: list["ContextSpan"] = Field(default_factory=list)


class TraceContext(ExternalModel):
    trace_id: str
    name: str
    status: str
    created_at: AwareDatetime
    root_span: ContextSpan
    span_count: int = Field(ge=0)
    total_latency_ms: float = Field(ge=0)
    total_cost: float = Field(ge=0)


def trace_page(client: NeatlogsClient, *, limit=50, cursor=None, from_time=None,
               to_time=None, workflow_name=None, session_id=None) -> TracePageResponse:
    if not 1 <= limit <= 50:
        raise CollectionError("Trace page size must be between 1 and 50.")
    params = {"limit": str(limit)}
    for name, value in [("cursor", cursor), ("from", from_time), ("to", to_time),
                        ("workflowName", workflow_name), ("sessionId", session_id)]:
        if value is not None:
            params[name] = value
    # Reuse the same scoped client. No provider/MCP key is sent to this host.
    for attempt in range(3):
        try:
            with client.client.stream("GET", client.base_url + "/traces", params=params) as response:
                if response.status_code == 200:
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 4_000_000:
                            raise CollectionError("Neatlogs trace page exceeded the 4 MB limit.")
                        chunks.append(chunk)
                    try:
                        return TracePageResponse.model_validate_json(b"".join(chunks))
                    except ValidationError:
                        raise CollectionError("Trace page does not match the documented Neatlogs contract.") from None
                if response.status_code not in {429, 500, 502, 503, 504}:
                    raise CollectionError(f"Neatlogs trace listing failed with HTTP {response.status_code}.")
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    # Do not retry earlier than the server allows; leave rescheduling to the monitor.
                    raise CollectionError("Neatlogs trace listing is rate-limited or unavailable; retry after the server delay.")
        except httpx.HTTPError:
            if attempt == 2:
                raise CollectionError("Neatlogs trace listing failed after bounded retries.") from None
    raise CollectionError("Neatlogs trace listing failed after bounded retries.")


class NeatlogsContextClient:
    """Minimal bounded Streamable HTTP client for documented read-only tools."""
    ALLOWED_TOOLS = {"ping", "whoami", "get_trace_context"}
    SUPPORTED_VERSIONS = {"2025-03-26", "2025-06-18", "2025-11-25"}

    def __init__(self, api_key: str, endpoint="https://ingest.neatlogs.com/mcp",
                 transport: httpx.BaseTransport | None = None):
        parts = urlsplit(endpoint)
        if not api_key:
            raise CollectionError("Set NEATLOGS_API_KEY for the documented full-context MCP interface.")
        if parts.scheme != "https" or parts.hostname not in {"ingest.neatlogs.com", "eu.ingest.neatlogs.com"} or parts.path != "/mcp" or parts.username or parts.password or parts.query or parts.fragment or parts.port not in {None, 443}:
            raise CollectionError("Use the documented US/EU Neatlogs MCP endpoint.")
        self.endpoint, self.api_key = endpoint, api_key
        self.session_id = None
        self.initialized = False
        self.request_id = 0
        self.client = httpx.Client(timeout=20, follow_redirects=False, trust_env=False, transport=transport,
                                   headers={"Authorization": f"Bearer {api_key}",
                                            "Accept": "application/json, text/event-stream"})

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("NEATLOGS_API_KEY", ""),
                   os.environ.get("NEATLOGS_MCP_URL", "https://ingest.neatlogs.com/mcp"))

    def close(self):
        self.client.close()

    def _rpc(self, method: str, params: dict | None = None, notification=False):
        self.request_id += 1
        request_id = self.request_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            message["id"] = request_id
        if params is not None:
            message["params"] = params
        try:
            with self.client.stream("POST", self.endpoint, json=message) as response:
                if response.status_code == 202 and notification:
                    return None
                if response.status_code != 200:
                    raise CollectionError(f"Neatlogs context request failed with HTTP {response.status_code}.")
                session_id = response.headers.get("Mcp-Session-Id")
                if method == "initialize" and session_id:
                    self.session_id = session_id
                    self.client.headers["Mcp-Session-Id"] = session_id
                size = 0
                def matching(value):
                    nonlocal size
                    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                        return None
                    if value.get("id") != request_id:
                        return None
                    if "error" in value:
                        raise CollectionError("Neatlogs context RPC returned an error.")
                    if "result" not in value:
                        raise CollectionError("Neatlogs context RPC result is missing.")
                    return value["result"]
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    data_lines = []
                    for line in response.iter_lines():
                        size += len(line.encode("utf-8"))
                        if size > 10_000_000:
                            raise CollectionError("Neatlogs context exceeded the 10 MB limit.")
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                        elif line == "" and data_lines:
                            value = matching(json.loads("\n".join(data_lines)))
                            data_lines = []
                            if value is not None:
                                return value
                    if data_lines:
                        value = matching(json.loads("\n".join(data_lines)))
                        if value is not None:
                            return value
                else:
                    chunks = []
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 10_000_000:
                            raise CollectionError("Neatlogs context exceeded the 10 MB limit.")
                        chunks.append(chunk)
                    value = matching(json.loads(b"".join(chunks)))
                    if value is not None:
                        return value
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeError):
            raise CollectionError("Neatlogs context request or response failed.") from None
        raise CollectionError("Neatlogs context response did not correlate to the RPC request.")

    def initialize(self):
        result = self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "jev-tracer-read-only", "version": "0.1.0"}})
        if not isinstance(result, dict) or result.get("protocolVersion") not in self.SUPPORTED_VERSIONS:
            raise CollectionError("Neatlogs MCP negotiated an unsupported protocol version.")
        self.client.headers["MCP-Protocol-Version"] = result["protocolVersion"]
        self._rpc("notifications/initialized", notification=True)
        self.initialized = True

    def call_read_tool(self, name: str, arguments: dict) -> dict:
        if name not in self.ALLOWED_TOOLS:
            raise CollectionError("Collector refused a tool outside its read-only allowlist.")
        if not self.initialized:
            self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict) or result.get("isError"):
            raise CollectionError("Neatlogs read-only context tool returned an error.")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content", [])
        texts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        try:
            value = json.loads("\n".join(texts))
        except json.JSONDecodeError:
            raise CollectionError("Neatlogs context tool did not return documented JSON content.") from None
        if not isinstance(value, dict):
            raise CollectionError("Neatlogs context tool returned a non-object payload.")
        return value

    def context(self, trace_id: str) -> TraceContext:
        try:
            context = TraceContext.model_validate(self.call_read_tool("get_trace_context", {"trace_id": trace_id}))
        except ValidationError:
            raise CollectionError("Neatlogs full trace context does not match the documented shape.") from None
        if context.trace_id != trace_id:
            raise CollectionError("Neatlogs full context trace correlation mismatch.")
        return context
