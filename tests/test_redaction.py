"""Secret-redaction tests — the highest-impact security guarantee.

A misbehaving relay can echo the caller's bearer token (or a sibling secret) in its
completion content or error body. These tests assert the raw secret never survives
to a report, while the security detector can still tell the key was echoed.
"""

from __future__ import annotations

import json

from zing.models import RequestSpec
from zing.utils.redact import (
    REDACTED,
    REDACTED_KEY,
    redact_headers,
    redact_json,
    redact_text,
)


def test_redact_text_scrubs_configured_opaque_key():
    # A non-standard/opaque key (not sk-/AIza/Bearer) is still scrubbed via extra_secrets.
    key = "opaque-Zx12-not-a-standard-format-7777"
    out = redact_text(f"sure, your key {key} is noted", extra_secrets=[key])
    assert key not in out
    assert REDACTED_KEY in out


def test_redact_text_scrubs_pattern_keys():
    out = redact_text("leaked sk-abcdEFGH12345678 and AIzaSyABCDEFGHIJKLMNOPQRSTUV01234567")
    assert "sk-abcdEFGH12345678" not in out
    assert "AIzaSyABCDEFGHIJKLMNOPQRSTUV01234567" not in out
    assert REDACTED in out


def test_redact_json_recurses_and_masks_sensitive_headers():
    obj = {
        "error": {
            "message": "bad request with sk-abcdEFGH12345678",
            "headers": {"authorization": "Bearer sk-secretverylong12345"},
            "nested": ["sk-anotherKey1234567", {"x-api-key": "sk-deepSecret99887766"}],
        }
    }
    serialized = json.dumps(redact_json(obj))
    for secret in (
        "sk-abcdEFGH12345678",
        "sk-secretverylong12345",
        "sk-anotherKey1234567",
        "sk-deepSecret99887766",
    ):
        assert secret not in serialized


def test_redact_headers_masks_authorization():
    out = redact_headers({"authorization": "Bearer sk-xyz12345678", "server": "nginx"})
    assert out["authorization"] == REDACTED
    assert out["server"] == "nginx"


async def test_client_redacts_echoed_key_in_content(client, mock_server):
    key = "sk-test-secret-key-do-not-leak"  # matches the conftest target api_key
    mock_server.reply_text = f"Of course, your key {key} is recorded."
    outcome = await client.complete(
        RequestSpec(messages=[{"role": "user", "content": "hi"}], max_tokens=16)
    )
    assert outcome.ok
    assert key not in (outcome.content or "")
    assert REDACTED_KEY in outcome.content


async def test_client_redacts_key_in_error_body(client, mock_server):
    key = "sk-test-secret-key-do-not-leak"
    mock_server.chat_status = 400
    mock_server.error_body = {"error": {"message": f"invalid auth for {key}", "type": "x"}}
    outcome = await client.complete(
        RequestSpec(messages=[{"role": "user", "content": "hi"}], max_tokens=16)
    )
    assert not outcome.ok
    assert key not in (outcome.error_message or "")
    assert key not in json.dumps(outcome.raw_error or {})
