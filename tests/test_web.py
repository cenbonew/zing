"""Tests for the local web server (`zing serve`).

Skipped automatically when the optional web extra (fastapi) isn't installed.
These don't hit the network: they cover health, the served SPA, and the
validation/error path of the SSE endpoint.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from zing.web.server import create_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["name"] == "zing"


def test_serves_spa(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "zing" in r.text and "中转站" in r.text


def test_spa_fallback_for_deep_link(client):
    r = client.get("/some/deep/link")
    assert r.status_code == 200
    assert "<!doctype html>" in r.text.lower()


def test_audit_stream_bad_input_is_clean_error(client):
    # No base_url/model → a clean SSE error event, not a 500.
    r = client.post("/api/audit/stream", json={"model": "", "base_url": ""})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert '"type": "error"' in r.text
    assert '"type": "done"' in r.text


def test_audit_stream_unknown_suite_errors(client):
    r = client.post(
        "/api/audit/stream",
        json={"base_url": "https://x.example/v1", "model": "gpt-4o", "suite": "bogus"},
    )
    assert '"type": "error"' in r.text
