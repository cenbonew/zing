"""Tests for OpenAICompatibleClient against an injected httpx.MockTransport.

These cover the raw-evidence surface the detectors depend on: content, usage,
returned model id, finish reason, streaming timing/chunk accounting, error
shaping, and /v1/models parsing.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import BASE_URL

from zing.clients import OpenAICompatibleClient
from zing.models import RequestSpec, TargetConfig


def _spec(stream: bool = False, **kw) -> RequestSpec:
    return RequestSpec(messages=[{"role": "user", "content": "hello"}], stream=stream, **kw)


# --------------------------------------------------------------------------- #
# base_url normalization
# --------------------------------------------------------------------------- #
def test_base_url_requires_scheme():
    with pytest.raises(ValueError):
        OpenAICompatibleClient(TargetConfig(base_url="relay.test/v1", model="m"))


def test_endpoint_urls():
    c = OpenAICompatibleClient(TargetConfig(base_url=BASE_URL + "/", model="m"))
    assert c.chat_completions_url == f"{BASE_URL}/chat/completions"
    assert c.models_url == f"{BASE_URL}/models"


# --------------------------------------------------------------------------- #
# non-stream
# --------------------------------------------------------------------------- #
async def test_complete_nonstream_content_usage_model_finish(client, mock_server):
    mock_server.reply_text = "Hello back"
    mock_server.served_model = "gpt-4o"
    out = await client.complete(_spec())
    assert out.ok is True
    assert out.status_code == 200
    assert out.content == "Hello back"
    assert out.model_returned == "gpt-4o"
    assert out.finish_reason == "stop"
    assert out.usage is not None
    assert out.usage["total_tokens"] == out.usage["prompt_tokens"] + out.usage["completion_tokens"]
    # non-stream client reports ttft == duration
    assert out.duration_ms is not None
    assert out.ttft_ms == out.duration_ms


async def test_complete_nonstream_missing_usage(client, mock_server):
    mock_server.emit_usage = False
    out = await client.complete(_spec())
    assert out.ok is True
    assert out.usage is None


async def test_complete_nonstream_tool_call(client, mock_server):
    mock_server.tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
    }
    out = await client.complete(_spec(tools=[{"type": "function", "function": {"name": "get_weather"}}]))
    assert out.ok is True
    assert out.tool_calls and out.tool_calls[0]["function"]["name"] == "get_weather"
    assert out.finish_reason == "tool_calls"


async def test_complete_nonstream_inflated_usage(client, mock_server):
    mock_server.reply_text = "one two three"
    mock_server.inflate_usage_factor = 10.0
    out = await client.complete(_spec())
    assert out.usage is not None
    # Inflated completion count is far larger than the ~3 words returned.
    assert out.usage["completion_tokens"] >= 30


# --------------------------------------------------------------------------- #
# streaming
# --------------------------------------------------------------------------- #
async def test_complete_stream_assembles_content_and_timing(client, mock_server):
    mock_server.reply_text = "the quick brown fox jumps over the lazy dog"
    mock_server.stream_chunk_words = 2
    out = await client.complete(_spec(stream=True))
    assert out.ok is True
    assert out.content == "the quick brown fox jumps over the lazy dog"
    assert out.chunk_count >= 2
    assert len(out.chunk_timings_ms) == out.chunk_count
    assert out.ttft_ms is not None
    assert out.finish_reason == "stop"
    # usage chunk is parsed out of the stream
    assert out.usage is not None and out.usage["total_tokens"] > 0
    assert out.model_returned == mock_server.served_model


async def test_complete_stream_fake_single_chunk(client, mock_server):
    mock_server.reply_text = "a b c d e f g h"
    mock_server.fake_stream = True
    out = await client.complete(_spec(stream=True))
    assert out.ok is True
    assert out.content == "a b c d e f g h"
    # The fake stream emits exactly one content delta -> a single timing sample.
    assert out.chunk_count == 1


async def test_complete_stream_missing_usage(client, mock_server):
    mock_server.usage_in_stream = False
    out = await client.complete(_spec(stream=True))
    assert out.ok is True
    assert out.usage is None


async def test_stream_options_added_to_body(client, mock_server):
    await client.complete(_spec(stream=True))
    body = mock_server.requests[-1]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


# --------------------------------------------------------------------------- #
# error shaping
# --------------------------------------------------------------------------- #
async def test_complete_4xx_shapes_error(client, mock_server):
    mock_server.chat_status = 429
    mock_server.error_body = {"error": {"message": "rate limited", "type": "rate_limit_error"}}
    out = await client.complete(_spec())
    assert out.ok is False
    assert out.status_code == 429
    assert out.error_type == "http_error"
    assert "rate limited" in (out.error_message or "")
    assert out.raw_error == mock_server.error_body


async def test_complete_stream_4xx_shapes_error(client, mock_server):
    mock_server.chat_status = 500
    out = await client.complete(_spec(stream=True))
    assert out.ok is False
    assert out.status_code == 500
    assert out.error_type == "http_error"


async def test_complete_transport_exception_is_caught():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    cfg = TargetConfig(base_url=BASE_URL, model="m")
    async with OpenAICompatibleClient(cfg, transport=httpx.MockTransport(boom)) as c:
        out = await c.complete(_spec())
    assert out.ok is False
    assert out.error_type == "ConnectError"
    assert out.status_code is None


# --------------------------------------------------------------------------- #
# list_models
# --------------------------------------------------------------------------- #
async def test_list_models_parses_ids(client, mock_server):
    mock_server.model_ids = ["gpt-4o", "gpt-4o-mini"]
    outcome, ids = await client.list_models()
    assert outcome.ok is True
    assert outcome.status_code == 200
    assert ids == ["gpt-4o", "gpt-4o-mini"]


async def test_list_models_4xx(client, mock_server):
    mock_server.models_status = 403
    outcome, ids = await client.list_models()
    assert outcome.ok is False
    assert outcome.status_code == 403
    assert ids == []


async def test_secret_never_leaks_into_evidence(client, mock_server):
    """Authorization is set from the api_key but redacted everywhere observable."""
    mock_server.chat_status = 401
    mock_server.error_body = {"error": {"message": "bad key"}}
    out = await client.complete(_spec())
    # The configured secret must not appear in any redacted error/header surface.
    blob = (out.error_message or "") + "".join(out.headers.values())
    assert "sk-test-secret-key-do-not-leak" not in blob
