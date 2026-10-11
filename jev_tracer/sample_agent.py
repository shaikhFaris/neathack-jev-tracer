"""Local deployment agent implemented with LangChain tools and LangGraph.

This deterministic sample intentionally supports a buggy graph and a fixed
graph. It is a reproducible test application, not an LLM-powered deployment.
"""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, TypedDict
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from langchain.tools import tool
from langgraph.graph import END, START, StateGraph

from .evidence import AgentRunEvidence
from .neatlogs import probe_endpoint


@tool
def deploy_service(service: str) -> dict:
    """Simulate a deployment service accepting a local demo release."""
    return {"service": service, "deployment_id": "demo-release-1", "status": "accepted"}


@tool
def check_local_health(url: str, expected_status: int, body_contains: str) -> dict:
    """Check only the local demo health endpoint before claiming deployment success."""
    parts = urlsplit(url)
    if parts.hostname != "127.0.0.1" or parts.scheme != "http" or parts.username or parts.password:
        raise ValueError("The sample agent may only check its local demo endpoint.")
    try:
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", url) as response:
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 65536:
                        break
                text = bytes(content[:65536]).decode("utf-8", errors="replace")
                return {"healthy": response.status_code == expected_status and body_contains in text,
                        "status_code": response.status_code, "body": text,
                        "body_truncated": len(content) > 65536}
    except httpx.HTTPError as exc:
        return {"healthy": False, "error": type(exc).__name__}


class DeploymentState(TypedDict, total=False):
    user_task: str
    url: str
    expected_status: int
    body_contains: str
    deployment: dict
    health: dict
    final_claim: str
    tool_events: list[dict]


def build_sample_agent(verify_endpoint: bool = True):
    """Compile the graph; the buggy variant skips the health-check node."""
    def deploy(state):
        result = deploy_service.invoke({"service": "demo-service"})
        return {"deployment": result, "tool_events": [{"name": "deploy_service",
                 "arguments": {"service": "demo-service"}, "result": result,
                 "timestamp": datetime.now(timezone.utc).isoformat()}]}

    def verify(state):
        args = {"url": state["url"], "expected_status": state["expected_status"],
                "body_contains": state["body_contains"]}
        result = check_local_health.invoke(args)
        return {"health": result, "tool_events": [*state["tool_events"],
                {"name": "check_local_health", "arguments": args, "result": result,
                 "timestamp": datetime.now(timezone.utc).isoformat()}]}

    def finish(state):
        # The bug: queued deployment acceptance is treated as verified success.
        healthy = state.get("health", {}).get("healthy", False) if verify_endpoint else True
        return {"final_claim": "Deployment succeeded. The service is healthy and ready to use."
                if healthy else "Deployment failed health verification. The service is not healthy; success is not confirmed."}

    graph = StateGraph(DeploymentState)
    graph.add_node("deploy", deploy)
    graph.add_node("claim", finish)
    graph.add_edge(START, "deploy")
    if verify_endpoint:
        graph.add_node("verify_health", verify)
        graph.add_edge("deploy", "verify_health")
        graph.add_edge("verify_health", "claim")
    else:
        graph.add_edge("deploy", "claim")
    graph.add_edge("claim", END)
    return graph.compile()


def run_sample_agent(url: str, expected_status: int = 200,
                     body_contains: str = '"status":"healthy"', verify_endpoint: bool = True,
                     callbacks: list | None = None) -> dict:
    return build_sample_agent(verify_endpoint).invoke(
        {"url": url, "expected_status": expected_status, "body_contains": body_contains,
         "user_task": "Deploy the service and confirm HTTP 200 with a healthy status."},
        {"recursion_limit": 8, "callbacks": callbacks or []},
    )


@contextmanager
def local_endpoint(status: int = 503, body: str = '{"status":"unavailable"}'):
    """Loopback-only fixture server on an ephemeral port, always cleaned up."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = body.encode("utf-8")
            self.send_response(status if self.path == "/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/health"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def observe_sample(url: str, verify_endpoint: bool = False, callbacks: list | None = None) -> AgentRunEvidence:
    trace = f"local-deployment-{uuid4().hex[:12]}"
    before = datetime.now(timezone.utc).isoformat()
    state = run_sample_agent(url, verify_endpoint=verify_endpoint, callbacks=callbacks)

    def provenance(item: str, source: str, timestamp: str, span=None) -> dict[str, Any]:
        return {"evidence_id": item, "source": source, "timestamp": timestamp,
                "trace_id": trace, "span_id": span, "artifact_ref": f"local-demo://{trace}/{item}"}

    run = AgentRunEvidence.model_validate({
        "run_id": trace, "trace_id": trace, "environment": "local",
        "task": {"role": "user", "content": "Deploy the service and confirm HTTP 200 with a healthy status.",
                 "provenance": provenance("task", "conversation", before)},
        "final_claim": {"role": "assistant", "content": state["final_claim"],
                        "provenance": provenance("claim", "conversation", datetime.now(timezone.utc).isoformat())},
        "tool_executions": [{"call_id": f"call-{i}", "name": event["name"],
                             "arguments_json": json.dumps(event["arguments"]),
                             "result_json": json.dumps(event["result"]), "result_available": True,
                             "provenance": provenance(f"tool-{i}", "tool_execution", event["timestamp"], f"span-{i}")}
                            for i, event in enumerate(state["tool_events"])],
        "endpoint_expectation": {"url": url, "expected_status": 200,
                                 "expected_body_contains": '"status":"healthy"',
                                 "provenance": provenance("health-expectation", "acceptance_criteria", before)},
    })
    # Separate collector: the independent probe does not use the agent's tool result.
    return probe_endpoint(run, allowed_hosts=("127.0.0.1",))
