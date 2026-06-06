"""Minimal Server-Sent-Events helpers for OpenAI-compatible streaming.

Relays diverge in subtle ways here, so we parse raw SSE rather than trusting an
SDK. We also surface per-event arrival timing, which the streaming-authenticity
detector uses to tell real token streaming from buffered-then-chunked fakes.
"""

from __future__ import annotations

import json
from typing import Any


def parse_sse_line(line: str) -> str | None:
    """Return the JSON payload string of a `data:` SSE line, or None to skip.

    Returns the sentinel ``"[DONE]"`` unchanged so callers can detect end-of-stream.
    Comment lines (starting with ``:``) and non-data lines are skipped.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith(":"):
        return None
    if not stripped.startswith("data:"):
        return None
    payload = stripped[len("data:"):].strip()
    return payload or None


def extract_content_delta(event: dict[str, Any]) -> str:
    """Pull the incremental text content out of a chat.completion.chunk event."""
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    if isinstance(content, str):
        return content
    # Some relays nest content as a list of parts.
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return ""


def extract_tool_calls_delta(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull incremental tool_call fragments out of a streaming event."""
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(delta, dict):
        return []
    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list):
        return [tc for tc in tool_calls if isinstance(tc, dict)]
    return []


def try_load_event(payload: str) -> dict[str, Any] | None:
    """Parse an SSE JSON payload, tolerating malformed lines."""
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    return event if isinstance(event, dict) else None
