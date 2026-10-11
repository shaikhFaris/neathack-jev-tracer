# Initial interface research

Research date: 2026-10-11. These are documented contracts, not confirmation
that a particular project has working API access.

## Neatlogs: read-only collection

The [Public API](https://docs.neatlogs.com/api-reference) base URL is
`https://app.neatlogs.com/api/v1/public`. EU projects use the EU host and
matching credentials; availability must be checked for that deployment.

The [quickstart](https://docs.neatlogs.com/api-reference/quickstart) uses a
service-account Bearer token, the project UUID in `x-project-id`, and
`context:read` plus `observability:read` scopes. An SDK ingest credential
should not be assumed to grant Public API access.

The [trace reference](https://docs.neatlogs.com/api-reference/endpoints/traces)
documents GET operations for:

- `/traces`: lists traces; the quickstart documents `data.traces`,
  `data.page`, and cursor pagination through `page.nextCursor`.
- `/traces/{traceId}`: fetches a trace.
- `/traces/{traceId}/spans`: lists its spans.
- `/traces/{traceId}/spans/{spanId}/payload`: retrieves selected payloads.

The [payload contract](https://docs.neatlogs.com/api-reference/endpoints/traces/get_api_v1_public_traces_traceId_spans_spanId_payload)
requires a `fields` query selecting input, output, and/or error. Its response
has `data.traceId`, `data.spanId`, and `data.fields`. Each selected field has
`available`, `truncated`, `mimeType`, and a nullable string `value`.
Missing or truncated payloads must remain explicit evidence limitations;
do not infer successful tool execution from their absence.

Success responses use `success`, `requestId`, and `data`. Exact span-list
and export response schemas remain unverified because their detailed pages
were unavailable through the research browser and direct network downloads
were blocked. The implemented adapter uses only the verified selected-payload
contract with an explicit collection plan. No authenticated requests have
been made; synthetic contract examples and mocked HTTP responses cover it.

## Jevals: evaluation boundary

The [repository](https://github.com/openlayer-ai/jevals) documents
`evaluate(sample, evals, backend=...)` and async `aevaluate`. The example
passes `{"messages": messages, "tools": tools}` with OpenAI-style chat
messages, including assistant tool calls and tool responses.

Built-ins such as `Grounded`, `UsedToolResult`, and `GoalCompletion` are
candidates for evaluating behavior. Grounded checks claims against tool
results; a deployment tool reporting success cannot prove endpoint health.
Use an independent deterministic endpoint assertion for that invariant.

The documented custom-evaluator interface is `state(sample)`,
`questions(sample)`, and `reduce(answers, sample)`. Explicitly select relevant
redacted evidence in `state`; do not assume built-ins inspect arbitrary
external-state fields added to the sample.

The [package metadata](https://raw.githubusercontent.com/openlayer-ai/jevals/main/pyproject.toml)
declares version 0.1.4 and Python >=3.10 with httpx, pydantic, and pyyaml.
This project pins 0.1.4. The repository's `__version__` constant differs
from its package metadata, so use installed distribution metadata to verify
the version and inspect installed code before writing the integration.

The README documents a mock backend for plumbing tests and several real
backends. Mock results are not model validation. A real backend remains a
later configuration decision; no model request has been made.

## Evidence schema and fixtures (implemented)

`jev_tracer/evidence.py` defines typed `AgentRunEvidence` and a local JSON
loader. `fixtures/deployment` contains synthetic good, bad, and missing-
verification cases. The final agent claim is separate from observed HTTP
status and expected response content. Every evidence item preserves source,
timestamp, trace correlation, and an artifact reference. Separate fixture
labels use pass/fail/insufficient-evidence outcomes. The evaluator validates all
three labels with a confusion matrix and zero false positives/negatives on
these three cases. This is not broad quality calibration.

`neatlogs.py` implements reviewed payload extraction and read-only collection;
`redaction.py` applies before model access and persistence. Normalized deployment
fixtures are distinct from the synthetic Public API payload examples under
`fixtures/neatlogs`.

The local reproduction now uses a real loopback endpoint returning 503 while
a LangGraph deployment sample reports success after acceptance. Its generated
test fails on the buggy graph and passes on the fixed graph. A source hash and
graph node snapshots corroborate the sample's root cause. The permanent
generated regression and GitHub Actions workflow are prepared locally.

## LangChain / LangGraph

Installed versions were verified: LangChain 1.4.4, LangGraph 1.2.14, and
langchain-openai 1.7.0; all are locked by uv. The implementation follows the
official [StateGraph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
and [LangChain tools](https://docs.langchain.com/oss/python/langchain/tools).
The sample deployment graph and the investigation workflow compile and run.
Optional reasoning uses `create_agent` with `ToolStrategy(Hypothesis)`;
its tool-call/structured-output loop is verified with a fake chat model,
without claiming a real model evaluation.

## Automatic per-request collection (implemented)

The detailed Public API trace-list response is now verified: trace ID, session,
workflow, completion/finalization state, duration, span/tool/LLM counts, tokens,
and cost. Its cursor and filter contract supports automatic discovery.
The per-request collector combines that list with the documented read-only
MCP `get_trace_context` tree, instead of guessing the unavailable Public API
span-list/export response. See [monitor guide](neatlogs-monitor.md).

Collection preserves parent/child spans, messages, actual tool results, schemas
when captured, relevant logs, and retrieval evidence. Models receive selected
redacted evidence. External invariants come from independent observation
artifacts and owner criteria, not from an agent's own success claim. Each
finalized request gets one batched Jevals evaluation and its own local report.
Pending, incomplete, and failed-collection/model requests remain explicit.
No authenticated live Neatlogs or real Jev request has been made yet.
