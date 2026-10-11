"""Opt-in Neatlogs export for the synthetic local LangGraph demo."""

import os
import logging
from contextlib import contextmanager
from urllib.parse import urlsplit

from .redaction import redact


@contextmanager
def demo_callbacks(enabled: bool):
    if not enabled:
        yield []
        return
    if not os.environ.get("NEATLOGS_API_KEY"):
        raise ValueError("Set NEATLOGS_API_KEY in the terminal running the demo.")
    try:
        import neatlogs
    except ImportError:
        raise ValueError('Install the tracing SDK with: uv pip install "neatlogs[langchain]"') from None
    endpoint = os.environ.get("NEATLOGS_MCP_URL", "https://ingest.neatlogs.com/mcp")
    parts = urlsplit(endpoint)
    if (parts.scheme != "https" or parts.hostname not in {"ingest.neatlogs.com", "eu.ingest.neatlogs.com"}
            or parts.port not in {None, 443} or parts.username or parts.password
            or parts.path != "/mcp" or parts.query or parts.fragment):
        raise ValueError("NEATLOGS_MCP_URL must be a hosted Neatlogs HTTPS /mcp endpoint.")
    neatlogs.init(api_key=os.environ["NEATLOGS_API_KEY"],
                  endpoint=f"https://{parts.hostname}", workflow_name="jev-demo-deployment",
                  mask=redact)
    try:
        yield [neatlogs.langchain_handler()]
    finally:
        try:
            logging.getLogger(__name__).info("Flushing demo traces to Neatlogs")
            neatlogs.flush()
        finally:
            neatlogs.shutdown()
            logging.getLogger(__name__).info("Neatlogs SDK shutdown complete; verify trace export in the dashboard")
