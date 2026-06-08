"""An OpenAI Responses-API client with the same interface.

A growing number of relays front OpenAI's newer ``/responses`` endpoint rather
than the classic ``/chat/completions`` one. This client speaks that protocol while
presenting the exact same :class:`~zing.models.CompletionOutcome` surface as the
other clients, so every detector runs against it unchanged. It translates a
:class:`RequestSpec` (OpenAI Chat-shaped) into a Responses request (``input`` items
plus top-level ``instructions``) and normalizes the response — the ``output`` item
list, ``function_call`` items, and ``input_tokens``/``output_tokens`` usage — back
into the common outcome shape.
"""

from __future__ import annotations

import json
import time
from typing import Any

from zing.clients.base import BaseHTTPClient
from zing.models import CompletionOutcome, RequestSpec
from zing.utils.redact import redact_headers, redact_text
from zing.utils.sse import parse_sse_line, try_load_event

_DEFAULT_MAX_TOKENS = 1024

# Responses status / incomplete_details -> OpenAI-style finish_reason.
_FINISH = {
    "completed": "stop",
    "max_output_tokens": "length",
    "content_filter": "content_filter",
}


class ResponsesClient(BaseHTTPClient):
    @property
    def responses_url(self) -> str:
        return f"{self.base_url}/responses"

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "zing-audit/0.1",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.headers)
        return headers

    # -- request translation ------------------------------------------------ #
    def _build_body(self, spec: RequestSpec) -> dict[str, Any]:
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []
        for msg in spec.messages:
            role = msg.get("role")
            content = msg.get("content", "")
            # Pull plain-string system prompts up into top-level instructions; a
            # list-of-parts system message falls through and is passed as an input item.
            if role == "system" and isinstance(content, str) and content:
                instructions.append(content)
                continue
            # list-of-parts content (multimodal) passes through unchanged; the caller
            # is responsible for using Responses part shapes (input_text/input_image).
            input_items.append({"role": role, "content": content})

        # Responses uses max_output_tokens; honor the OpenAI-side reasoning override.
        max_tokens = (
            spec.max_tokens
            or spec.extra_body.get("max_completion_tokens")
            or _DEFAULT_MAX_TOKENS
        )
        body: dict[str, Any] = {
            "model": self.config.model,
            "input": input_items,
            "max_output_tokens": int(max_tokens),
            "stream": spec.stream,
        }
        if instructions:
            body["instructions"] = "\n\n".join(instructions)
        if spec.temperature is not None:
            body["temperature"] = spec.temperature
        if spec.top_p is not None:
            body["top_p"] = spec.top_p
        if spec.tools:
            body["tools"] = [self._translate_tool(t) for t in spec.tools]
            choice = self._translate_tool_choice(spec.tool_choice)
            if choice is not None:
                body["tool_choice"] = choice
        # response_format / seed / stop have no stable Responses equivalent — dropped.
        extra = {k: v for k, v in spec.extra_body.items() if k != "max_completion_tokens"}
        body.update(extra)
        return body

    @staticmethod
    def _translate_tool(tool: dict[str, Any]) -> dict[str, Any]:
        """OpenAI Chat function tool -> Responses function tool (flattened shape)."""
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        return {
            "type": "function",
            "name": fn.get("name", "tool"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        }

    @staticmethod
    def _translate_tool_choice(choice: Any) -> Any:
        if choice in (None, "none", "auto", "required"):
            return choice if choice != "none" else None
        if isinstance(choice, dict):
            fn = choice.get("function") or {}
            if fn.get("name"):
                return {"type": "function", "name": fn["name"]}
        return choice

    @staticmethod
    def _usage(raw: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        inp = raw.get("input_tokens")
        out = raw.get("output_tokens")
        total = raw.get("total_tokens")
        usage: dict[str, Any] = {"input_tokens": inp, "output_tokens": out}
        if isinstance(inp, int) and isinstance(out, int):
            usage["prompt_tokens"] = inp
            usage["completion_tokens"] = out
            usage["total_tokens"] = total if isinstance(total, int) else inp + out
        return usage

    @staticmethod
    def _parse_output(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        """Concatenate text and collect function calls from a Responses payload."""
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        output = data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "message":
                    for part in item.get("content") or []:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            text_parts.append(str(part.get("text", "")))
                elif itype == "function_call":
                    tool_calls.append(
                        {
                            "id": item.get("call_id") or item.get("id"),
                            "type": "function",
                            "function": {
                                "name": item.get("name"),
                                "arguments": item.get("arguments") or "{}",
                            },
                        }
                    )
        # The convenience aggregate is preferred when no message items carried text.
        if not text_parts and isinstance(data.get("output_text"), str):
            text_parts.append(data["output_text"])
        return "".join(text_parts), tool_calls

    @staticmethod
    def _finish_for(data: dict[str, Any]) -> str | None:
        status = data.get("status")
        details = data.get("incomplete_details")
        if isinstance(details, dict) and details.get("reason"):
            return _FINISH.get(details["reason"], details["reason"])
        if status == "incomplete":
            return "length"
        return _FINISH.get(status or "", status)

    # -- endpoints ---------------------------------------------------------- #
    async def list_models(self) -> tuple[CompletionOutcome, list[str]]:
        """GET /models. Returns (outcome, list-of-model-ids)."""
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
                response = await client.post(self.responses_url, json=body)
                duration_ms = (time.perf_counter() - started) * 1000
                headers = redact_headers(dict(response.headers), extra_secrets=self._extra_secrets())
                if response.status_code >= 400:
                    return self._error_from_response(response, duration_ms, headers)

                data = response.json() if response.content else {}
                if not isinstance(data, dict):
                    data = {}
                text, tool_calls = self._parse_output(data)
                return CompletionOutcome(
                    ok=True,
                    status_code=response.status_code,
                    content=redact_text(text, extra_secrets=self._extra_secrets()),
                    tool_calls=tool_calls,
                    finish_reason=self._finish_for(data),
                    usage=self._usage(data.get("usage")),
                    duration_ms=duration_ms,
                    ttft_ms=duration_ms,
                    headers=headers,
                    model_returned=data.get("model"),
                )
        except Exception as exc:
            return self._exception_outcome(exc, started)

    async def _complete_stream(self, body: dict[str, Any]) -> CompletionOutcome:
        started = time.perf_counter()
        first_token_at: float | None = None
        content_parts: list[str] = []
        chunk_timings: list[float] = []
        # Tool-call args accumulate by output_index across delta/done events.
        tool_args: dict[int, str] = {}
        tool_meta: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        model_returned: str | None = None
        finish_reason: str | None = None

        try:
            async with self._session() as client, client.stream(
                "POST", self.responses_url, json=body
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

                    if etype == "response.output_text.delta":
                        piece = event.get("delta", "")
                        if isinstance(piece, str) and piece:
                            if first_token_at is None:
                                first_token_at = time.perf_counter()
                            content_parts.append(piece)
                            chunk_timings.append(now)
                    elif etype == "response.output_item.added":
                        item = event.get("item") or {}
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            idx = event.get("output_index", 0)
                            tool_meta[idx] = {
                                "id": item.get("call_id") or item.get("id"),
                                "name": item.get("name"),
                            }
                            tool_args.setdefault(idx, "")
                    elif etype == "response.function_call_arguments.delta":
                        idx = event.get("output_index", 0)
                        tool_args[idx] = tool_args.get(idx, "") + str(event.get("delta", ""))
                    elif etype == "response.function_call_arguments.done":
                        idx = event.get("output_index", 0)
                        if isinstance(event.get("arguments"), str):
                            tool_args[idx] = event["arguments"]
                    elif etype in ("response.completed", "response.incomplete"):
                        resp = event.get("response") or {}
                        if isinstance(resp, dict):
                            model_returned = resp.get("model") or model_returned
                            usage = self._usage(resp.get("usage")) or usage
                            finish_reason = self._finish_for(resp) or finish_reason
                            # function_call items may only appear in the final payload.
                            _, final_tools = self._parse_output(resp)
                            for tc in final_tools:
                                idx = len(tool_meta)
                                if not any(
                                    m.get("id") == tc["id"] for m in tool_meta.values()
                                ):
                                    tool_meta[idx] = {
                                        "id": tc["id"],
                                        "name": tc["function"]["name"],
                                    }
                                    tool_args.setdefault(idx, tc["function"]["arguments"])

                status_code = response.status_code

            duration_ms = (time.perf_counter() - started) * 1000
            ttft_ms = (first_token_at - started) * 1000 if first_token_at is not None else None
            tool_calls = [
                {
                    "id": meta.get("id"),
                    "type": "function",
                    "function": {
                        "name": meta.get("name"),
                        "arguments": tool_args.get(idx) or "{}",
                    },
                }
                for idx, meta in sorted(tool_meta.items())
            ]
            return CompletionOutcome(
                ok=True,
                status_code=status_code,
                content=redact_text("".join(content_parts), extra_secrets=self._extra_secrets()),
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
                duration_ms=duration_ms,
                ttft_ms=ttft_ms,
                chunk_timings_ms=chunk_timings,
                chunk_count=len(chunk_timings),
                headers=headers,
                model_returned=model_returned,
            )
        except Exception as exc:
            return self._exception_outcome(exc, started)
