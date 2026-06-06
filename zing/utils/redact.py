"""Redaction helpers.

zing handles real API keys and provider responses. Reports and logs must never
leak secrets, so every value that crosses an output boundary is scrubbed here.

Two sentinels are used so a redacted report can still carry meaning:

* ``REDACTED`` masks secret-looking substrings matched by pattern (other people's
  keys echoed in an error body, bearer tokens, etc.).
* ``REDACTED_KEY`` masks the *caller's own* configured API key. The security
  detector looks for this sentinel to tell whether the relay echoed the key back
  — detection survives redaction without the raw key ever reaching a report.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

REDACTED = "«redacted»"
REDACTED_KEY = "«redacted-key»"

# A configured key shorter than this is not pattern-scrubbed: short strings cause
# collateral redaction of ordinary text. Real provider keys are far longer.
_MIN_SECRET_LEN = 6

# Sensitive response/request headers that should be masked in reports.
_SENSITIVE_HEADERS = {
    "authorization",
    "x-api-key",
    "api-key",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "openai-organization",
    "x-goog-api-key",
}

# Patterns for secrets that may appear inside free-form text (error bodies, etc.).
_SECRET_PATTERNS = [
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}\b"),      # Anthropic-style (before sk-)
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),          # OpenAI-style
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b"),        # Google API keys
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
]


def fingerprint_secret(secret: str | None) -> str | None:
    """Return a short, non-reversible fingerprint of a secret for correlation.

    Lets a report say "same key as run X" without ever storing the key itself.
    """
    if not secret:
        return None
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


def mask_secret(secret: str | None) -> str:
    """Mask a secret for human display, keeping only a short suffix for recognition."""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return f"{secret[:3]}...{secret[-4:]}"


def redact_text(text: str | None, *, extra_secrets: list[str | None] | None = None) -> str:
    """Scrub any secret-looking substrings from free text.

    ``extra_secrets`` (typically the caller's own API key) are masked verbatim with
    :data:`REDACTED_KEY` *before* the generic patterns run, so an opaque/non-standard
    key format is still caught and the security detector can detect a verbatim echo.
    """
    if not text:
        return ""
    redacted = text
    for secret in extra_secrets or ():
        if secret and len(secret) >= _MIN_SECRET_LEN:
            redacted = redacted.replace(secret, REDACTED_KEY)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def redact_json(obj: Any, *, extra_secrets: list[str | None] | None = None) -> Any:
    """Recursively scrub a parsed JSON value (dict/list/str) for reports.

    String values are run through :func:`redact_text`; any dict key whose name is a
    sensitive header is masked outright. Non-string scalars are returned unchanged.
    """
    if isinstance(obj, str):
        return redact_text(obj, extra_secrets=extra_secrets)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_HEADERS:
                out[key] = REDACTED
            else:
                out[key] = redact_json(value, extra_secrets=extra_secrets)
        return out
    if isinstance(obj, list):
        return [redact_json(item, extra_secrets=extra_secrets) for item in obj]
    return obj


def redact_headers(
    headers: dict[str, str], *, extra_secrets: list[str | None] | None = None
) -> dict[str, str]:
    """Mask sensitive header values; preserve the rest (useful as evidence)."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADERS:
            out[key] = REDACTED
        else:
            out[key] = redact_text(value, extra_secrets=extra_secrets)
    return out
