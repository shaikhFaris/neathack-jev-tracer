"""CLI environment loading and safe progress output."""

import logging
from pathlib import Path

from dotenv import load_dotenv

from .redaction import redact_text


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact_text(super().format(record)).replace("\r", "\\r").replace("\n", "\\n")


def configure_runtime(root: Path, level: str = "INFO", stream=None):
    # Explicit terminal/production variables take precedence over local values.
    load_dotenv(root / ".env", override=False, encoding="utf-8-sig")
    logger = logging.getLogger("jev_tracer")
    logger.setLevel(level)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logger.handlers.clear()
    logger.addHandler(handler)
    return logger
