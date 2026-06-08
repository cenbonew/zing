"""Unit tests for zing.notify — webhook alert formatters and delivery.

No network: :func:`send` is exercised against an ``httpx.MockTransport`` injected
into a patched ``httpx.AsyncClient`` so we can assert the request shape and the
boolean result without touching a real webhook.
"""

from __future__ import annotations

import json

import httpx
import pytest

from zing import notify

# A representative high-risk report dict (AuditReport.model_dump() shape).
REPORT = {
    "verdict": {
        "overall_score": 42.0,
        "rating": "D",
        "risk_level": "high",
        "headline": "Likely downgrade",
        "key_findings": ["发现一", "发现二", "发现三"],
    },
    "target": {
        "base_url": "https://relay.example.com/v1",
        "claimed_model": "gpt-4o",
        "model": "gpt-4o-mini",
    },
}
PREV_LOW = {"verdict": {"risk_level": "low"}}
PREV_HIGH = {"verdict": {"risk_level": "high"}}


# --------------------------------------------------------------------------- #
# Text digest
# --------------------------------------------------------------------------- #
def test_build_text_contains_core_fields():
    text = notify.build_text(REPORT)
    assert "https://relay.example.com/v1" in text   # base_url
    assert "gpt-4o" in text                          # claimed model
    assert "42.0/100" in text                        # score
    assert "D" in text                               # rating
    assert "发现一" in text                          # a key finding
    assert "Likely downgrade" in text                # headline


def test_build_text_caps_key_findings():
    rep = {
        "verdict": {"risk_level": "high", "key_findings": [f"f{i}" for i in range(20)]},
        "target": {"base_url": "https://x/v1", "model": "m"},
    }
    text = notify.build_text(rep)
    assert "f0" in text and "f4" in text
    assert "f5" not in text  # only the first 5 are shown


def test_build_text_delta_when_previous_given():
    worse = notify.build_text(REPORT, PREV_LOW)
    assert "较上次" in worse and "恶化" in worse
    none = notify.build_text(REPORT, None)
    assert "较上次" not in none


# --------------------------------------------------------------------------- #
# Per-platform payload shapes
# --------------------------------------------------------------------------- #
def test_format_slack_shape():
    payload = notify.format_slack(REPORT)
    assert set(payload) == {"text"}
    assert isinstance(payload["text"], str) and payload["text"]


def test_format_feishu_shape():
    payload = notify.format_feishu(REPORT)
    assert payload["msg_type"] == "text"
    assert isinstance(payload["content"]["text"], str)
    assert "relay.example.com" in payload["content"]["text"]


def test_format_dingtalk_shape():
    payload = notify.format_dingtalk(REPORT)
    assert payload["msgtype"] == "text"
    assert isinstance(payload["text"]["content"], str)
    assert "relay.example.com" in payload["text"]["content"]


def test_format_generic_shape():
    payload = notify.format_generic(REPORT, PREV_LOW)
    assert payload["tool"] == "zing"
    assert payload["event"] == "audit_alert"
    assert payload["risk_level"] == "high"
    assert payload["score"] == 42.0
    assert payload["rating"] == "D"
    assert payload["base_url"] == "https://relay.example.com/v1"
    assert payload["claimed_model"] == "gpt-4o"
    assert payload["key_findings"] == ["发现一", "发现二", "发现三"]
    assert payload["previous_risk_level"] == "low"
    assert payload["regressed"] is True
    assert isinstance(payload["text"], str)


def test_format_generic_no_previous():
    payload = notify.format_generic(REPORT)
    assert payload["previous_risk_level"] is None
    assert payload["regressed"] is False


# --------------------------------------------------------------------------- #
# URL auto-detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://hooks.slack.com/services/T/B/X", "slack"),
        ("https://open.feishu.cn/open-apis/bot/v2/hook/abc", "feishu"),
        ("https://open.larksuite.com/open-apis/bot/v2/hook/abc", "feishu"),
        ("https://oapi.dingtalk.com/robot/send?access_token=x", "dingtalk"),
        ("https://example.com/my/webhook", "generic"),
        ("not a url", "generic"),
    ],
)
def test_detect_kind(url, expected):
    assert notify.detect_kind(url) == expected


def test_build_payload_auto_uses_url_host():
    slack = notify.build_payload(REPORT, kind="auto", webhook_url="https://hooks.slack.com/x")
    assert set(slack) == {"text"}
    feishu = notify.build_payload(REPORT, kind="auto", webhook_url="https://open.feishu.cn/x")
    assert feishu["msg_type"] == "text"


def test_build_payload_explicit_kind_overrides_url():
    # Explicit kind wins even when the host would auto-detect to something else.
    payload = notify.build_payload(REPORT, kind="dingtalk", webhook_url="https://hooks.slack.com/x")
    assert payload["msgtype"] == "text"


# --------------------------------------------------------------------------- #
# regressed() ordering
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "current,previous,expected",
    [
        ("high", "low", True),
        ("medium", "low", True),
        ("low", "clean", True),
        ("high", "high", False),     # equal is not a regression
        ("low", "high", False),      # improvement
        ("clean", "low", False),
        ("high", None, False),       # no previous
    ],
)
def test_regressed_ordering(current, previous, expected):
    cur = {"verdict": {"risk_level": current}}
    prev = None if previous is None else {"verdict": {"risk_level": previous}}
    assert notify.regressed(cur, prev) is expected


# --------------------------------------------------------------------------- #
# send() against a MockTransport
# --------------------------------------------------------------------------- #
class _Recorder:
    """Captures the single POST send() makes, so we can assert on it."""

    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"ok": True})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _patch_async_client(monkeypatch, transport: httpx.MockTransport) -> None:
    """Force httpx.AsyncClient(...) to use our MockTransport, ignoring kwargs."""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(transport=transport)

    monkeypatch.setattr(notify.httpx, "AsyncClient", factory)


async def test_send_posts_and_returns_true(monkeypatch):
    rec = _Recorder(status=200)
    _patch_async_client(monkeypatch, rec.transport)

    ok = await notify.send(
        "https://hooks.slack.com/services/T/B/X", REPORT, previous=PREV_LOW
    )
    assert ok is True
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "POST"
    body = json.loads(req.content.decode())
    # Auto-detected slack shape, with the regression delta in the text.
    assert set(body) == {"text"}
    assert "较上次" in body["text"]


async def test_send_explicit_kind_feishu(monkeypatch):
    rec = _Recorder(status=200)
    _patch_async_client(monkeypatch, rec.transport)

    ok = await notify.send("https://example.com/hook", REPORT, kind="feishu")
    assert ok is True
    body = json.loads(rec.requests[0].content.decode())
    assert body["msg_type"] == "text"


async def test_send_non_2xx_returns_false(monkeypatch):
    rec = _Recorder(status=500)
    _patch_async_client(monkeypatch, rec.transport)

    ok = await notify.send("https://example.com/hook", REPORT)
    assert ok is False
    assert len(rec.requests) == 1  # it still attempted the POST


async def test_send_network_error_returns_false(monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    _patch_async_client(monkeypatch, httpx.MockTransport(boom))
    ok = await notify.send("https://example.com/hook", REPORT)
    assert ok is False  # never raises


async def test_send_empty_url_returns_false():
    assert await notify.send("", REPORT) is False
