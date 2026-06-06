"""Shared HTTP machinery for relay clients.

Both the OpenAI-compatible and Anthropic-native clients speak raw HTTP (not an
official SDK) so the detectors can reason about the raw evidence — status codes,
headers, partial streams, per-chunk timing. The transport lifecycle, timeout,
secret redaction, and error shaping are identical across protocols and live here;
subclasses only supply the auth headers and the protocol-specific request/response
translation.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import httpx

from zing.models import CompletionOutcome, RequestSpec, TargetConfig
from zing.utils.redact import redact_json, redact_text


class BaseHTTPClient:
    """Connection lifecycle + redaction + error shaping shared by all clients."""

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
    async def __aenter__(self):
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

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            self.config.timeout_sec,
            connect=min(15.0, self.config.timeout_sec),
        )

    def _extra_secrets(self) -> list[str | None]:
        """Secrets to scrub from any relay-controlled text before it is stored."""
        return [self.config.api_key]

    # Subclasses provide protocol-specific auth headers and request handling.
    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    async def complete(self, spec: RequestSpec) -> CompletionOutcome:
        raise NotImplementedError

    async def list_models(self) -> tuple[CompletionOutcome, list[str]]:
        raise NotImplementedError

    # -- error shaping (shared) -------------------------------------------- #
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
