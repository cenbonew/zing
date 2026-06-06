"""Anthropic Messages-API client tests.

Verifies the request translation (system extraction, stop_sequences, temperature
clamp, tool input_schema), the response normalization (text/tool_use blocks and
input/output token usage -> the common CompletionOutcome shape), SSE streaming,
and protocol auto-detection.
"""

from __future__ import annotations

import json

import httpx
import pytest

from zing.clients import AnthropicClient, OpenAICompatibleClient, detect_api, make_client
from zing.config import AuditOptions
from zing.context import AuditContext
from zing.detectors.model_identity import ModelIdentityDetector
from zing.knowledge import load_knowledge_base
from zing.models import RequestSpec, Status, TargetConfig

BASE = "https://api.anthropic.test/v1"
MODEL = "claude-opus-4-8"


class AnthropicMock:
    """A minimal Messages-API endpoint backed by httpx.MockTransport."""

    def __init__(self, *, text="I am Claude, made by Anthropic.", tool=None, stream_text=None):
        self.text = text
        self.tool = tool
        self.stream_text = stream_text
        self.requests: list[dict] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": MODEL, "type": "model"}]})
        body = json.loads(request.content.decode()) if request.content else {}
        self.requests.append(body)
        if body.get("stream"):
            return self._stream(body)
        return self._json(body)

    def _json(self, body: dict) -> httpx.Response:
        content = [{"type": "text", "text": self.text}]
        stop_reason = "end_turn"
        if self.tool is not None:
            content = [{"type": "tool_use", "id": "toolu_1", "name": self.tool["name"], "input": self.tool["input"]}]
            stop_reason = "tool_use"
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": content,
                "stop_reason": stop_reason,
                "usage": {"input_tokens": 12, "output_tokens": 7},
            },
        )

    def _stream(self, body: dict) -> httpx.Response:
        words = (self.stream_text or self.text).split()
        events = [
            ("message_start", {"type": "message_start", "message": {"model": MODEL, "usage": {"input_tokens": 9, "output_tokens": 0}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ]
        for w in words:
            events.append(("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w + " "}}))
        events += [
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": len(words)}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        chunks = "".join(f"event: {name}\ndata: {json.dumps(d)}\n\n" for name, d in events)
        return httpx.Response(200, content=chunks.encode(), headers={"content-type": "text/event-stream"})


def _target(**kw) -> TargetConfig:
    return TargetConfig(name="t", kind="target", base_url=BASE, api_key="sk-ant-secret123456", model=MODEL, api="anthropic", **kw)


async def test_anthropic_nonstream_basic():
    mock = AnthropicMock()
    async with AnthropicClient(_target(), transport=mock.transport) as c:
        out = await c.complete(RequestSpec(messages=[{"role": "user", "content": "who are you?"}], max_tokens=64))
    assert out.ok
    assert "Claude" in out.content
    assert out.model_returned == MODEL
    assert out.finish_reason == "stop"
    assert out.usage["prompt_tokens"] == 12
    assert out.usage["completion_tokens"] == 7
    assert out.usage["total_tokens"] == 19


async def test_anthropic_request_translation():
    mock = AnthropicMock()
    async with AnthropicClient(_target(), transport=mock.transport) as c:
        await c.complete(
            RequestSpec(
                messages=[{"role": "system", "content": "Be terse."}, {"role": "user", "content": "hi"}],
                temperature=2.0,
                stop="STOP",
                max_tokens=None,
                tools=[{"type": "function", "function": {"name": "get_weather", "description": "w", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
                tool_choice="auto",
            )
        )
    body = mock.requests[-1]
    assert body["system"] == "Be terse."                       # system extracted
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["max_tokens"] >= 1                              # required field synthesized
    assert body["stop_sequences"] == ["STOP"]                  # stop -> stop_sequences
    assert body["temperature"] == 1.0                          # clamped from 2.0
    assert body["tools"][0]["input_schema"]["properties"]["city"]  # parameters -> input_schema
    assert "response_format" not in body and "seed" not in body


async def test_anthropic_tool_use_mapping():
    mock = AnthropicMock(tool={"name": "get_weather", "input": {"city": "Paris"}})
    async with AnthropicClient(_target(), transport=mock.transport) as c:
        out = await c.complete(RequestSpec(messages=[{"role": "user", "content": "weather?"}], max_tokens=64))
    assert out.finish_reason == "tool_calls"
    call = out.tool_calls[0]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}


async def test_anthropic_streaming():
    mock = AnthropicMock(stream_text="Hello there friend")
    async with AnthropicClient(_target(), transport=mock.transport) as c:
        out = await c.complete(RequestSpec(messages=[{"role": "user", "content": "hi"}], max_tokens=64, stream=True))
    assert out.ok
    assert out.content.split() == ["Hello", "there", "friend"]
    assert out.chunk_count == 3
    assert out.usage["completion_tokens"] == 3
    assert out.model_returned == MODEL


async def test_anthropic_error_is_redacted():
    class ErrMock(AnthropicMock):
        def _handler(self, request):
            return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key sk-ant-secret123456"}})

    mock = ErrMock()
    async with AnthropicClient(_target(), transport=mock.transport) as c:
        out = await c.complete(RequestSpec(messages=[{"role": "user", "content": "hi"}], max_tokens=8))
    assert not out.ok and out.status_code == 401
    assert "sk-ant-secret123456" not in (out.error_message or "")
    assert "sk-ant-secret123456" not in json.dumps(out.raw_error or {})


@pytest.mark.parametrize(
    "base_url,model,api,expected",
    [
        ("https://api.anthropic.com/v1", "claude-opus-4-8", "auto", "anthropic"),
        ("https://relay.example.com/v1", "claude-3-haiku", "auto", "anthropic"),
        ("https://relay.example.com/v1", "gpt-4o", "auto", "openai"),
        ("https://relay.example.com/v1", "gpt-4o", "anthropic", "anthropic"),
        ("https://api.anthropic.com/v1", "claude-x", "openai", "openai"),
    ],
)
def test_detect_api(base_url, model, api, expected):
    cfg = TargetConfig(base_url=base_url, model=model, api=api)
    assert detect_api(cfg) == expected


def test_make_client_picks_implementation():
    assert isinstance(make_client(TargetConfig(base_url=BASE, model=MODEL, api="anthropic")), AnthropicClient)
    assert isinstance(make_client(TargetConfig(base_url="https://x/v1", model="gpt-4o")), OpenAICompatibleClient)


async def test_detector_runs_against_anthropic_backend():
    # A genuine Claude relay self-identifying as Claude should pass identity.
    mock = AnthropicMock(text="I am Claude, an AI model made by Anthropic.")
    kb = load_knowledge_base()
    target = _target()
    async with AnthropicClient(target, transport=mock.transport) as c:
        ctx = AuditContext(
            target=target, client=c, options=AuditOptions(suite="standard"),
            kb=kb, profile=kb.resolve(MODEL),
        )
        result = await ModelIdentityDetector().run(ctx)
    assert result.status in (Status.PASS, Status.WARN)  # genuine — never FAIL
    self_id = [f for f in result.findings if f.id == "model_identity.self_id"]
    assert self_id and self_id[0].status == Status.PASS
