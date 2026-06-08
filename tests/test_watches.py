"""Tests for the scheduled-watch store and its web endpoints.

Skipped automatically when the optional web extra (fastapi) isn't installed.
These never touch the network or trigger the real scheduler loop: they cover the
SQLite store roundtrip and the /api/watches CRUD endpoints, asserting that the
api_key never leaks into a listing.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from zing.web.server import create_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


def test_watch_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ZING_DATA_DIR", str(tmp_path))
    from zing.web import watches

    assert watches.list_all() == []

    wid = watches.create(
        {
            "name": "prod relay",
            "base_url": "https://relay.test/v1",
            "api_key": "sk-super-secret",
            "model": "gpt-4o",
            "claimed_model": "gpt-4o",
            "api": "openai",
            "suite": "standard",
            "interval_sec": 3600,
            "alert_on": "medium",
            "webhooks": ["https://hooks.slack.com/services/x"],
        }
    )
    assert isinstance(wid, int) and wid > 0

    # Listing must never expose the api_key, but should decode webhooks/enabled.
    rows = watches.list_all()
    assert len(rows) == 1
    row = rows[0]
    assert "api_key" not in row
    assert row["name"] == "prod relay"
    assert row["webhooks"] == ["https://hooks.slack.com/services/x"]
    assert row["enabled"] is True

    # get() is the server-side view and DOES carry the key for the scheduler.
    full = watches.get(wid)
    assert full is not None and full["api_key"] == "sk-super-secret"

    # A brand-new watch (never run) is due immediately.
    import time

    due = watches.due(time.time())
    assert len(due) == 1 and due[0]["id"] == wid

    # Record a run; risk/score/report id + run time persist.
    watches.mark_run(wid, "high", 21.0, 7, ts=time.time())
    after = watches.get(wid)
    assert after["last_risk"] == "high" and after["last_score"] == 21.0
    assert after["last_report_id"] == 7

    # With a fresh last_run and a long interval, it is no longer due.
    assert watches.due(time.time()) == []

    # Disable, then it is excluded from due() regardless of timing.
    watches.set_enabled(wid, False)
    assert watches.list_all()[0]["enabled"] is False
    assert watches.due(time.time() + 10_000) == []

    watches.delete(wid)
    assert watches.list_all() == []


def test_watches_endpoints_create_list_delete(tmp_path, monkeypatch, client):
    monkeypatch.setenv("ZING_DATA_DIR", str(tmp_path))

    assert client.get("/api/watches").json() == []

    r = client.post(
        "/api/watches",
        json={
            "name": "my watch",
            "base_url": "https://relay.test/v1",
            "api_key": "sk-do-not-leak",
            "model": "gpt-4o",
            "suite": "standard",
            "alert_on": "medium",
            "interval_sec": 7200,
            "webhooks": ["https://example.com/hook"],
        },
    )
    assert r.status_code == 201
    wid = r.json()["id"]
    assert isinstance(wid, int) and wid > 0

    listed = client.get("/api/watches").json()
    assert len(listed) == 1
    one = listed[0]
    assert one["id"] == wid and one["name"] == "my watch"
    # The endpoint listing must NOT include the api_key anywhere.
    assert "api_key" not in one
    assert "sk-do-not-leak" not in r.text
    assert "sk-do-not-leak" not in client.get("/api/watches").text

    # PATCH toggles enabled.
    assert client.patch(f"/api/watches/{wid}", json={"enabled": False}).status_code == 200
    assert client.get("/api/watches").json()[0]["enabled"] is False

    # DELETE removes it.
    assert client.delete(f"/api/watches/{wid}").status_code == 200
    assert client.get("/api/watches").json() == []


def test_watches_create_bad_target_is_clean_400(tmp_path, monkeypatch, client):
    monkeypatch.setenv("ZING_DATA_DIR", str(tmp_path))
    r = client.post("/api/watches", json={"base_url": "", "model": ""})
    assert r.status_code == 400 and "error" in r.json()
    # Nothing was persisted.
    assert client.get("/api/watches").json() == []


def test_watches_create_unknown_suite_is_400(tmp_path, monkeypatch, client):
    monkeypatch.setenv("ZING_DATA_DIR", str(tmp_path))
    r = client.post(
        "/api/watches",
        json={"base_url": "https://x/v1", "model": "gpt-4o", "suite": "bogus"},
    )
    assert r.status_code == 400


def test_watches_page_and_nav_link(client):
    r = client.get("/watches")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "监控" in r.text
    # The SPA exposes a nav link to /watches.
    assert 'href="/watches"' in client.get("/").text


def test_patch_and_run_missing_watch_404(tmp_path, monkeypatch, client):
    monkeypatch.setenv("ZING_DATA_DIR", str(tmp_path))
    assert client.patch("/api/watches/999999", json={"enabled": True}).status_code == 404
    assert client.post("/api/watches/999999/run").status_code == 404
