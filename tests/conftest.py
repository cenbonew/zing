"""Shared test fixtures: a configurable in-process mock OpenAI-compatible server.

Tests never touch the network. We inject an ``httpx.MockTransport`` into the real
:class:`OpenAICompatibleClient`, so the client, SSE parsing, and detectors all run
against deterministic, scenario-driven responses. The :class:`MockServer` exposes
knobs to reproduce the divergences zing hunts for: model downgrade, context
truncation, fake streaming, missing/inflated usage, tool calls, and JSON mode.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest

from zing.clients import OpenAICompatibleClient
from zing.config import AuditOptions
from zing.context import AuditContext
from zing.knowledge import load_knowledge_base
from zing.models import TargetConfig

BASE_URL = "http://relay.test/v1"
DEFAULT_MODEL = "gpt-4o"


@dataclass
class MockServer:
    """A configurable OpenAI-compatible endpoint backed by ``httpx.MockTransport``.

    Toggle the knobs before issuing a request to simulate a scenario. Every knob
    has an honest default, so an untouched server behaves like a faithful relay.
    """

    # Identity / listing
    served_model: str = DEFAULT_MODEL          # value echoed in the "model" field
    self_identity: str = "I am GPT-4o, a large language model made by OpenAI."
    model_ids: list[str] = field(
        default_factory=lambda: ["gpt-4o", "gpt-4o-mini", "text-embedding-3-small"]
    )

    # Failure injection
    models_status: int = 200                   # status for GET /v1/models
    chat_status: int = 200                      # status for POST /v1/chat/completions
    error_body: dict = field(
        default_factory=lambda: {"error": {"message": "boom", "type": "server_error"}}
    )

    # Context-window behaviour
    truncate_above_tokens: int | None = None    # 400 once prompt exceeds this many chars
    recall_needle: bool = True                  # echo the embedded needle back

    # Content
    reply_text: str | None = None               # overrides the default echo reply

    # Streaming
    fake_stream: bool = False                    # emit one giant chunk instead of many
    stream_chunk_words: int = 5                  # words per delta chunk when honest

    # Usage
    emit_usage: bool = True
    inflate_usage_factor: float = 1.0            # multiply reported token counts
    usage_in_stream: bool = True                 # include a trailing usage chunk

    # Capability
    tool_call: dict | None = None                # echo this tool_call back if set
    finish_reason: str = "stop"

    # Bookkeeping for assertions.
    requests: list[dict] = field(default_factory=list)

    # -- handler ----------------------------------------------------------- #
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/models"):
            return self._models_response()
        if request.method == "POST" and path.endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            self.requests.append(body)
            return self._chat_response(body)
        return httpx.Response(404, json={"error": {"message": f"no route for {path}"}})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # -- /v1/models -------------------------------------------------------- #
    def _models_response(self) -> httpx.Response:
        if self.models_status >= 400:
            return httpx.Response(self.models_status, json=self.error_body)
        data = {"object": "list", "data": [{"id": mid, "object": "model"} for mid in self.model_ids]}
        return httpx.Response(200, json=data)

    # -- /v1/chat/completions ---------------------------------------------- #
    def _chat_response(self, body: dict) -> httpx.Response:
        if self.chat_status >= 400:
            return httpx.Response(self.chat_status, json=self.error_body)

        prompt_chars = len(json.dumps(body.get("messages", [])))
        if self.truncate_above_tokens is not None and prompt_chars > self.truncate_above_tokens:
            return httpx.Response(
                400,
                json={"error": {"message": "maximum context length exceeded", "type": "invalid_request_error"}},
            )

        text = self._reply_for(body)
        if body.get("stream"):
            return self._sse_response(body, text)
        return self._json_response(body, text)

    def _reply_for(self, body: dict) -> str:
        """Decide the assistant text for this request."""
        if self.reply_text is not None:
            return self.reply_text
        messages = body.get("messages", [])
        user = " ".join(
            m.get("content", "") for m in messages if isinstance(m.get("content"), str)
        ).lower()
        # Identity question -> served identity (the downgrade tell).
        if (
            "who are you" in user
            or "which model" in user
            or "what model are you" in user
            or "identify" in user
        ):
            return self.self_identity
        # Needle recall: echo back the marker found in the prompt if asked to.
        if "pass phrase" in user or "secret" in user:
            return self._needle_from(messages) if self.recall_needle else "I don't know."
        # Echo-canary style instruction "reply with exactly this text: X".
        marker = self._marker_after(user, "nothing else:")
        if marker:
            return marker
        return "The quick brown fox."

    @staticmethod
    def _marker_after(text: str, sep: str) -> str | None:
        if sep in text:
            tail = text.split(sep, 1)[1].strip()
            return tail or None
        return None

    @staticmethod
    def _needle_from(messages: list[dict]) -> str:
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str) and "pass phrase is" in content.lower():
                after = content.lower().split("pass phrase is", 1)[1]
                token = after.strip().split()[0].strip(".,").upper()
                return token
        return "UNKNOWN"

    def _usage(self, body: dict, completion_tokens: int) -> dict | None:
        if not self.emit_usage:
            return None
        prompt = max(1, len(json.dumps(body.get("messages", []))) // 4)
        prompt = int(prompt * self.inflate_usage_factor)
        completion = int(completion_tokens * self.inflate_usage_factor)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    def _json_response(self, body: dict, text: str) -> httpx.Response:
        message: dict = {"role": "assistant", "content": text}
        if self.tool_call is not None:
            message["tool_calls"] = [self.tool_call]
            message["content"] = None
        payload = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": self.served_model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if self.tool_call else self.finish_reason,
                }
            ],
        }
        usage = self._usage(body, completion_tokens=max(1, len(text.split())))
        if usage is not None:
            payload["usage"] = usage
        return httpx.Response(200, json=payload)

    def _sse_response(self, body: dict, text: str) -> httpx.Response:
        words = text.split() or [text]
        chunks: list[str] = []

        def event(delta: dict, finish: str | None = None) -> str:
            ev = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "model": self.served_model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(ev)}\n\n"

        # role priming chunk
        chunks.append(event({"role": "assistant"}))
        if self.fake_stream:
            # One buffered-then-dumped chunk: the fake-streaming tell.
            chunks.append(event({"content": text}))
        else:
            step = max(1, self.stream_chunk_words)
            for i in range(0, len(words), step):
                piece = " ".join(words[i : i + step])
                if i + step < len(words):
                    piece += " "
                chunks.append(event({"content": piece}))
        if self.tool_call is not None:
            chunks.append(event({"tool_calls": [self.tool_call]}))
        # final chunk with finish_reason
        chunks.append(event({}, finish="tool_calls" if self.tool_call else self.finish_reason))

        usage = self._usage(body, completion_tokens=max(1, len(words)))
        if usage is not None and self.usage_in_stream:
            usage_event = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "model": self.served_model,
                "choices": [],
                "usage": usage,
            }
            chunks.append(f"data: {json.dumps(usage_event)}\n\n")
        chunks.append("data: [DONE]\n\n")

        return httpx.Response(
            200,
            content="".join(chunks).encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def mock_server() -> MockServer:
    """An honest mock relay; mutate its knobs in the test to inject a scenario."""
    return MockServer()


@pytest.fixture(scope="session")
def knowledge_base():
    """The packaged knowledge base (loaded once)."""
    return load_knowledge_base()


@pytest.fixture
def target_config() -> TargetConfig:
    return TargetConfig(
        name="target",
        kind="target",
        base_url=BASE_URL,
        api_key="sk-test-secret-key-do-not-leak",
        model=DEFAULT_MODEL,
    )


@pytest.fixture
async def client(mock_server: MockServer, target_config: TargetConfig) -> Iterator[OpenAICompatibleClient]:
    """An opened client wired to the mock server's transport."""
    async with OpenAICompatibleClient(target_config, transport=mock_server.transport) as c:
        yield c


@pytest.fixture
async def audit_context(
    mock_server: MockServer,
    target_config: TargetConfig,
    knowledge_base,
) -> Iterator[AuditContext]:
    """A fully-formed AuditContext for exercising detectors against the mock."""
    async with OpenAICompatibleClient(target_config, transport=mock_server.transport) as c:
        ctx = AuditContext(
            target=target_config,
            client=c,
            options=AuditOptions(suite="full"),
            kb=knowledge_base,
            profile=knowledge_base.resolve(target_config.model),
        )
        yield ctx
