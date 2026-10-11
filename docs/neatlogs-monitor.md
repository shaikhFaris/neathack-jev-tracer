# Per-request Neatlogs to Jev monitoring

The CLI loads `.env` from the project root automatically. Existing terminal
variables take precedence. INFO progress logs go to stderr while the final JSON
summary remains on stdout. Logs show page discovery, pending requests, context
collection, normalization, Jev evaluation, outcomes, and polling waits. Trace
identifiers are represented by their artifact hashes; payloads and credentials
are omitted. Use `--log-level DEBUG` before the command to also see checkpoint
skips, or `--log-level WARNING` for quieter output:

```powershell
uv run --locked python -m jev_tracer --log-level DEBUG monitor --watch
```

The monitor discovers finalized requests through the Neatlogs Public API and
collects the full trace tree using its documented read-only MCP tool. Collection
and normalization use ordinary code; no agent chooses APIs or reshapes JSON.

## Interfaces verified from documentation

- [Trace list](https://docs.neatlogs.com/api-reference/endpoints/traces/get_api_v1_public_traces):
  `GET /api/v1/public/traces` with date/workflow/session filters, completion and
  finalization state, span/tool/LLM counts, duration, tokens, and cost.
- [Cursor pagination](https://docs.neatlogs.com/api-reference/pagination):
  pages contain `data.page.nextCursor`; copy it unchanged with the same filters.
  Page size is at most 50. The Public API does not use offset pagination.
- [Full context](https://docs.neatlogs.com/integrations/mcp-tools):
  `get_trace_context` supplies the root and child spans, actual inputs/outputs,
  errors, timestamps, and captured metadata.
- [MCP connection](https://docs.neatlogs.com/integrations/mcp): Streamable HTTP
  at the ingest host's `/mcp`, authenticated with the project API key. The client
  allows only `ping`, `whoami`, and `get_trace_context`. Protocol POST requests
  invoke read-only operations; no fix, deployment, or log-write tools are called.

The detailed Public API span/export pages remained unavailable to the research
browser. Their response format is not guessed: full-tree collection uses the
documented MCP interface instead. No live access has been verified yet.

## Credentials and live command

Supply these in the local process environment or secret manager:

- `NEATLOGS_TOKEN`: read-scoped Public API service-account token.
- `NEATLOGS_PROJECT_ID`: authorized project UUID.
- `NEATLOGS_API_KEY`: same project's key for the MCP interface.
- `AI_GATEWAY_API_KEY`: Jev through Vercel; alternatively `TYPESAFE_API_KEY`.

The Public API token and MCP key are distinct credentials. Project identity
is checked before reading contexts. The CLI automatically loads the project-root
`.env` without overriding existing process variables. `.env.example` is only a template.
EU projects need matching credentials and both EU endpoints:
`NEATLOGS_BASE_URL=https://eu.app.neatlogs.com/api/v1/public` and
`NEATLOGS_MCP_URL=https://eu.ingest.neatlogs.com/mcp`.

```powershell
uv run --locked python -m jev_tracer monitor --jevals-backend vercel --max-requests 50 --output artifacts/neatlogs
```

Filter and watch for new completed requests:

```powershell
uv run --locked python -m jev_tracer monitor --jevals-backend vercel --workflow your-workflow --from 2026-10-11T00:00:00Z --watch --poll-seconds 30
```

Use `--jevals-backend typesafe` for direct TypeSafe access. Ctrl+C retains
checkpoints and closes clients. Completed requests with unchanged metadata,
policy, observations, and backend are skipped; collection/model errors remain
retryable. `--force` re-evaluates existing requests. Each scan is bounded by
`--max-requests` and `--max-pages`. A continuation cursor is retained when a scan
stops early; watch mode continues that chain before restarting discovery.
Pending traces are revisited and are never checkpointed as successfully evaluated.

## Collected evidence and judgments

The normalizer selects user task, final answer, assistant tool calls, actual
executed tool arguments/results, captured tool definitions, span relationships,
failed steps, warning/error logs, retrieved context, and trace metrics.
Full system/developer prompts, arbitrary metadata, and debug dumps are excluded.
Data is redacted before storage or model use. Input bounds and missing/truncated
evidence remain explicit limitations.

Jevals batches applicable judgments into one request per trace: completion,
false success claims, grounding, use of tool results, tool choice and arguments,
loops, and indirect injection. Missing tool schemas are not invented. Token,
cost, and latency budgets use deterministic checks on trace metadata.

`--policy` reads a trusted JSON file with `default` criteria and optional
`workflows` overrides. See `fixtures/requests/policy.json`. Supported settings
include acceptance criteria, behavior instructions, independent-verification
requirements, budgets, and custom root task/final extraction paths. Trace text
never becomes monitoring instructions.

`--observations` reads a trace-ID map of independently verified business
invariants, as in `fixtures/requests/observations.json`. Each item preserves
source, timestamp, trace correlation, and an artifact reference. This is external
ground truth, not a tool's own success response. Collecting database/deployment
state requires the application's read-only adapter; Neatlogs cannot manufacture
missing ground truth.

Outcomes are pass/fail/insufficient evidence, with separate suspected-silent-
failure and corroborated-failure flags. A Jev concern is suspicion until
corroborated. Model errors, partial traces, unavailable conversations, and required
but absent external state stay insufficient evidence. A pass is not universal
proof of correctness.

## Artifacts and offline verification

Open `artifacts/neatlogs/index.html`. Each request directory contains
`metadata.json`, `evidence.json`, `jev-input.json`, `evaluation.json`, and
`report.html`, or an explicit collection-error artifact. Trace IDs are hashed
for directory names so they cannot select arbitrary filesystem paths.

```powershell
uv run --locked --offline python -m jev_tracer monitor --recorded-bundle fixtures/requests/neatlogs-bundle.json --policy fixtures/requests/policy.json --observations fixtures/requests/observations.json --jevals-backend mock --output artifacts/request-monitor
```

This synthetic bundle has two finalized requests and one pending request. One
independent observation corroborates a mismatch. Mock judgments are labeled and
cannot certify the other request as healthy. Tests use mocked HTTP responses
for Public API discovery and MCP JSON/SSE transport; they are not live smoke tests.

For application tracing, follow the [Python SDK documentation](https://docs.neatlogs.com/sdk/python).
LangChain/LangGraph uses `neatlogs.langchain_handler()` attached to the graph
invocation, not independently to every node. One root should represent one
application request. Capture relevant logs and tool schemas, and flush/shut down
telemetry at the documented application lifecycle boundary.

Conversation reconstruction supports explicit OpenAI-style messages, common
serialized LangChain human/AI/tool records, and reviewed custom root paths.
Unsupported formats and ambiguous branches remain gaps. Exact live responses
and real Jev behavior still need validation with configured credentials.
