"""FastAPI app for `zing serve` — serves the SPA and streams live audits over SSE.

Endpoints:
  GET  /                     the single-page app
  GET  /api/health           {ok, version}
  POST /api/audit/stream     run an audit; stream detector progress + final report
                             as Server-Sent Events (text/event-stream)

The audit runs in-process with the same `run_audit` the CLI uses; a progress
callback pushes per-detector events into a queue the SSE generator drains. The
final report is the model's own redacted `model_dump` (API key fingerprinted,
relay text scrubbed), so nothing secret crosses to the browser.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

# fastapi is the optional [web] extra. This module is only imported when serving
# (CLI `serve`) or by the web tests, both of which handle a missing dependency.
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from zing import __version__
from zing.config import (
    AuditOptions,
    ConfigError,
    build_target,
    validate_api,
    validate_suite,
)
from zing.runner import run_audit

_STATIC = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="zing", version=__version__, docs_url=None, redoc_url=None)

    @app.get("/api/health")
    async def health() -> Any:
        return {"ok": True, "version": __version__, "name": "zing"}

    @app.get("/")
    async def index() -> Any:
        return FileResponse(_STATIC / "index.html")

    @app.post("/api/audit/stream")
    async def audit_stream(request: Request) -> Any:
        body = await request.json()

        def sse(event: dict[str, Any]) -> str:
            # default=str so any unexpected evidence value can't break the stream.
            return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

        bl = body.get("baseline") or {}
        has_baseline = bool(bl.get("base_url") and bl.get("model"))

        # Validate up front so bad input fails as a clean error event, not a 500.
        try:
            suite = validate_suite(str(body.get("suite") or "standard"))
            target = build_target(
                kind="target",
                name=body.get("name") or "target",
                base_url=body.get("base_url"),
                api_key=body.get("api_key"),
                model=body.get("model"),
                claimed_model=body.get("claimed_model") or None,
                declared_provider=body.get("declared_provider") or None,
                api=validate_api(body.get("api")),
            )
            baseline = None
            if has_baseline:
                baseline = build_target(
                    kind="baseline",
                    name=bl.get("name") or "baseline",
                    base_url=bl.get("base_url"),
                    api_key=bl.get("api_key"),
                    model=bl.get("model"),
                    api=validate_api(bl.get("api")),
                )
            options = AuditOptions(suite=suite)
        except ConfigError as exc:
            msg = str(exc)  # bind now: `exc` is cleared when the except block exits

            async def err_stream():
                yield sse({"type": "error", "message": msg})
                yield sse({"type": "done"})

            return StreamingResponse(err_stream(), media_type="text/event-stream")

        async def event_stream():
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            async def run() -> None:
                try:
                    report = await run_audit(
                        target,
                        options,
                        baseline=baseline,
                        mode="compare" if baseline is not None else "check",
                        on_event=queue.put_nowait,
                    )
                    queue.put_nowait(
                        {"type": "report", "report": json.loads(report.model_dump_json())}
                    )
                except Exception as exc:  # surface any audit failure to the client
                    queue.put_nowait({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
                finally:
                    queue.put_nowait({"type": "done"})

            task = asyncio.create_task(run())
            try:
                while True:
                    event = await queue.get()
                    yield sse(event)
                    if event.get("type") == "done":
                        break
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Static assets (e.g. future JS/CSS split-outs) under /assets.
    assets = _STATIC / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    # 404 fallback to the SPA shell so deep links work.
    @app.exception_handler(404)
    async def spa_fallback(request: Request, exc: Any) -> Any:  # noqa: ARG001
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(_STATIC / "index.html")

    return app
