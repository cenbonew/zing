"""Multimodal (vision) detector tests.

Verifies the known-answer image probe with a self-contained MockTransport relay:
(a) a relay that "sees" and returns the color -> PASS,
(b) a blind relay that says it cannot see images -> WARN (MEDIUM),
(c) a profile that does not claim vision -> skipped (INFO, never a failure).

Also confirms the probe actually ships a valid base64 PNG image part in the
protocol-correct shape (the whole point — a text-only substitute must be given a
real image to be caught lying about it).
"""

from __future__ import annotations

import base64
import json

import httpx

from zing.clients import OpenAICompatibleClient
from zing.config import AuditOptions
from zing.context import AuditContext
from zing.detectors.vision import VisionDetector
from zing.knowledge import load_knowledge_base
from zing.knowledge.schema import ModelProfile, ProviderProfile, ResolvedProfile
from zing.models import Severity, Status, TargetConfig

BASE = "https://relay.example.test/v1"
MODEL = "gpt-vision-test"


class VisionMock:
    """A minimal OpenAI Chat Completions endpoint that replies with fixed text.

    Records each request body so a test can assert the image part was sent.
    """

    def __init__(self, *, text: str):
        self.text = text
        self.requests: list[dict] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        self.requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
            },
        )


def _target(**kw) -> TargetConfig:
    return TargetConfig(
        name="t", kind="target", base_url=BASE, api_key="sk-secret-key-123456",
        model=MODEL, api="openai", **kw,
    )


def _profile(modalities: list[str]) -> ResolvedProfile:
    """A hand-built profile so the test does not depend on KB data drift."""
    provider = ProviderProfile(provider="testco", display_name="Test Co")
    model = ModelProfile(id=MODEL, modalities=modalities)
    return ResolvedProfile(provider=provider, model=model, match_confidence="exact")


async def _run(text: str, modalities: list[str]):
    mock = VisionMock(text=text)
    target = _target()
    kb = load_knowledge_base()
    async with OpenAICompatibleClient(target, transport=mock.transport) as client:
        ctx = AuditContext(
            target=target, client=client, options=AuditOptions(suite="deep"),
            kb=kb, profile=_profile(modalities),
        )
        result = await VisionDetector().run(ctx)
    return result, mock


async def test_vision_seen_passes():
    """A relay that returns the known color -> PASS, and it was sent a real image."""
    result, mock = await _run("橙色 / Orange", ["text", "vision"])
    assert result.status == Status.PASS
    assert result.score == 100.0
    color = [f for f in result.findings if f.id == "vision.color"]
    assert color and color[0].status == Status.PASS

    # The probe must have shipped a valid base64 PNG image part (openai shape).
    content = mock.requests[-1]["messages"][0]["content"]
    image_parts = [p for p in content if p.get("type") == "image_url"]
    assert image_parts, "expected an image_url part in the user message"
    uri = image_parts[0]["image_url"]["url"]
    assert uri.startswith("data:image/png;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"  # genuine PNG signature
    text_parts = [p for p in content if p.get("type") == "text"]
    assert text_parts and "color" in text_parts[0]["text"].lower()


async def test_vision_blind_warns():
    """A blind relay claiming vision but saying it can't see -> WARN (MEDIUM)."""
    result, _ = await _run(
        "I'm sorry, I can't see images. As a text-based model I have no vision.",
        ["text", "vision"],
    )
    assert result.status == Status.WARN
    assert result.score == 0.0
    color = [f for f in result.findings if f.id == "vision.color"]
    assert color and color[0].status == Status.WARN
    assert color[0].severity == Severity.MEDIUM
    assert color[0].evidence.get("admitted_blind") is True


async def test_vision_wrong_color_warns():
    """A wrong-color guess (not a refusal) still means vision not delivered -> WARN."""
    result, _ = await _run("Blue", ["text", "vision"])
    assert result.status == Status.WARN
    color = [f for f in result.findings if f.id == "vision.color"]
    assert color and color[0].severity == Severity.MEDIUM
    assert color[0].evidence.get("admitted_blind") is False


async def test_vision_not_claimed_skipped():
    """A profile that does not claim vision -> INFO skip, never a failure, no call."""
    result, mock = await _run("orange", ["text"])
    assert result.status == Status.INFO
    assert result.score is None
    assert not mock.requests, "no probe should be sent when vision is not claimed"
    skipped = [f for f in result.findings if f.id == "vision.not_claimed"]
    assert skipped and skipped[0].status == Status.INFO
