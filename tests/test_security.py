"""Security detector tests — transport and key-echo signals."""

from __future__ import annotations

from zing.detectors.security import SecurityDetector
from zing.models import Severity, Status


async def test_security_flags_non_https(audit_context):
    # The conftest base_url is http:// — the bearer token would travel in clear text.
    result = await SecurityDetector().run(audit_context)
    tls = [f for f in result.findings if f.id == "security.tls"]
    assert tls and tls[0].severity == Severity.HIGH
    assert result.status == Status.FAIL


async def test_security_detects_key_echo(audit_context, mock_server):
    # Relay echoes the caller's API key verbatim; redaction hides the raw key but the
    # detector still recognises the echo via the sentinel and flags it.
    key = "sk-test-secret-key-do-not-leak"
    mock_server.reply_text = f"your key {key} is recorded"
    result = await SecurityDetector().run(audit_context)
    echo = [f for f in result.findings if f.id == "security.key_echo"]
    assert echo and echo[0].status == Status.FAIL
    # The raw key must never appear in any serialized finding evidence.
    assert key not in result.model_dump_json()
