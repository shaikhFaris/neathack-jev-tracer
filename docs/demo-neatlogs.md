# Export the demo to Neatlogs

The SDK is an optional installation so offline demos remain usable. In your
configured PowerShell terminal, install and check the SDK:

```powershell
uv pip install "neatlogs[langchain]"
.venv\Scripts\python.exe -m neatlogs doctor --local --json
.venv\Scripts\python.exe -m jev_tracer demo --neatlogs
```

The graph invocation gets the documented Neatlogs callback; the SDK flushes
and shuts down at the end. Synthetic task, tool results, and final claim are
exported under workflow `jev-demo-deployment`. No LLM key is needed to run this
deterministic sample. The CLI loads the project-root `.env` automatically;
existing process variables take precedence. Use the interpreter directly
after installing this optional SDK; a later `uv sync` can remove it.

Inspect that trace in Neatlogs, then evaluate it:

```powershell
.venv\Scripts\python.exe -m jev_tracer monitor --workflow jev-demo-deployment --policy fixtures/requests/demo-policy.json --jevals-backend vercel --output artifacts/live-demo --force
```

Open `artifacts/live-demo/index.html`. Finalization can take a few seconds;
rerun if the request is pending. The policy extracts custom graph state fields
`user_task` and `final_claim`; verify their shape on the actual exported trace.
Independent endpoint evidence is saved in `artifacts/demo/evidence.json` by the
local demo, but is not automatically correlated to the Neatlogs trace. Until
that adapter exists, live monitoring can flag suspicion but reports insufficient
evidence rather than a confirmed endpoint failure. The local demo's report
does independently confirm the HTTP failure.

SDK reference: https://docs.neatlogs.com/sdk/python
