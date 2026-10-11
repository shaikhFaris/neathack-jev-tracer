# Generated deployment regression

This regression was generated from the local sample run's independently observed
HTTP 503 response. `case.json` records its criteria and evidence identifiers.
It asserts that a broken endpoint or incorrect response body prevents a success
claim, while a matching endpoint permits success.

`agent_target.py` calls the current sample agent's default behavior. CI executes
this committed test on every push and pull request. The demo separately runs
the same invariant against buggy and fixed graph variants to demonstrate
failure before the fix and pass after it.

```powershell
uv run --locked python -m unittest discover -s regressions/deployment -p 'test_*.py' -v
```

`proposed_fix.diff` shows the change used in the temporary sample comparison.
The local sample is not the original code of an arbitrary production agent.
