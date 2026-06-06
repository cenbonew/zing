"""An Anthropic-native (Messages API) client with the same interface.

Some relays front Anthropic's ``/v1/messages`` API rather than OpenAI's
``/v1/chat/completions``. This client speaks that protocol while presenting the
exact same :class:`~zing.models.CompletionOutcome` surface as the OpenAI client,
so every detector runs against either backend unchanged. It translates a
:class:`RequestSpec` (OpenAI-shaped) into a Messages request and normalizes the
response — content blocks, ``tool_use`` blocks, and ``input_tokens``/
``output_tokens`` usage — back into the common outcome shape.
"""

from __future__ import annotations

import json
import time
from typing import Any

from zing.clients.base import BaseHTTPClient
from zing.models import CompletionOutcome, RequestSpec
from zing.utils.redact import redact_headers, redact_text
from zing.utils.sse import parse_sse_line, try_load_event

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 1024

# Anthropic stop_reason -> OpenAI-style finish_reason.
_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


class AnthropicClient(BaseHTTPClient):
    @property
    def messages_url(self) -> str:
        return f"{self.base_url}/messages"

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "zing-audit/0.1",
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        if self.config.api_key:
            headers["x-api-key"] = self.config.api_key
        headers.update(self.config.headers)
        return headers

    # -- request translation ------------------------------------------------ #
    def _build_body(self, spec: RequestSpec) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for msg in spec.messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                if isinstance(content, str) and content:
                    system_parts.append(content)
                continue
            messages.append({"role": role, "content": content})

        # Anthropic requires max_tokens; honor the OpenAI-side reasoning override too.
        max_tokens = (
            spec.max_tokens
            or spec.extra_body.get("max_completion_tokens")
            or _DEFAULT_MAX_TOKENS
        )
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "stream": spec.stream,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if spec.temperature is not None:
            # Anthropic's temperature range is 0..1; clamp so an OpenAI-style 2.0
            # doesn't get rejected.
            body["temperature"] = max(0.0, min(1.0, spec.temperature))
        if spec.top_p is not None:
            body["top_p"] = spec.top_p
        if spec.stop is not None:
            body["stop_sequences"] = [spec.stop] if isinstance(spec.stop, str) else list(spec.stop)
        if spec.tools:
            body["tools"] = [self._translate_tool(t) for t in spec.tools]
            choice = self._translate_tool_choice(spec.tool_choice)
            if choice is not None:
                body["tool_choice"] = choice
        # response_format / seed have no Messages-API equivalent — intentionally dropped.
        extra = {k: v for k, v in spec.extra_body.items() if k != "max_completion_tokens"}
        body.update(extra)
        return body

    @staticmethod
    def _translate_tool(tool: dict[str, Any]) -> dict[str, Any]:
        """OpenAI function tool -> Anthropic tool (parameters -> input_schema)."""
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        return {
            "name": fn.get("name", "tool"),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }

    @staticmethod
    def _translate_tool_choice(choice: Any) -> dict[str, Any] | None:
        if choice in (None, "none"):
            return None
        if choice == "auto":
            return {"type": "auto"}
        if choice == "required":
            return {"type": "any"}
        if isinstance(choice, dict):
            fn = choice.get("function") or {}
            if fn.get("name"):
                return {"type": "tool", "name": fn["name"]}
        return {"type": "auto"}

    @staticmethod
    def _usage(raw: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        inp = raw.get("input_tokens")
        out = raw.get("output_tokens")
        usage: dict[str, Any] = {"input_tokens": inp, "output_tokens": out}
        if isinstance(inp, int) and isinstance(out, int):
            usage["prompt_tokens"] = inp
            usage["completion_tokens"] = out
            usage["total_tokens"] = inp + out
        return usage

    # -- endpoints ---------------------------------------------------------- #
    async def list_models(self) -> tuple[CompletionOutcome, list[str]]:
        started = time.perf_counter()
        try:
            async with self._session() as client:
                response = await client.get(self.models_url)
                duration_ms = (time.perf_counter() - started) * 1000
                headers = redact_headers(dict(response.headers), extra_secrets=self._extra_secrets())
                if response.status_code >= 400:
                    return self._error_from_response(response, duration_ms, headers), []
                ids: list[str] = []
                content = ""
                try:
                    data = response.json()
                    raw = data.get("data", data) if isinstance(data, dict) else data
                    if isinstance(raw, list):
                        ids = [str(item.get("id", item)) for item in raw if item]
                        content = ", ".join(ids[:20])
                    else:
                        content = json.dumps(data, ensure_ascii=False)[:500]
                except Exception:
                    content = response.text[:500]
                content = redact_text(content, extra_secrets=self._extra_secrets())
                return (
                    CompletionOutcome(
                        ok=True,
                        status_code=response.status_code,
                        content=content,
                        duration_ms=duration_ms,
                        headers=headers,
                    ),
                    ids,
                )
        except Exception as exc:
            return self._exception_outcome(exc, started), []

    async def complete(self, spec: RequestSpec) -> CompletionOutcome:
        body = self._build_body(spec)
        if spec.stream:
            return await self._complete_stream(body)
        return await self._complete_nonstream(body)

    async def _complete_nonstream(self, body: dict[str, Any]) -> CompletionOutcome:
        started = time.perf_counter()
        try:
            async with self._session() as client:
                response = await client.post(self.messages_url, json=body)
                duration_ms = (time.perf_counter() - started) * 1000
                headers = redact_headers(dict(response.headers), extra_secrets=self._extra_secrets())
                if response.status_code >= 400:
                    return self._error_from_response(response, duration_ms, headers)

                data = response.json()
                blocks = data.get("content") or [] if isinstance(data, dict) else []
                text = "".join(
                    str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text"
                )
                tool_calls = [
                    {
                        "id": b.get("id"),
                        "type": "function",
                        "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {})},
                    }
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
                stop_reason = data.get("stop_reason") if isinstance(data, dict) else None
                return CompletionOutcome(
                    ok=True,
                    status_code=response.status_code,
                    content=redact_text(text, extra_secrets=self._extra_secrets()),
                    tool_calls=tool_calls,
                    finish_reason=_FINISH.get(stop_reason or "", stop_reason),
                    usage=self._usage(data.get("usage") if isinstance(data, dict) else None),
                    duration_ms=duration_ms,
                    ttft_ms=duration_ms,
                    headers=headers,
                    model_returned=data.get("model") if isinstance(data, dict) else None,
                )
        except Exception as exc:
            return self._exception_outcome(exc, started)

    async def _complete_stream(self, body: dict[str, Any]) -> CompletionOutcome:
        started = time.perf_counter()
        first_token_at: float | None = None
        content_parts: list[str] = []
        chunk_timings: list[float] = []
        tool_blocks: dict[int, dict[str, Any]] = {}
        input_tokens: int | None = None
        output_tokens: int | None = None
        model_returned: str | None = None
        finish_reason: str | None = None

        try:
            async with self._session() as client, client.stream(
                "POST", self.messages_url, json=body
            ) as response:
                headers = redact_headers(dict(response.headers), extra_secrets=self._extra_secrets())
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", errors="replace")
                    duration_ms = (time.perf_counter() - started) * 1000
                    return CompletionOutcome(
                        ok=False,
                        status_code=response.status_code,
                        duration_ms=duration_ms,
                        headers=headers,
                        error_type="http_error",
                        error_message=redact_text(raw[:1000], extra_secrets=self._extra_secrets()),
                    )

                async for line in response.aiter_lines():
                    payload = parse_sse_line(line)
                    if payload is None or payload == "[DONE]":
                        continue
                    event = try_load_event(payload)
                    if event is None:
                        continue
                    etype = event.get("type")
                    now = (time.perf_counter() - started) * 1000

                    if etype == "message_start":
                        msg = event.get("message") or {}
                        model_returned = msg.get("model") or model_returned
                        usage = msg.get("usage") or {}
                        if isinstance(usage.get("input_tokens"), int):
                            input_tokens = usage["input_tokens"]
                    elif etype == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            tool_blocks[event.get("index", 0)] = {
                                "id": block.get("id"),
                                "name": block.get("name"),
                                "args": "",
                            }
                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            piece = delta.get("text", "")
                            if piece:
                                if first_token_at is None:
                                    first_token_at = time.perf_counter()
                                content_parts.append(piece)
                                chunk_timings.append(now)
                        elif delta.get("type") == "input_json_delta":
                            idx = event.get("index", 0)
                            if idx in tool_blocks:
                                tool_blocks[idx]["args"] += delta.get("partial_json", "")
                    elif etype == "message_delta":
                        delta = event.get("delta") or {}
                        finish_reason = _FINISH.get(
                            delta.get("stop_reason") or "", delta.get("stop_reason")
                        ) or finish_reason
                        usage = event.get("usage") or {}
                        if isinstance(usage.get("output_tokens"), int):
                            output_tokens = usage["output_tokens"]

                status_code = response.status_code

            duration_ms = (time.perf_counter() - started) * 1000
            ttft_ms = (first_token_at - started) * 1000 if first_token_at is not None else None
            tool_calls = [
                {
                    "id": tb.get("id"),
                    "type": "function",
                    "function": {"name": tb.get("name"), "arguments": tb.get("args") or "{}"},
                }
                for tb in tool_blocks.values()
            ]
            return CompletionOutcome(
                ok=True,
                status_code=status_code,
                content=redact_text("".join(content_parts), extra_secrets=self._extra_secrets()),
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=self._usage({"input_tokens": input_tokens, "output_tokens": output_tokens}),
                duration_ms=duration_ms,
                ttft_ms=ttft_ms,
                chunk_timings_ms=chunk_timings,
                chunk_count=len(chunk_timings),
                headers=headers,
                model_returned=model_returned,
            )
        except Exception as exc:
            return self._exception_outcome(exc, started)
