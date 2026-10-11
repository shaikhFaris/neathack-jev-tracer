# Jev Tracer

An evidence-based reliability engineer prototype for AI agents. The MVP catches
an agent claiming deployment success while an independently checked health
endpoint returns HTTP 503, then turns that finding into a regression test.

Agents use **LangChain tools and LangGraph workflows**. Collection, normalization,
redaction, and verifiable side-effect checks use ordinary deterministic code.

## Quick start

Python 3.12 and uv are required. From this directory:

```powershell
uv sync --locked
uv run --locked python -m jev_tracer demo --approve-demo-fix
```

Open `artifacts/demo/report.html`. The demo starts a temporary loopback HTTP
endpoint, runs the buggy deployment graph, independently verifies the failure,
generates a regression, and demonstrates assertion failure before the fix and
all three tests passing after it. No API keys or external services are needed.
`--approve-demo-fix` approves testing the fix in a temporary local sample copy.

To review before approving:

```powershell
uv run --locked python -m jev_tracer demo
# Review artifacts/demo/report.html and artifacts/demo/regression/proposed_fix.diff.
uv run --locked python -m jev_tracer approve-demo-fix artifacts/demo
```

The fixed sample graph checks HTTP status and required response content before
claiming success. The buggy graph treats deployment acceptance as success and
skips the health-check node. Production code is never edited by these commands.

## Recorded evidence and evaluation

```powershell
uv run --locked python -m jev_tracer evaluate fixtures/deployment/bad.json --output artifacts/bad
uv run --locked python -m jev_tracer evaluate fixtures/deployment/missing_verification.json --output artifacts/missing
uv run --locked python -m jev_tracer validate-fixtures
```

The normalized fixtures represent healthy, failed, and missing-verification
runs. Expected labels live separately in `fixtures/deployment/labels.json`.
Fixture validation reports a confusion matrix and false positives/negatives.
These three synthetic cases validate basic deterministic behavior; they do not
establish broad evaluator accuracy or calibrate Jevals.

Add `--investigate` to generate a local failure-class reproduction for a
corroborated silent failure. Add `--fail-on-failure` to `evaluate` for exit codes
0 (pass), 1 (fail), or 2 (insufficient evidence/error).

## Neatlogs ingestion

The read-only adapter uses the documented selected span payload endpoint.
Review a collection plan with actual trace/span IDs, original timestamps,
extraction paths, and acceptance criteria. The example plan expects task and
final-claim fields in an agent span and a separate deployment tool span.

Normalize synthetic recorded API responses without credentials:

```powershell
uv run --locked python -m jev_tracer collect --plan fixtures/neatlogs/collection-plan.json --recorded-payloads fixtures/neatlogs/payloads.json --output artifacts/recorded
```

For live reads, provide `NEATLOGS_TOKEN` and `NEATLOGS_PROJECT_ID` through the
local process environment. Use a service-account token with minimum project
access and the documented read scopes. An SDK ingest key is a different
credential. `NEATLOGS_BASE_URL` can select the documented EU Public API host.

```powershell
uv run --locked python -m jev_tracer collect --plan path/to/reviewed-plan.json --output artifacts/live
```

The collector only issues GET requests and does not follow redirects. It
preserves unavailable/truncated payloads as evidence limitations, correlates
trace/span IDs, bounds retries and payload sizes, and redacts before storage.
Original timestamps come from the reviewed plan because the selected-payload
response does not supply them.

To independently probe a reviewed endpoint, explicitly allow its hostname:

```powershell
uv run --locked python -m jev_tracer collect --plan path/to/reviewed-plan.json --probe-host your-demo-host.example --output artifacts/live
```

For automatic **per-request Neatlogs → Jev monitoring**, use:

```powershell
# Configure NEATLOGS_TOKEN, NEATLOGS_PROJECT_ID, NEATLOGS_API_KEY,
# and AI_GATEWAY_API_KEY in the local process environment first.
uv run --locked python -m jev_tracer monitor --jevals-backend vercel --watch
```

The monitor discovers finalized traces, reads full context using the documented
read-only Neatlogs MCP tool, redacts relevant evidence, and evaluates each request
with Jevals. Results and checkpoints are saved under `artifacts/neatlogs`;
open `index.html` for the request table. See the [monitor guide](docs/neatlogs-monitor.md)
for filters, policies, independent observations, credentials, and offline testing.

Probing an old trace now cannot establish the endpoint's state at the original
claim time; observations beyond the five-minute correlation window remain
insufficient evidence. The manual-plan adapter remains available; automatic
request collection uses full-context MCP because Public API span-list/export
response schemas could not be verified.

## Jevals and LLM investigation

Jevals 0.1.4 is pinned. Real evaluations require an explicit backend selection
and its credentials supplied locally:

```powershell
# AI_GATEWAY_API_KEY must already be set in the process environment.
uv run --locked python -m jev_tracer demo --jevals-backend vercel --approve-demo-fix
# Alternatively use --jevals-backend typesafe with TYPESAFE_API_KEY.
```

The integration runs built-in `Grounded` and `UsedToolResult`, plus custom
`SuccessSupported`, which explicitly sees selected independent verification.
Model results are reported separately. A negative model judgment can flag
suspicion; corroborated deterministic evidence establishes the demo failure.

`--jevals-backend mock` exercises the installed Jevals plumbing without a model
request. The report labels those results as mock and ignores them for the final
deterministic outcome.

Optional AI investigation uses LangChain `create_agent` inside the LangGraph
workflow. Configure `OPENAI_API_KEY` and `INVESTIGATOR_MODEL`, then enable it:

```powershell
uv run --locked python -m jev_tracer demo --llm-investigation --approve-demo-fix
```

Only selected redacted task/claim/tool/HTTP evidence is sent. The reasoning
agent has two read-only evidence tools, bounded execution, structured hypothesis
output, and checked evidence citations. Model output never becomes executable
code. Offline investigation remains available without an LLM.

`.env.example` lists environment variable names; it is not loaded automatically.
Keep credentials in the process environment or your secret manager.

## Artifacts and CI

Each output directory contains `evidence.json`, `evaluation.json`, and a
standalone escaped HTML report. Investigated failures also produce
`investigation.json`, `root_cause.json`, a generated regression, its observed
case, a proposed diff, and captured pre-/post-fix test output.

`regressions/deployment` contains a permanent generated regression against the
current sample agent. `.github/workflows/reliability.yml` runs unit tests,
the permanent regression, fixture validation, and the complete local demo on
pushes and pull requests, then uploads the evidence artifacts. The workflow
is prepared for review; hosted CI has not run in this non-Git workspace.

```powershell
uv run --locked --offline python -m unittest discover -s tests -v
uv run --locked --offline python -m unittest discover -s regressions/deployment -p 'test_*.py' -v
```

## Practical limits

A pass means no failure detected by these checks, not proof of overall correctness.
The deterministic claim classifier recognizes conservative demo phrases;
unsupported language stays insufficient evidence. Transport failures do not
prove a deployment is broken. Provenance records are validated for correlation,
but cannot authenticate a supplied fixture's asserted source.

Local reproduction establishes the sample graph's cause. It reproduces the
failure class for external evidence, not the internals of arbitrary production
agents. Temporary subprocesses run trusted template code; they are not an OS
security sandbox for executing unknown applications. Heuristic redaction does
not detect every kind of private data. Live Neatlogs and real model access remain
to be smoke-tested with configured credentials.

See [architecture](docs/architecture.md) and [verified interface notes](docs/interfaces.md).
