"""Normalize documented Neatlogs context into task-relevant request evidence."""

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .neatlogs import CollectionError
from .neatlogs_requests import ContextSpan, TraceContext, TraceMetadata
from .redaction import known_secret_values, redact
from .request_evidence import (
    AgentRequestEvidence, InvariantObservation, RequestMessage, RequestPolicy,
    RequestSource, RequestSpan, RequestTool,
)

RELEVANT_METADATA = {
    "model", "input_tokens", "output_tokens", "tokens_used", "cost_usd", "retry_count",
    "tool_name", "tool_call_id", "code.filepath", "code.function", "code.lineno",
    "neatlogs.tool.name", "neatlogs.tool.description", "neatlogs.tool.parameters",
    "log.level", "neatlogs.retriever.query", "neatlogs.retriever.top_k",
}


def decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def path_value(value, path):
    value = decode(value)
    try:
        for key in path:
            value = decode(value)[key]
    except (KeyError, IndexError, TypeError):
        return None
    return value


def text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [part.get("text") for part in value if isinstance(part, dict) and part.get("type") in {"text", "output_text"}]
        if parts and all(isinstance(part, str) for part in parts):
            return "\n".join(parts)
    return json.dumps(value, ensure_ascii=False)


def conversation(value) -> list[dict]:
    """Recognize explicit OpenAI/LangChain chat records; do not infer roles from prose."""
    value = decode(value)
    if isinstance(value, dict):
        if "messages" in value:
            return conversation(value["messages"])
        if "choices" in value and isinstance(value["choices"], list) and value["choices"]:
            return conversation([value["choices"][0].get("message", {})])
        if "role" in value or "type" in value:
            value = [value]
        else:
            return []
    if not isinstance(value, list):
        return []
    result = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        message = entry.get("data", entry)
        if not isinstance(message, dict):
            continue
        role = message.get("role", {"human": "user", "ai": "assistant", "tool": "tool"}.get(entry_type))
        if role not in {"user", "assistant", "tool"}:
            continue  # Full system/developer prompts are intentionally not collected.
        calls = message.get("tool_calls", []) or []
        normalized_calls = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function", call)
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                continue
            args = function.get("arguments", function.get("args", {}))
            normalized_calls.append({"id": call.get("id"), "type": "function", "function": {
                "name": function["name"], "arguments": args if isinstance(args, str) else json.dumps(args)}})
        result.append({"role": role, "content": text(message.get("content")),
                       "tool_calls": normalized_calls, "tool_call_id": message.get("tool_call_id")})
    return result


