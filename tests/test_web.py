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


def test_serves_console_and_history_and_i18n(client):
    for path in ("/console", "/history"):
        r = client.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    j = client.get("/i18n.js")
    assert j.status_code == 200 and "ZING_I18N" in j.text


def test_serves_icons_and_modelpicker_js(client):
    icons = client.get("/icons.js")
    assert icons.status_code == 200
    assert "application/javascript" in icons.headers["content-type"]
    assert "ZING_ICONS" in icons.text and "zingIcon" in icons.text

    mp = client.get("/modelpicker.js")
    assert mp.status_code == 200
    assert "application/javascript" in mp.headers["content-type"]
    assert "ZingModelPicker" in mp.text


def test_api_kb_lists_providers_with_models(client):
    r = client.get("/api/kb")
    assert r.status_code == 200
    body = r.json()
    providers = body["providers"]
    assert isinstance(providers, list) and providers
    # Every provider entry exposes only public metadata (no api keys).
    for p in providers:
        assert {"provider", "display_name", "models"} <= set(p)
        for m in p["models"]:
            assert set(m) == {"id", "aliases"}
    # At least one provider has models; deepseek ships a deepseek-* id.
    assert any(p["models"] for p in providers)
    deepseek = next((p for p in providers if p["provider"] == "deepseek"), None)
    assert deepseek is not None
    assert any(m["id"].startswith("deepseek") for m in deepseek["models"])


def test_history_module_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ZING_DATA_DIR", str(tmp_path))
    from zing.web import history

    assert history.recent() == []
    sample = {
        "generated_at": "2026-06-07T00:00:00Z",
        "target": {"base_url": "https://relay.test/v1", "claimed_model": "deepseek-v4-pro", "model": "doubao-x"},
        "mode": "compare",
        "suite": "standard",
        "verdict": {"risk_level": "high", "overall_score": 21.0, "rating": "F"},
    }
    rid = history.save(sample)
    assert isinstance(rid, int) and rid > 0
    rows = history.recent()
    assert len(rows) == 1 and rows[0]["risk_level"] == "high" and rows[0]["score"] == 21.0
    full = history.get(rid)
    assert full["verdict"]["rating"] == "F" and full["target"]["model"] == "doubao-x"
    trend = history.trend("https://relay.test/v1", "deepseek-v4-pro")
    assert len(trend) == 1 and trend[0]["score"] == 21.0
    history.delete(rid)
    assert history.recent() == []


def test_history_endpoints(tmp_path, monkeypatch, client):
    monkeypatch.setenv("ZING_DATA_DIR", str(tmp_path))
    assert client.get("/api/history").json() == []
    from zing.web import history

    rid = history.save({
        "generated_at": "t", "target": {"base_url": "https://x/v1", "claimed_model": "m", "model": "m"},
        "mode": "check", "suite": "smoke", "verdict": {"risk_level": "clean", "overall_score": 95.0, "rating": "A"},
    })
    listed = client.get("/api/history").json()
    assert len(listed) == 1 and listed[0]["id"] == rid
    assert client.get(f"/api/history/{rid}").json()["verdict"]["overall_score"] == 95.0
    assert client.get("/api/history/999999").status_code == 404


def test_audit_stream_invalid_baseline_errors(client):
    # A compare request with a malformed baseline base_url is a clean error event.
    r = client.post(
        "/api/audit/stream",
        json={
            "base_url": "https://x.example/v1",
            "model": "gpt-4o",
            "baseline": {"base_url": "ftp://nope", "model": "gpt-4o"},
        },
    )
    assert '"type": "error"' in r.text
