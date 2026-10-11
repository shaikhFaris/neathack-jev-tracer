"""CLI for the complete local MVP and explicit live integrations."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .evidence import AgentRunEvidence, load_evidence
from .evaluation import evaluate_run, validate_fixtures
from .neatlogs import NeatlogsClient, PayloadResponse, collect_run, load_plan, normalize_payloads, probe_endpoint
from .redaction import redact
from .reporting import persist_report, render_report, write_json

ROOT = Path(__file__).resolve().parents[1]


def optional_model(enabled: bool):
    if not enabled:
        return None
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("INVESTIGATOR_MODEL"):
        raise ValueError("Set OPENAI_API_KEY and INVESTIGATOR_MODEL locally to enable LLM investigation.")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=os.environ["INVESTIGATOR_MODEL"], temperature=0, timeout=20, max_retries=1)


def run_pipeline(run, args, investigate_failure=False):
    # Redaction precedes all persistence and model access.
    run = AgentRunEvidence.model_validate(redact(run.model_dump(mode="json")))
    report = evaluate_run(run, backend=args.jevals_backend)
    folder = Path(args.output).resolve()
    persist_report(folder, run, report)
    investigation = None
    if investigate_failure:
        from .investigator import investigate

        investigation = investigate(run, report, folder, getattr(args, "approve_demo_fix", False),
                                    model=optional_model(getattr(args, "llm_investigation", False)))
        render_report(folder, run, report, investigation)
    summary = {"run_id": run.run_id, "outcome": report.outcome.value,
               "silent_failure": report.silent_failure, "jevals_mode": report.jevals_mode,
               "report": str(folder / "report.html")}
    if investigation:
        summary.update(approval_required=investigation.get("approval_required", False),
                       red_before_green_verified=investigation.get("red_before_green_verified", False))
    print(json.dumps(summary, indent=2))
    return report


def parser():
    root = argparse.ArgumentParser(description="Turn corroborated agent failures into regression tests.")
    root.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    commands = root.add_subparsers(dest="command", required=True)

    def common(command):
        command.add_argument("--output", default="artifacts/latest")
        command.add_argument("--jevals-backend", default=None,
                             help="Explicit backend, e.g. vercel, typesafe, or mock. Default: deterministic checks only.")
        command.add_argument("--llm-investigation", action="store_true",
                             help="Send selected redacted evidence to the configured LangChain chat model.")
        command.add_argument("--approve-demo-fix", action="store_true",
                             help="Approve testing the proposed fix in a temporary local sample copy.")

    demo = commands.add_parser("demo", help="Run the local HTTP failure, detection, and regression workflow.")
    common(demo)
    demo.set_defaults(output="artifacts/demo")
    demo.add_argument("--neatlogs", action="store_true", help="Export the synthetic demo graph through the Neatlogs SDK.")

    evaluate = commands.add_parser("evaluate", help="Evaluate an already-normalized recorded run.")
    common(evaluate)
    evaluate.add_argument("evidence")
    evaluate.add_argument("--investigate", action="store_true")
    evaluate.add_argument("--fail-on-failure", action="store_true", help="Return exit 1 for failure, 2 for insufficient evidence.")

    collect = commands.add_parser("collect", help="Read reviewed Neatlogs span payloads using a collection plan.")
    common(collect)
    collect.add_argument("--plan", required=True)
    collect.add_argument("--recorded-payloads", help="Normalize a local JSON map of span IDs to recorded Public API responses.")
    collect.add_argument("--probe-host", action="append", default=[],
                         help="Explicitly allow a host for an independent GET; omitted means no probe.")
    collect.add_argument("--investigate", action="store_true")

    approve = commands.add_parser("approve-demo-fix", help="Verify a proposed local sample fix after artifact review.")
    approve.add_argument("folder", help="Artifact folder containing evidence.json and evaluation.json.")

    validate = commands.add_parser("validate-fixtures", help="Check synthetic labels and report confusion matrix.")
    validate.add_argument("--folder", default=str(ROOT / "fixtures" / "deployment"))
    validate.add_argument("--output", default="artifacts/fixture-validation.json")

    monitor = commands.add_parser("monitor", help="Discover finalized Neatlogs requests and evaluate each with Jev.")
    monitor.add_argument("--output", default="artifacts/neatlogs")
    monitor.add_argument("--jevals-backend", default="vercel", help="Real Jev backend (vercel/typesafe), or explicit mock for offline plumbing.")
    monitor.add_argument("--from", dest="from_time")
    monitor.add_argument("--to", dest="to_time")
    monitor.add_argument("--workflow", dest="workflow_name")
    monitor.add_argument("--session", dest="session_id")
    monitor.add_argument("--cursor")
    monitor.add_argument("--max-requests", type=int, default=50)
    monitor.add_argument("--max-pages", type=int, default=20)
    monitor.add_argument("--policy", help="Trusted JSON with default RequestPolicy and optional workflows overrides.")
    monitor.add_argument("--observations", help="JSON mapping trace IDs to independently verified business invariants.")
    monitor.add_argument("--recorded-bundle", help="Recorded Public API pages and MCP contexts for offline testing.")
    monitor.add_argument("--watch", action="store_true", help="Keep polling for completed requests until Ctrl+C.")
    monitor.add_argument("--poll-seconds", type=float, default=30)
    monitor.add_argument("--fail-on-failure", action="store_true")
    monitor.add_argument("--force", action="store_true", help="Re-evaluate previously checkpointed requests.")
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    from .runtime import configure_runtime

    logger = configure_runtime(ROOT, args.log_level)
    try:
        if args.command == "demo":
            from .sample_agent import local_endpoint, observe_sample
            from .demo_tracing import demo_callbacks

            with demo_callbacks(args.neatlogs) as callbacks:
                logger.info("Running local demo agent; Neatlogs export=%s", args.neatlogs)
                with local_endpoint() as url:
                    run = observe_sample(url, verify_endpoint=False, callbacks=callbacks)
                logger.info("Demo execution and independent endpoint verification complete")
            run_pipeline(run, args, investigate_failure=True)
        elif args.command == "evaluate":
            report = run_pipeline(load_evidence(args.evidence), args, args.investigate)
            if args.fail_on_failure:
                return {"pass": 0, "fail": 1, "insufficient_evidence": 2}[report.outcome.value]
        elif args.command == "collect":
            plan = load_plan(args.plan)
            if args.recorded_payloads:
                recorded = json.loads(Path(args.recorded_payloads).read_text(encoding="utf-8"))
                run = normalize_payloads(plan, {span: PayloadResponse.model_validate(payload)
                                               for span, payload in recorded.items()})
            else:
                client = NeatlogsClient.from_env()
                try:
                    run = collect_run(client, plan)
                finally:
                    client.close()
            if args.probe_host:
                run = probe_endpoint(run, tuple(args.probe_host))
            run_pipeline(run, args, args.investigate)
        elif args.command == "approve-demo-fix":
            from .investigator import investigate

            folder = Path(args.folder).resolve()
            run = load_evidence(folder / "evidence.json")
            report = evaluate_run(run)
            if not report.silent_failure:
                raise ValueError("The recorded evidence no longer corroborates the proposed silent failure.")
            # Rebuild artifacts from typed evidence; do not execute a tampered proposal/test file.
            result = investigate(run, report, folder, approve_demo_fix=True)
            persist_report(folder, run, report)
            render_report(folder, run, report, result)
            print(json.dumps({"red_before_green_verified": result.get("red_before_green_verified", False),
                              "report": str(folder / "report.html")}, indent=2))
        elif args.command == "validate-fixtures":
            result = validate_fixtures(args.folder)
            write_json(Path(args.output), result)
            print(json.dumps(result, indent=2))
            return 0 if result["all_correct"] else 1
        elif args.command == "monitor":
            from pydantic import AwareDatetime, TypeAdapter
            from .monitor import LiveRequestSource, RecordedRequestSource, scan_requests
            from .request_evidence import InvariantObservation, RequestPolicy

            for value in [args.from_time, args.to_time]:
                if value:
                    TypeAdapter(AwareDatetime).validate_python(value)
            if args.from_time and args.to_time and TypeAdapter(AwareDatetime).validate_python(args.from_time) > TypeAdapter(AwareDatetime).validate_python(args.to_time):
                raise ValueError("The from timestamp must precede the to timestamp.")
            if not 1 <= args.poll_seconds <= 60:
                raise ValueError("Poll interval must be between 1 and 60 seconds.")
            if args.recorded_bundle and args.watch:
                raise ValueError("Watch mode requires live Neatlogs data, not a fixed recorded bundle.")
            config = json.loads(Path(args.policy).read_text(encoding="utf-8")) if args.policy else {}
            if not isinstance(config, dict) or set(config) - {"default", "workflows"}:
                raise ValueError("Monitor policy must contain only default and workflows objects.")
            policy = RequestPolicy.model_validate(config.get("default", {}))
            workflows = config.get("workflows", {})
            for override in workflows.values():
                RequestPolicy.model_validate({**policy.model_dump(), **override})
            observations = {}
            if args.observations:
                observations = {trace: tuple(InvariantObservation.model_validate(item) for item in items)
                                for trace, items in json.loads(Path(args.observations).read_text(encoding="utf-8")).items()}
            logger.info("Opening %s trace source", "recorded" if args.recorded_bundle else "Neatlogs")
            source = RecordedRequestSource(Path(args.recorded_bundle)) if args.recorded_bundle else LiveRequestSource()
            logger.info("Trace source ready")
            continuation = args.cursor
            try:
                while True:
                    summary = scan_requests(source, Path(args.output).resolve(), args.jevals_backend,
                                            policy, workflows, observations, args.max_requests, args.max_pages,
                                            continuation, args.from_time, args.to_time,
                                            args.workflow_name, args.session_id, args.force)
                    print(json.dumps(summary, indent=2), flush=True)
                    if not args.watch:
                        if args.fail_on_failure:
                            return 1 if summary["outcomes"]["fail"] else 2 if summary["outcomes"]["insufficient_evidence"] else 0
                        break
                    continuation = summary["next_cursor"] if summary["has_more"] else None
                    logger.info("Waiting %.1f seconds before the next scan", args.poll_seconds)
                    time.sleep(args.poll_seconds)
            finally:
                source.close()
        return 0
    except KeyboardInterrupt:
        print("Monitoring stopped; persisted request checkpoints are retained.")
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        # Validation errors from input models can contain raw payloads; do not print those.
        from pydantic import ValidationError

        message = "Input failed schema validation." if isinstance(exc, ValidationError) else str(exc)
        print(f"Error: {redact(message)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
