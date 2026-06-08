"""An OpenAI-compatible HTTP client purpose-built for auditing.

It deliberately speaks raw HTTP instead of using the official SDK: relays diverge
in subtle ways and the raw evidence (status codes, headers, partial streams,
per-chunk timing) is exactly what the detectors reason about.

Shared transport/lifecycle/redaction lives in :class:`BaseHTTPClient`; this class
adds the OpenAI Chat Completions request/response and SSE handling.
"""

from __future__ import annotations

import base64
import contextlib
import json
import time
from typing import Any

from zing.clients.base import BaseHTTPClient
from zing.models import CompletionOutcome, RequestSpec
from zing.utils.redact import redact_headers, redact_text
from zing.utils.sse import (
    extract_content_delta,
    extract_tool_calls_delta,
    parse_sse_line,
    try_load_event,
)


class OpenAICompatibleClient(BaseHTTPClient):
    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"

    @property
    def embeddings_url(self) -> str:
        return f"{self.base_url}/embeddings"

    @property
    def rerank_url(self) -> str:
        return f"{self.base_url}/rerank"

    @property
    def images_url(self) -> str:
        return f"{self.base_url}/images/generations"

    @property
    def audio_speech_url(self) -> str:
        return f"{self.base_url}/audio/speech"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "zing-audit/0.1",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.headers)
        return headers

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

    async def embeddings(
        self, inputs: list[str]
    ) -> tuple[CompletionOutcome, list[list[float]]]:
        """POST /embeddings. Returns (outcome, list-of-vectors).

        Each vector is a list of floats. On any error the outcome carries the
        redacted failure and the vector list is empty. This is a non-chat surface,
        so it never routes through the chat detector pipeline.
        """
        body: dict[str, Any] = {"model": self.config.model, "input": inputs}
        started = time.perf_counter()
        try:
            async with self._session() as client:
                response = await client.post(self.embeddings_url, json=body)
                duration_ms = (time.perf_counter() - started) * 1000
                headers = redact_headers(dict(response.headers), extra_secrets=self._extra_secrets())
                if response.status_code >= 400:
                    return self._error_from_response(response, duration_ms, headers), []

                data = response.json() if response.content else {}
                vectors: list[list[float]] = []
                if isinstance(data, dict):
                    rows = data.get("data") or []
                    if isinstance(rows, list):
                        # OpenAI returns rows out of order under load; honor `index`.
                        ordered = sorted(
                            (r for r in rows if isinstance(r, dict)),
                            key=lambda r: r.get("index", 0),
                        )
                        for row in ordered:
                            emb = row.get("embedding")
                            if isinstance(emb, list):
                                vectors.append([float(x) for x in emb])
                outcome = CompletionOutcome(
                    ok=True,
                    status_code=response.status_code,
                    usage=data.get("usage") if isinstance(data, dict) else None,
                    duration_ms=duration_ms,
                    headers=headers,
                    model_returned=data.get("model") if isinstance(data, dict) else None,
                )
                return outcome, vectors
        except Exception as exc:
            return self._exception_outcome(exc, started), []

    async def rerank(
        self, query: str, documents: list[str]
    ) -> tuple[CompletionOutcome, list[dict[str, Any]]]:
        """POST /rerank. Returns (outcome, results) sorted by score descending.

        Each result is ``{"index": int, "relevance_score": float}`` where ``index``
        points into the original ``documents`` list. Non-chat surface — no detector
        pipeline.
        """
        body: dict[str, Any] = {
            "model": self.config.model,
            "query": query,
            "documents": documents,
        }
        started = time.perf_counter()
        try:
            async with self._session() as client:
                response = await client.post(self.rerank_url, json=body)
                duration_ms = (time.perf_counter() - started) * 1000
                headers = redact_headers(dict(response.headers), extra_secrets=self._extra_secrets())
                if response.status_code >= 400:
                    return self._error_from_response(response, duration_ms, headers), []

                data = response.json() if response.content else {}
                results: list[dict[str, Any]] = []
                raw = data.get("results") if isinstance(data, dict) else None
                if isinstance(raw, list):
                    for row in raw:
                        if not isinstance(row, dict):
                            continue
                        idx = row.get("index")
                        score = row.get("relevance_score")
                        if score is None:
                            score = row.get("score")
                        if isinstance(idx, int) and score is not None:
                            results.append(
                                {"index": idx, "relevance_score": float(score)}
                            )
                results.sort(key=lambda r: r["relevance_score"], reverse=True)
                outcome = CompletionOutcome(
                    ok=True,
                    status_code=response.status_code,
                    usage=data.get("usage") if isinstance(data, dict) else None,
                    duration_ms=duration_ms,
                    headers=headers,
                    model_returned=data.get("model") if isinstance(data, dict) else None,
                )
                return outcome, results
        except Exception as exc:
            return self._exception_outcome(exc, started), []

    async def images_generate(
        self,
        prompt: str,
        size: str,
        n: int = 1,
        response_format: str = "b64_json",
    ) -> tuple[CompletionOutcome, list[bytes]]:
        """POST /images/generations. Returns (outcome, list-of-image-bytes).

        Decodes each ``data[i].b64_json`` to raw bytes; if a row carries only a
        ``url`` it GETs that url (over the same session) to fetch the bytes. On any
        error the outcome carries the redacted failure and the byte list is empty.
        Non-chat surface — never routes through the chat detector pipeline.
        """
        body: dict[str, Any] = {
            "model": self.config.model,
            "prompt": prompt,
            "size": size,
            "n": n,
            "response_format": response_format,
        }
        started = time.perf_counter()
        try:
            async with self._session() as client:
                response = await client.post(self.images_url, json=body)
                duration_ms = (time.perf_counter() - started) * 1000
                headers = redact_headers(dict(response.headers), extra_secrets=self._extra_secrets())
                if response.status_code >= 400:
                    return self._error_from_response(response, duration_ms, headers), []

                data = response.json() if response.content else {}
                images: list[bytes] = []
                rows = data.get("data") if isinstance(data, dict) else None
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        b64 = row.get("b64_json")
                        if isinstance(b64, str) and b64:
                            with contextlib.suppress(ValueError, TypeError):
                                images.append(base64.b64decode(b64))
                            continue
                        url = row.get("url")
                        if isinstance(url, str) and url:
                            img_resp = await client.get(url)
                            if img_resp.status_code < 400 and img_resp.content:
                                images.append(img_resp.content)
                outcome = CompletionOutcome(
                    ok=True,
                    status_code=response.status_code,
                    usage=data.get("usage") if isinstance(data, dict) else None,
                    duration_ms=duration_ms,
                    headers=headers,
                    model_returned=data.get("model") if isinstance(data, dict) else None,
                )
                return outcome, images
        except Exception as exc:
            return self._exception_outcome(exc, started), []

    async def audio_speech(
        self, text: str, voice: str, response_format: str = "wav"
    ) -> tuple[CompletionOutcome, bytes]:
        """POST /audio/speech. Returns (outcome, raw-audio-bytes).

        The response body is the raw audio file (``response.content``); there is no
        JSON envelope. On any error the outcome carries the redacted failure and the
        byte string is empty. Non-chat surface — no detector pipeline.
        """
        body: dict[str, Any] = {
            "model": self.config.model,
            "input": text,
            "voice": voice,
            "response_format": response_format,
        }
        started = time.perf_counter()
        try:
            async with self._session() as client:
                response = await client.post(self.audio_speech_url, json=body)
                duration_ms = (time.perf_counter() - started) * 1000
                headers = redact_headers(dict(response.headers), extra_secrets=self._extra_secrets())
                if response.status_code >= 400:
                    return self._error_from_response(response, duration_ms, headers), b""

                audio = response.content or b""
                outcome = CompletionOutcome(
                    ok=True,
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                    headers=headers,
                    model_returned=response.headers.get("x-model"),
                )
                return outcome, audio
        except Exception as exc:
            return self._exception_outcome(exc, started), b""

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
