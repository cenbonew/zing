"""A small OpenAI-compatible HTTP client purpose-built for auditing.

It deliberately speaks raw HTTP instead of using the official SDK: relays diverge
in subtle ways and the raw evidence (status codes, headers, partial streams,
per-chunk timing) is exactly what the detectors reason about.

The client can be reused across many calls via ``async with`` (one pooled
connection — important for context-window binary search and reliability probes),
or used statelessly for one-off calls. A custom ``transport`` can be injected for
deterministic tests against a mock server.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx

from zing.models import CompletionOutcome, RequestSpec, TargetConfig
from zing.utils.redact import redact_headers, redact_json, redact_text
from zing.utils.sse import (
    extract_content_delta,
    extract_tool_calls_delta,
    parse_sse_line,
    try_load_event,
)


class OpenAICompatibleClient:
    def __init__(
        self,
        config: TargetConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.base_url = self._normalize_base_url(config.base_url)
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle ---------------------------------------------------------- #
    async def __aenter__(self) -> OpenAICompatibleClient:
        self._client = httpx.AsyncClient(
            timeout=self._timeout(),
            transport=self.transport,
            headers=self._headers(),
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @asynccontextmanager
    async def _session(self):
        """Yield a client: the pooled one if open, else a short-lived one."""
        if self._client is not None:
            yield self._client
            return
        client = httpx.AsyncClient(
            timeout=self._timeout(),
            transport=self.transport,
            headers=self._headers(),
            follow_redirects=False,
        )
        try:
            yield client
        finally:
            await client.aclose()

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        stripped = base_url.strip().rstrip("/")
        if not stripped.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return stripped

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"

    def _extra_secrets(self) -> list[str | None]:
        """Secrets to scrub from any relay-controlled text before it is stored."""
        return [self.config.api_key]

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "zing-audit/0.1",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.headers)
        return headers

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            self.config.timeout_sec,
            connect=min(15.0, self.config.timeout_sec),
        )

    def _build_body(self, spec: RequestSpec) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": spec.messages,
            "stream": spec.stream,
        }
        optional = {
            "temperature": spec.temperature,
            "max_tokens": spec.max_tokens,
            "top_p": spec.top_p,
            "stop": spec.stop,
            "seed": spec.seed,
            "response_format": spec.response_format,
            "tools": spec.tools,
            "tool_choice": spec.tool_choice,
        }
        for key, value in optional.items():
            if value is not None:
                body[key] = value
        body.update(spec.extra_body)
        if spec.stream and "stream_options" not in body:
            body["stream_options"] = {"include_usage": True}
        return body

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
                outcome = CompletionOutcome(
                    ok=True,
                    status_code=response.status_code,
                    content=content,
                    duration_ms=duration_ms,
                    headers=headers,
                )
                return outcome, ids
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
                response = await client.post(self.chat_completions_url, json=body)
                duration_ms = (time.perf_counter() - started) * 1000
                headers = redact_headers(dict(response.headers), extra_secrets=self._extra_secrets())
                if response.status_code >= 400:
                    return self._error_from_response(response, duration_ms, headers)

                data = response.json()
                choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
                message = choice.get("message") or {}
                content = message.get("content") or ""
                if isinstance(content, list):
                    content = "".join(
                        str(part.get("text", "")) for part in content if isinstance(part, dict)
                    )
                tool_calls = message.get("tool_calls") or []
                return CompletionOutcome(
                    ok=True,
                    status_code=response.status_code,
                    content=redact_text(str(content), extra_secrets=self._extra_secrets()),
                    tool_calls=tool_calls if isinstance(tool_calls, list) else [],
                    finish_reason=choice.get("finish_reason"),
                    usage=data.get("usage") if isinstance(data, dict) else None,
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
        tool_call_parts: list[dict[str, Any]] = []
        chunk_timings: list[float] = []
        usage: dict[str, Any] | None = None
        model_returned: str | None = None
        finish_reason: str | None = None

        try:
            async with self._session() as client, client.stream(
                "POST", self.chat_completions_url, json=body
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
                    if payload is None:
                        continue
                    if payload == "[DONE]":
                        break
                    event = try_load_event(payload)
                    if event is None:
                        continue
                    now = (time.perf_counter() - started) * 1000
                    model_returned = event.get("model") or model_returned
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    choices = event.get("choices") or []
                    if choices and isinstance(choices[0], dict):
                        finish_reason = choices[0].get("finish_reason") or finish_reason
                    delta_text = extract_content_delta(event)
                    if delta_text:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        content_parts.append(delta_text)
                        chunk_timings.append(now)
                    delta_tools = extract_tool_calls_delta(event)
                    if delta_tools:
                        tool_call_parts.extend(delta_tools)

                status_code = response.status_code

            duration_ms = (time.perf_counter() - started) * 1000
            ttft_ms = (first_token_at - started) * 1000 if first_token_at is not None else None
            return CompletionOutcome(
                ok=True,
                status_code=status_code,
                content=redact_text("".join(content_parts), extra_secrets=self._extra_secrets()),
                tool_calls=tool_call_parts,
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

    # -- error shaping ------------------------------------------------------ #
    def _error_from_response(
        self, response: httpx.Response, duration_ms: float, headers: dict[str, str]
    ) -> CompletionOutcome:
        secrets = self._extra_secrets()
        raw_error: dict[str, Any] | None = None
        message = response.text[:1000]
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                # A relay can echo the bearer token / a sibling key inside the error
                # body, so scrub every string value recursively before we keep it.
                raw_error = redact_json(parsed, extra_secrets=secrets)
                error = parsed.get("error") or parsed
                if isinstance(error, dict):
                    message = str(error.get("message") or error)[:1000]
        except Exception:
            pass
        return CompletionOutcome(
            ok=False,
            status_code=response.status_code,
            duration_ms=duration_ms,
            headers=headers,
            error_type="http_error",
            error_message=redact_text(message, extra_secrets=secrets),
            raw_error=raw_error,
        )

    def _exception_outcome(self, exc: Exception, started: float) -> CompletionOutcome:
        duration_ms = (time.perf_counter() - started) * 1000
        return CompletionOutcome(
            ok=False,
            duration_ms=duration_ms,
            error_type=type(exc).__name__,
            error_message=redact_text(str(exc)[:1000], extra_secrets=self._extra_secrets()),
        )
