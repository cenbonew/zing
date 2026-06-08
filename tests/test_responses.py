"""OpenAI Responses-API client tests.

Verifies the request translation (system -> instructions, max_tokens ->
max_output_tokens, messages -> input items, tool flattening, dropped
seed/response_format), the response normalization (output_text + function_call
items and input/output token usage -> the common CompletionOutcome shape), SSE
streaming, and protocol auto-detection.
"""

from __future__ import annotations

import json

import httpx
import pytest

from zing.clients import (
    OpenAICompatibleClient,
    ResponsesClient,
    detect_api,
    make_client,
)
from zing.models import RequestSpec, TargetConfig

BASE = "https://api.openai.test/v1"
MODEL = "gpt-5"


class ResponsesMock:
    """A minimal Responses-API endpoint backed by httpx.MockTransport."""

    def __init__(self, *, text="I am GPT, made by OpenAI.", tool=None, stream_text=None):
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
            return httpx.Response(200, json={"data": [{"id": MODEL, "object": "model"}]})
        body = json.loads(request.content.decode()) if request.content else {}
        self.requests.append(body)
        if body.get("stream"):
            return self._stream(body)
        return self._json(body)

    def _json(self, body: dict) -> httpx.Response:
        output: list[dict] = []
        status = "completed"
        if self.tool is not None:
            output.append(
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": self.tool["name"],
                    "arguments": json.dumps(self.tool["input"]),
                }
            )
        else:
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self.text}],
                }
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "model": MODEL,
                "status": status,
                "output": output,
                "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
            },
        )

    def _stream(self, body: dict) -> httpx.Response:
        words = (self.stream_text or self.text).split()
        events: list[dict] = [
            {
                "type": "response.created",
                "response": {"id": "resp_1", "model": MODEL, "status": "in_progress"},
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "message", "role": "assistant"},
            },
        ]
        for w in words:
            events.append(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "delta": w + " ",
                }
            )
        events.append(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "model": MODEL,
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 9,
                        "output_tokens": len(words),
                        "total_tokens": 9 + len(words),
                    },
                },
            }
        )
        chunks = "".join(
            f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
        )
        return httpx.Response(
            200, content=chunks.encode(), headers={"content-type": "text/event-stream"}
        )


def _target(**kw) -> TargetConfig:
    return TargetConfig(
        name="t",
        kind="target",
        base_url=BASE,
        api_key="sk-resp-secret123456",
        model=MODEL,
        api="responses",
        **kw,
    )


async def test_responses_nonstream_basic():
    mock = ResponsesMock()
    async with ResponsesClient(_target(), transport=mock.transport) as c:
        out = await c.complete(
            RequestSpec(messages=[{"role": "user", "content": "who are you?"}], max_tokens=64)
        )
    assert out.ok
    assert "GPT" in out.content
    assert out.model_returned == MODEL
    assert out.finish_reason == "stop"
    assert out.usage["prompt_tokens"] == 12
    assert out.usage["completion_tokens"] == 7
    assert out.usage["total_tokens"] == 19
    assert out.usage["input_tokens"] == 12
    assert out.usage["output_tokens"] == 7


async def test_responses_request_translation():
    mock = ResponsesMock()
    async with ResponsesClient(_target(), transport=mock.transport) as c:
        await c.complete(
            RequestSpec(
                messages=[
                    {"role": "system", "content": "Be terse."},
                    {"role": "user", "content": "hi"},
                ],
                temperature=0.5,
                top_p=0.9,
                seed=7,
                response_format={"type": "json_object"},
                max_tokens=None,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "w",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                            },
                        },
                    }
                ],
                tool_choice="auto",
            )
        )
    body = mock.requests[-1]
    assert body["instructions"] == "Be terse."                  # system -> instructions
    assert all(item["role"] != "system" for item in body["input"])
    assert body["input"] == [{"role": "user", "content": "hi"}]
    assert body["max_output_tokens"] >= 1                       # default synthesized
    assert body["temperature"] == 0.5 and body["top_p"] == 0.9
    # tools flattened to Responses shape (type/name/parameters at top level)
    tool = body["tools"][0]
    assert tool["type"] == "function" and tool["name"] == "get_weather"
    assert tool["parameters"]["properties"]["city"]
    assert body["tool_choice"] == "auto"
    # unsupported knobs dropped
    assert "response_format" not in body and "seed" not in body and "stop" not in body


