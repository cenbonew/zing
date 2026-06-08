"""HTTP clients for talking to relay endpoints.

Three wire protocols are supported behind one :class:`~zing.models.CompletionOutcome`
interface: OpenAI Chat Completions, the OpenAI Responses API, and the Anthropic
Messages API. ``make_client`` picks the right one from the target's ``api`` field
(``auto`` infers it).
"""

from __future__ import annotations

import httpx

from zing.clients.anthropic import AnthropicClient
from zing.clients.base import BaseHTTPClient
from zing.clients.openai_compatible import OpenAICompatibleClient
from zing.clients.responses import ResponsesClient
from zing.models import TargetConfig

Client = BaseHTTPClient

__all__ = [
    "AnthropicClient",
    "BaseHTTPClient",
    "Client",
    "OpenAICompatibleClient",
    "ResponsesClient",
    "detect_api",
    "make_client",
]


def detect_api(config: TargetConfig) -> str:
    """Resolve an ``api`` of 'auto' to a concrete 'openai' | 'anthropic' | 'responses'."""
    flavor = (config.api or "auto").lower()
    if flavor in ("openai", "anthropic", "responses"):
        return flavor
    url = (config.base_url or "").lower()
    model = (config.model or "").lower()
    if url.rstrip("/").endswith("/responses"):
        return "responses"
    if "anthropic" in url or url.rstrip("/").endswith("/messages") or model.startswith("claude"):
        return "anthropic"
    return "openai"


def make_client(
    config: TargetConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> Client:
    """Construct the client matching the target's (possibly auto-detected) protocol."""
    flavor = detect_api(config)
    if flavor == "anthropic":
        return AnthropicClient(config, transport=transport)
    if flavor == "responses":
        return ResponsesClient(config, transport=transport)
    return OpenAICompatibleClient(config, transport=transport)
