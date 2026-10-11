"""Conservative redaction before persistence or model evaluation."""

import json
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASK = "[REDACTED]"
SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|"
    r"secret|credential|cookie|customer|email|phone|address|ssn|private[_-]?key)", re.I
)
PATTERNS = [
    re.compile(r"\b(?:sk-|nl_|nlt_|ghp_|github_pat_)[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}\b"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S),
]
ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*"
    r"(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&#]+)"
)
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+")


def known_secret_values() -> tuple[str, ...]:
    return tuple(value for key, value in os.environ.items()
                 if len(value) >= 8 and re.search(r"(?:TOKEN|SECRET|API_KEY|PASSWORD|CREDENTIAL)", key, re.I))


def redact_text(text: str, known_secrets: tuple[str, ...] | None = None) -> str:
    if known_secrets is None:
        known_secrets = known_secret_values()
    for secret in sorted(set(known_secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, MASK)
    text = ASSIGNMENT.sub(lambda match: f"{match.group(1)}={MASK}", text)
    text = BEARER.sub(f"Bearer {MASK}", text)
    for pattern in PATTERNS:
        text = pattern.sub(MASK, text)
    return text


def redact_url(value: str) -> str:
    """Strip URL credentials and mask sensitive query parameters."""
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    query = urlencode([(k, MASK if SENSITIVE_KEY.search(k) else v)
                       for k, v in parse_qsl(parts.query, keep_blank_values=True)])
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact(value: Any, known_secrets: tuple[str, ...] | None = None) -> Any:
    if known_secrets is None:
        known_secrets = known_secret_values()
    if isinstance(value, dict):
        return {k: MASK if SENSITIVE_KEY.search(str(k)) else redact(v, known_secrets)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, known_secrets) for v in value]
    if isinstance(value, str):
        # Nested tool results are often JSON strings. Preserve valid JSON after redaction.
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                pass
            else:
                clean = redact(parsed, known_secrets)
                # Preserve exact raw response formatting when nothing is sensitive;
                # acceptance criteria may intentionally match an exact body fragment.
                return value if clean == parsed else json.dumps(clean, ensure_ascii=False)
        if value.startswith(("https://", "http://", "synthetic://")):
            value = redact_url(value)
        return redact_text(value, known_secrets)
    return value