async def test_responses_multimodal_input_passthrough():
    mock = ResponsesMock()
    parts = [
        {"type": "input_text", "text": "what is this?"},
        {"type": "input_image", "image_url": "https://x/y.png"},
    ]
    async with ResponsesClient(_target(), transport=mock.transport) as c:
        await c.complete(
            RequestSpec(messages=[{"role": "user", "content": parts}], max_tokens=32)
        )
    body = mock.requests[-1]
    # list-of-parts content passes through unchanged for multimodal
    assert body["input"][0]["content"] == parts


async def test_responses_tool_call_mapping():
    mock = ResponsesMock(tool={"name": "get_weather", "input": {"city": "Paris"}})
    async with ResponsesClient(_target(), transport=mock.transport) as c:
        out = await c.complete(
            RequestSpec(messages=[{"role": "user", "content": "weather?"}], max_tokens=64)
        )
    assert out.ok
    call = out.tool_calls[0]
    assert call["id"] == "call_1"
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}


async def test_responses_streaming():
    mock = ResponsesMock(stream_text="Hello there friend")
    async with ResponsesClient(_target(), transport=mock.transport) as c:
        out = await c.complete(
            RequestSpec(
                messages=[{"role": "user", "content": "hi"}], max_tokens=64, stream=True
            )
        )
    assert out.ok
    assert out.content.split() == ["Hello", "there", "friend"]
    assert out.chunk_count == 3
    assert out.usage["completion_tokens"] == 3
    assert out.usage["total_tokens"] == 12
    assert out.model_returned == MODEL
    assert out.finish_reason == "stop"


async def test_responses_error_is_redacted():
    class ErrMock(ResponsesMock):
        def _handler(self, request):
            return httpx.Response(
                401,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "bad key sk-resp-secret123456",
                    }
                },
            )

    mock = ErrMock()
    async with ResponsesClient(_target(), transport=mock.transport) as c:
        out = await c.complete(
            RequestSpec(messages=[{"role": "user", "content": "hi"}], max_tokens=8)
        )
    assert not out.ok and out.status_code == 401
    assert "sk-resp-secret123456" not in (out.error_message or "")
    assert "sk-resp-secret123456" not in json.dumps(out.raw_error or {})


async def test_responses_list_models():
    mock = ResponsesMock()
    async with ResponsesClient(_target(), transport=mock.transport) as c:
        outcome, ids = await c.list_models()
    assert outcome.ok
    assert MODEL in ids


@pytest.mark.parametrize(
    "base_url,model,api,expected",
    [
        ("https://api.openai.com/v1/responses", "gpt-5", "auto", "responses"),
        ("https://relay.example.com/v1/responses", "gpt-4o", "auto", "responses"),
        ("https://relay.example.com/v1", "gpt-4o", "responses", "responses"),
        ("https://relay.example.com/v1", "gpt-4o", "auto", "openai"),
        ("https://api.anthropic.com/v1", "claude-x", "responses", "responses"),
    ],
)
def test_detect_api_responses(base_url, model, api, expected):
    cfg = TargetConfig(base_url=base_url, model=model, api=api)
    assert detect_api(cfg) == expected


def test_make_client_picks_responses():
    assert isinstance(
        make_client(TargetConfig(base_url=BASE, model=MODEL, api="responses")),
        ResponsesClient,
    )
    assert isinstance(
        make_client(TargetConfig(base_url="https://x/v1", model="gpt-4o")),
        OpenAICompatibleClient,
    )
