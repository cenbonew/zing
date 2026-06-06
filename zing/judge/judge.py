"""A thin LLM-as-judge wrapper around a trusted OpenAI-compatible endpoint."""

from __future__ import annotations

import json
import re
from typing import Any

from zing.clients import OpenAICompatibleClient
from zing.models import RequestSpec

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class JudgeUnavailable(Exception):
    """Raised when a judge is required but not configured."""


class Judge:
    """Wraps a trusted model used to assess fuzzy, non-deterministic signals.

    Always point this at a model you trust (e.g. an official endpoint), distinct
    from the relay under audit, so the verdict is not graded by the suspect.
    """

    def __init__(self, client: OpenAICompatibleClient, model: str) -> None:
        self.client = client
        self.model = model

    async def evaluate_json(
        self, system: str, user: str, *, max_tokens: int = 600
    ) -> dict[str, Any]:
        """Ask the judge to return a JSON object and parse it robustly."""
        spec = RequestSpec(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        outcome = await self.client.complete(spec)
        if not outcome.ok or not outcome.content:
            return {"_error": outcome.error_message or "judge call failed"}
        return _parse_json(outcome.content)


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    fence = _JSON_FENCE.search(text)
    if fence:
        try:
            return json.loads(fence.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    obj = _JSON_OBJECT.search(text)
    if obj:
        try:
            return json.loads(obj.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return {"_error": "could not parse judge JSON", "_raw": text[:500]}