def normalize_request(metadata: TraceMetadata, context: TraceContext,
                      policy: RequestPolicy | None = None,
                      invariants: tuple[InvariantObservation, ...] = (),
                      known_secrets: tuple[str, ...] = ()) -> AgentRequestEvidence:
    if metadata.traceId != context.trace_id:
        raise CollectionError("Trace metadata and full context refer to different requests.")
    policy = policy or RequestPolicy()
    gaps = []
    if metadata.finalizationStatus != "finalized" or metadata.completedAt is None:
        gaps.append("Trace is not completed and finalized; later spans may still arrive.")
    stack = [(context.root_span, None)]
    entries = []
    seen = set()
    while stack:
        span, parent = stack.pop()
        if span.span_id in seen:
            raise CollectionError("Trace context contains a duplicate or cyclic span ID.")
        seen.add(span.span_id)
        entries.append((span, parent))
        if len(entries) > 1000:
            raise CollectionError("Trace context exceeds the 1000-span collection limit.")
        stack.extend((child, span.span_id) for child in reversed(span.children))
    if len(entries) != context.span_count or len(entries) != metadata.spansCount:
        gaps.append("Reported span count differs from collected context; the context may be truncated.")
    if metadata.status != context.status:
        gaps.append("Trace status changed or differs between metadata and context.")

    def source(span=None):
        timestamp = (span.start_time if span is not None else metadata.startedAt) or metadata.startedAt
        if span is not None and span.start_time is None:
            gaps.append(f"Original timestamp missing for span {span.span_id}; using trace start with an explicit gap.")
        return RequestSource(source="neatlogs", trace_id=metadata.traceId,
                             span_id=span.span_id if span else None, timestamp=timestamp,
                             artifact_ref=f"neatlogs://traces/{quote(metadata.traceId, safe='')}" +
                             (f"/spans/{quote(span.span_id, safe='')}" if span else ""))

    entries.sort(key=lambda item: (item[0].start_time or metadata.startedAt, item[0].span_id))
    spans = []
    definitions = []
    for span, parent in entries:
        attrs = {k: v for k, v in span.metadata.items() if k in RELEVANT_METADATA}
        log_level = str(span.metadata.get("log.level", "")).lower() or None
        log_message = None
        retrieval_context = None
        locally_limited = False
        if span.span_type.upper() == "LOG" and (log_level in {"warning", "error", "critical"} or span.error):
            log_message = text(span.input)
            if log_message and len(log_message) > 4000:
                log_message, locally_limited = log_message[:4000], True
        elif span.span_type.upper() == "LOG" and log_level is None:
            gaps.append(f"Log severity is unavailable for span {span.span_id}; unclassified log payload was excluded.")
        if span.span_type.upper() in {"RETRIEVER", "RETRIEVAL"}:
            retrieval_context = text(span.metadata.get("neatlogs.retriever.documents", span.output))
            if retrieval_context and len(retrieval_context) > 4000:
                retrieval_context, locally_limited = retrieval_context[:4000], True
        if locally_limited:
            gaps.append(f"Relevant log/retrieval content was truncated for span {span.span_id}.")
        spans.append(RequestSpan(
            span_id=span.span_id, parent_span_id=parent, name=span.name, kind=span.span_type,
            status=span.status, started_at=span.start_time or metadata.startedAt, ended_at=span.end_time,
            duration_ms=span.latency_ms, input_available=span.input is not None,
            output_available=span.output is not None,
            input_truncated=bool(span.metadata.get("input_truncated", False)) or locally_limited,
            output_truncated=bool(span.metadata.get("output_truncated", False)) or locally_limited,
            error=span.error, model=str(span.metadata["model"]) if span.metadata.get("model") else None,
            log_level=log_level, log_message=log_message, retrieval_context=retrieval_context,
            attributes=attrs, provenance=source(span),
        ))
        payload = decode(span.input)
        if isinstance(payload, dict) and isinstance(payload.get("tools"), list):
            definitions.extend(item for item in payload["tools"] if isinstance(item, dict))
        parameters = decode(span.metadata.get("neatlogs.tool.parameters"))
        name = span.metadata.get("neatlogs.tool.name", span.metadata.get("tool_name"))
        if name and isinstance(parameters, dict):
            definitions.append({"type": "function", "function": {"name": str(name),
                "description": str(span.metadata.get("neatlogs.tool.description", "")), "parameters": parameters}})

    # A root LangGraph message state is authoritative when present. Otherwise
    # use a recorded final LLM input transcript plus its output, not every
    # cumulative prompt snapshot concatenated together.
    root = context.root_span
    root_output_messages = conversation(root.output)
    input_messages = conversation(root.input)
    records: list[tuple[dict, ContextSpan]] = []
    if any(message["role"] == "user" for message in root_output_messages):
        records = [(message, root) for message in root_output_messages]
    else:
        llm_entries = [span for span, _ in entries if span.span_type.upper() == "LLM"]
        complete_llm = [(span, conversation(span.input)) for span in llm_entries
                        if any(message["role"] == "user" for message in conversation(span.input))]
        if complete_llm:
            last, transcript = complete_llm[-1]
            records = [(message, last) for message in transcript]
            records.extend((message, last) for message in conversation(last.output))
            if len(complete_llm) > 1 and any(len(messages) > len(transcript) for _, messages in complete_llm[:-1]):
                gaps.append("Multiple LLM branches cannot be reconstructed as one complete conversation.")
        else:
            records = [(message, root) for message in input_messages]
            records.extend((message, root) for message in root_output_messages)
        if not any(message["role"] == "user" for message, _ in records):
            task = path_value(root.input, policy.task_path)
            if isinstance(task, str) and task.strip():
                records.insert(0, ({"role": "user", "content": task}, root))
        final = path_value(root.output, policy.final_path)
        last_assistant = next((message.get("content") for message, _ in reversed(records)
                               if message["role"] == "assistant" and not message.get("tool_calls")), None)
        if isinstance(final, str) and final.strip() and final != last_assistant:
            records.append(({"role": "assistant", "content": final}, root))

    messages = [RequestMessage(**message, provenance=source(span)) for message, span in records]
    if not any(message.role == "user" and message.content for message in messages):
        gaps.append("User task could not be recovered; configure task_path for a custom root payload.")
    if not any(message.role == "assistant" and message.content and not message.tool_calls for message in messages):
        gaps.append("Final assistant response could not be recovered; configure final_path for a custom root payload.")

    tools = []
    for span, _ in entries:
        if span.span_type.upper() not in {"TOOL", "MCP_TOOL"}:
            continue
        arguments = decode(span.input)
        if isinstance(arguments, dict) and "input" in arguments and len(arguments) == 1:
            arguments = decode(arguments["input"])
        tool_name = str(span.metadata.get("neatlogs.tool.name", span.metadata.get("tool_name", span.name)))
        call_id = span.metadata.get("tool_call_id")
        if not call_id:
            # Match only a unique recorded name+argument pair; ambiguity remains explicit.
            matches = [call.get("id") for message in messages for call in message.tool_calls
                       if call["function"]["name"] == tool_name and
                       decode(call["function"]["arguments"]) == arguments and call.get("id")]
            if len(matches) == 1:
                call_id = matches[0]
            else:
                call_id = span.span_id
        if arguments is None:
            gaps.append(f"Tool arguments unavailable for span {span.span_id}.")
        tools.append(RequestTool(
            call_id=span.span_id, agent_call_id=str(call_id), name=tool_name, arguments=arguments, result=decode(span.output),
            result_available=span.output is not None, result_truncated=bool(span.metadata.get("output_truncated", False)),
            error=span.error, provenance=source(span),
        ))
    if len(tools) != metadata.toolCallsCount:
        gaps.append("Reported tool call count differs from collected TOOL/MCP_TOOL spans.")
    if sum(span.span_type.upper() == "LLM" for span, _ in entries) != metadata.llmCallsCount:
        gaps.append("Reported LLM call count differs from collected LLM spans.")
    # Keep the last seen definition for a name, as requested tools may change mid-run.
    by_name = {}
    for definition in definitions:
        function = definition.get("function", definition)
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            if function["name"] in by_name and by_name[function["name"]] != definition:
                gaps.append("Tool definitions changed during the request; schema judgments need per-step review.")
            by_name[function["name"]] = definition
    value = AgentRequestEvidence(
        trace_id=metadata.traceId, session_id=metadata.sessionId, workflow_name=metadata.workflowName,
        status=metadata.status, finalization_status=metadata.finalizationStatus,
        started_at=metadata.startedAt, completed_at=metadata.completedAt,
        collected_at=datetime.now(timezone.utc), duration_ms=metadata.durationMs,
        total_cost_usd=metadata.totalCostUsd, total_tokens=metadata.totalTokens,
        messages=tuple(messages), tools=tuple(tools), tool_definitions=tuple(by_name.values()),
        spans=tuple(spans), invariants=invariants, policy=policy,
        evidence_gaps=tuple(dict.fromkeys(gaps)), provenance=source(),
    )
    return AgentRequestEvidence.model_validate(redact(value.model_dump(mode="json"),
                                               (*known_secret_values(), *known_secrets)))
