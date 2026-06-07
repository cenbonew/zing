"""Model-identity detector tests — the core 货不对板 check.

Covers the false-positive/false-negative fixes: the #1 silent downgrade
(gpt-4o -> gpt-4o-mini) must be caught, while an honest model that merely
contrasts itself against rival brands must not be flagged HIGH.
"""

from __future__ import annotations

import json

import httpx
import pytest

from zing.clients import OpenAICompatibleClient
from zing.config import AuditOptions
from zing.context import AuditContext
from zing.detectors.helpers import contains_word, contains_word_any, words_present
from zing.detectors.model_identity import ModelIdentityDetector
from zing.models import Severity, Status, TargetConfig


@pytest.mark.parametrize(
    "requested,returned,diverges",
    [
        ("gpt-4o", "gpt-4o-mini", True),     # the #1 silent downgrade
        ("gpt-4.1", "gpt-4.1-nano", True),
        ("gpt-5.4", "gpt-5.4-mini", True),
        ("gpt-4o", "gpt-4o-2024-08-06", False),         # snapshot, not a downgrade
        ("claude-3-haiku", "claude-3-haiku-20240307", False),
        ("gpt-4o", "qwen-max", True),                    # different family
        ("gpt-4o", "gpt-4o", False),
    ],
)
def test_model_field_diverges(requested, returned, diverges):
    assert ModelIdentityDetector._model_field_diverges(requested, returned) is diverges


def test_contains_word_respects_boundaries():
    assert not contains_word("metadata about the model", "meta")
    assert not contains_word("I am ChatGPT", "gpt")
    assert contains_word("I am ChatGPT", "chatgpt")
    assert contains_word("Unlike Llama models", "llama")
    assert contains_word("I'm GPT-5", "gpt-5")


def test_words_present_filters_to_whole_words():
    text = "I am Claude by Anthropic, not a Google or Meta model"
    assert set(words_present(text, ["google", "meta", "metadata"])) == {"google", "meta"}
    assert contains_word_any(text, ["claude", "anthropic"])


async def test_rival_brand_self_id_is_high(audit_context, mock_server):
    # gpt-4o relay that self-identifies as Claude -> strong substitution signal.
    mock_server.self_identity = "I am Claude, an AI assistant made by Anthropic."
    result = await ModelIdentityDetector().run(audit_context)
    assert result.status == Status.FAIL
    assert any(f.severity == Severity.HIGH for f in result.findings)


async def test_honest_self_id_with_negation_is_not_high(audit_context, mock_server):
    # Honest gpt-4o naming rivals only to contrast itself must NOT be flagged HIGH.
    mock_server.self_identity = (
        "I am GPT-4o, made by OpenAI — not Claude, Gemini, or a Meta model."
    )
    result = await ModelIdentityDetector().run(audit_context)
    assert all(f.severity != Severity.HIGH for f in result.findings)
    assert result.status != Status.FAIL


async def test_echoed_downgrade_model_field_is_flagged(audit_context, mock_server):
    # The relay claims gpt-4o but echoes model=gpt-4o-mini.
    mock_server.served_model = "gpt-4o-mini"
    result = await ModelIdentityDetector().run(audit_context)
    field_findings = [f for f in result.findings if f.id == "model_identity.model_field"]
    assert field_findings and field_findings[0].status == Status.WARN


def test_forbidden_brands_includes_global_rivals(knowledge_base):
    # A DeepSeek profile's forbidden set must include rivals the KB never listed
    # (Doubao/ByteDance) so a Doubao substitution can't slip through.
    model = knowledge_base.resolve("deepseek-v4-flash").model
    forbidden = {b.lower() for b in ModelIdentityDetector._forbidden_brands(model)}
    assert {"doubao", "豆包", "bytedance", "字节跳动"} <= forbidden
    assert "deepseek" not in forbidden  # the model's own brand is never forbidden


class _SelfIdMock:
    def __init__(self, self_identity: str, served_model: str = "deepseek-v4-flash"):
        self.self_identity = self_identity
        self.served_model = served_model

    @property
    def transport(self):
        return httpx.MockTransport(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        user = " ".join(m.get("content", "") for m in body["messages"]).lower()
        is_id = "what model are you" in user or "who are you" in user or "which model" in user
        text = self.self_identity if is_id else "ok"
        return httpx.Response(
            200,
            json={
                "model": self.served_model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )


async def test_doubao_sold_as_deepseek_is_detected(knowledge_base):
    # A ByteDance/Doubao model audited against the deepseek-v4-flash profile must FAIL.
    mock = _SelfIdMock("I am Doubao (豆包), a model developed by ByteDance (字节跳动).")
    target = TargetConfig(
        base_url="https://relay.test/v1", api_key="sk-x-123456",
        model="doubao-seed-2-0-lite-260428", claimed_model="deepseek-v4-flash",
    )
    assert target.claimed == "deepseek-v4-flash"
    async with OpenAICompatibleClient(target, transport=mock.transport) as c:
        ctx = AuditContext(
            target=target, client=c, options=AuditOptions(suite="standard"),
            kb=knowledge_base, profile=knowledge_base.resolve(target.claimed),
        )
        result = await ModelIdentityDetector().run(ctx)
    assert result.status == Status.FAIL
    assert any(f.severity == Severity.HIGH for f in result.findings)


async def test_genuine_deepseek_self_id_passes(knowledge_base):
    mock = _SelfIdMock("I am DeepSeek-V4, a model from DeepSeek (深度求索).")
    target = TargetConfig(base_url="https://relay.test/v1", api_key="sk-x-123456", model="deepseek-v4-flash")
    async with OpenAICompatibleClient(target, transport=mock.transport) as c:
        ctx = AuditContext(
            target=target, client=c, options=AuditOptions(suite="standard"),
            kb=knowledge_base, profile=knowledge_base.resolve("deepseek-v4-flash"),
        )
        result = await ModelIdentityDetector().run(ctx)
    self_id = [f for f in result.findings if f.id == "model_identity.self_id"]
    assert self_id and self_id[0].status == Status.PASS
