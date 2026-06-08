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
import contextlib
import json
import time
from collections.abc import AsyncIterator
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

# How often the watch scheduler wakes to look for due re-audits. The interval an
# individual watch runs on is its own (much larger) interval_sec; this is just
# the polling tick.
_SCHEDULER_TICK_SEC = 30.0

# Built-in known-answer rerank probe: one document (index 2) is unmistakably the
# most relevant answer to the query. A genuine reranker must rank it first.
_RERANK_PROBE_QUERY = "What is the capital of France?"
_RERANK_PROBE_DOCS = [
    "Bananas are a good source of potassium.",
    "The Great Wall of China is very long.",
    "Paris is the capital of France.",
    "Photosynthesis happens in plants.",
]
_RERANK_PROBE_TOP = 2


def _coerce_int(value: Any) -> int:
    """Best-effort int from JSON input (str/float/None all tolerated). 0 on failure."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _kb_embedding_dimensions(model_id: str | None, provider_hint: str | None) -> int:
    """Native embedding dimension for ``model_id`` from the KB; 0 if unknown."""
    if not model_id:
        return 0
    try:
        from zing.knowledge import load_knowledge_base

        resolved = load_knowledge_base().resolve(model_id, provider_hint=provider_hint)
    except Exception:
        return 0
    if resolved is None:
        return 0
    return int(resolved.model.embedding_dimensions or 0)


async def _run_one_watch(row: dict[str, Any]) -> None:
    """Run a single due watch once: audit, persist, alert on regression/threshold.

    Always best-effort — any exception is swallowed by the caller so one bad
    watch can never kill the scheduler loop. The watch's run timestamp is
    recorded even on failure so a permanently broken target doesn't get retried
    every tick.
    """
    from zing.web import history, watches

    wid = int(row["id"])
    now = time.time()
    risk: str | None = None
    score: float | None = None
    report_id: int | None = None
    try:
        suite = validate_suite(str(row.get("suite") or "standard"))
        target = build_target(
            kind="target",
            name=row.get("name") or "watch",
            base_url=row.get("base_url"),
            api_key=row.get("api_key"),
            model=row.get("model"),
            claimed_model=row.get("claimed_model") or None,
            declared_provider=row.get("declared_provider") or None,
            api=validate_api(row.get("api")),
        )
        options = AuditOptions(suite=suite)

        # Previous saved run for this target+model — used for the regression check
        # and the "较上次" delta in the alert. notify.send needs the full report,
        # so find the most recent prior history row for this target and re-fetch it.
        claimed = target.claimed_model or target.model
        previous: dict[str, Any] | None = None
        for item in history.recent(50):  # newest first
            if (
                item.get("base_url") == target.base_url
                and item.get("claimed_model") == claimed
            ):
                previous = history.get(int(item["id"]))
                break

        report = await run_audit(target, options, baseline=None, mode="check")
        report_dict = json.loads(report.model_dump_json())
        report_id = history.save(report_dict)
        if report_id is not None and report_id < 0:
            report_id = None

        verdict = report_dict.get("verdict") or {}
        # report_dict came through model_dump_json, so risk_level is a plain str.
        raw_risk = verdict.get("risk_level")
        risk = raw_risk if isinstance(raw_risk, str) else None
        raw_score = verdict.get("overall_score")
        score = float(raw_score) if isinstance(raw_score, (int, float)) else None

        # Decide whether to alert: risk crossed the configured threshold, OR it
        # regressed versus the previous saved run.
        from zing.notify import regressed, send

        alert_on = str(row.get("alert_on") or "medium")
        crossed = _risk_meets(risk, alert_on)
        went_worse = regressed(report_dict, previous)
        if crossed or went_worse:
            for url in row.get("webhooks") or []:
                if not isinstance(url, str) or not url.strip():
                    continue
                with contextlib.suppress(Exception):
                    await send(url.strip(), report_dict, previous=previous)
    finally:
        # Record the run no matter what so cadence stays honest.
        with contextlib.suppress(Exception):
            watches.mark_run(wid, risk, score, report_id, now)


def _risk_meets(risk: str | None, threshold: str) -> bool:
    """True when ``risk`` is at or above the ``threshold`` risk level."""
    from zing.models import RiskLevel

    order = {
        RiskLevel.CLEAN.value: 0,
        RiskLevel.INCONCLUSIVE.value: 1,
        RiskLevel.LOW.value: 2,
        RiskLevel.MEDIUM.value: 3,
        RiskLevel.HIGH.value: 4,
    }
    if not risk:
        return False
    return order.get(risk, 0) >= order.get(threshold, order[RiskLevel.MEDIUM.value])


async def _scheduler_loop() -> None:
    """Poll for due watches every tick and run each one (best-effort, idle-quiet).

    If nothing is due, the loop does nothing and the server stays silent. Each
    watch is wrapped in its own try/except so a single failure never stops the
    loop. Cancellation (on server shutdown) propagates cleanly.
    """
    from zing.web import watches

    while True:
        try:
            due = watches.due(time.time())
        except Exception:
            due = []
        for row in due:
            try:
                await _run_one_watch(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad watch must never take down the whole loop.
                pass
        await asyncio.sleep(_SCHEDULER_TICK_SEC)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Spawn the watch scheduler on startup; cancel it cleanly on shutdown."""
    from zing.web import watches

    with contextlib.suppress(Exception):
        watches.init()
    task = asyncio.create_task(_scheduler_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def create_app() -> FastAPI:
    app = FastAPI(
        title="zing",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    @app.get("/api/health")
    async def health() -> Any:
        return {"ok": True, "version": __version__, "name": "zing"}

    @app.get("/")
    async def index() -> Any:
        return FileResponse(_STATIC / "index.html")

    @app.get("/console")
    async def console() -> Any:
        return FileResponse(_STATIC / "console.html")

    @app.get("/i18n.js")
    async def i18n_js() -> Any:
        return FileResponse(_STATIC / "i18n.js", media_type="application/javascript")

    @app.get("/icons.js")
    async def icons_js() -> Any:
        return FileResponse(_STATIC / "icons.js", media_type="application/javascript")

    @app.get("/modelpicker.js")
    async def modelpicker_js() -> Any:
        return FileResponse(
            _STATIC / "modelpicker.js", media_type="application/javascript"
        )

    @app.get("/api/kb")
    async def kb() -> Any:
        # Public model metadata only — no keys, no secrets. Mirrors the grouping
        # the CLI `kb --json` command uses: providers sorted, each with its models.
        from zing.knowledge import load_knowledge_base

        knowledge = load_knowledge_base()
        provs = sorted(knowledge.providers.values(), key=lambda p: p.provider)
        providers = [
            {
                "provider": prov.provider,
                "display_name": prov.display_name,
                "models": [{"id": m.id, "aliases": list(m.aliases)} for m in prov.models],
            }
            for prov in provs
        ]
        return JSONResponse({"providers": providers})

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
                    report_dict = json.loads(report.model_dump_json())
                    try:  # best-effort persist; never let history break the stream
                        from zing.web import history

                        history.save(report_dict)
                    except Exception:
                        pass
                    queue.put_nowait({"type": "report", "report": report_dict})
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

    @app.get("/history")
    async def history_page() -> Any:
        return FileResponse(_STATIC / "history.html")

    @app.get("/api/history")
    async def history_list(limit: int = 50) -> Any:
        from zing.web import history

        return JSONResponse(history.recent(limit))

    @app.get("/api/history/trend")
    async def history_trend(base_url: str, claimed_model: str, limit: int = 30) -> Any:
        from zing.web import history

        return JSONResponse(history.trend(base_url, claimed_model, limit))

    @app.get("/api/history/{rid}")
    async def history_get(rid: int) -> Any:
        from zing.web import history

        report = history.get(rid)
        if report is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(report)

    @app.delete("/api/history/{rid}")
    async def history_delete(rid: int) -> Any:
        from zing.web import history

        history.delete(rid)
        return JSONResponse({"ok": True})

    @app.delete("/api/history")
    async def history_clear() -> Any:
        from zing.web import history

        history.clear()
        return JSONResponse({"ok": True})

    # ----- Scheduled watches (monitoring) -------------------------------- #
    @app.get("/watches")
    async def watches_page() -> Any:
        return FileResponse(_STATIC / "watches.html")

    @app.get("/api/watches")
    async def watches_list() -> Any:
        from zing.web import watches

        # list_all() never returns api_key, so this is safe to send to the browser.
        return JSONResponse(watches.list_all())

    @app.post("/api/watches")
    async def watches_create(request: Request) -> Any:
        from zing.web import watches

        body = await request.json()
        # Validate the target + suite up front so bad input is a clean 400.
        try:
            suite = validate_suite(str(body.get("suite") or "standard"))
            build_target(
                kind="target",
                name=body.get("name") or "watch",
                base_url=body.get("base_url"),
                api_key=body.get("api_key"),
                model=body.get("model"),
                claimed_model=body.get("claimed_model") or None,
                declared_provider=body.get("declared_provider") or None,
                api=validate_api(body.get("api")),
            )
        except ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        cfg = {**body, "suite": suite}
        wid = watches.create(cfg)
        return JSONResponse({"ok": True, "id": wid}, status_code=201)

    @app.delete("/api/watches/{wid}")
    async def watches_delete(wid: int) -> Any:
        from zing.web import watches

        watches.delete(wid)
        return JSONResponse({"ok": True})

    @app.patch("/api/watches/{wid}")
    async def watches_patch(wid: int, request: Request) -> Any:
        from zing.web import watches

        if watches.get(wid) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await request.json()
        if "enabled" in body:
            watches.set_enabled(wid, bool(body.get("enabled")))
        return JSONResponse({"ok": True})

    @app.post("/api/watches/{wid}/run")
    async def watches_run(wid: int) -> Any:
        from zing.web import watches

        row = watches.get(wid)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        # Run the same path the scheduler uses (audit + persist + alert + mark).
        try:
            await _run_one_watch(row)
        except Exception as exc:  # surface a clean error, not a 500 stack
            return JSONResponse(
                {"error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )
        # Return the freshly saved report so the UI can show the result.
        from zing.web import history

        refreshed = watches.get(wid)
        report = None
        if refreshed and refreshed.get("last_report_id") is not None:
            report = history.get(int(refreshed["last_report_id"]))
        return JSONResponse({"ok": True, "report": report})

    # ----- Tools: embedding & rerank auditors (non-chat surface) ---------- #
    @app.get("/tools")
    async def tools_page() -> Any:
        return FileResponse(_STATIC / "tools.html")

    @app.post("/api/embed")
    async def embed_audit(request: Request) -> Any:
        # Lazy import: the embed auditor pulls in the client stack only on demand.
        from zing.embed_audit import audit_embeddings

        body = await request.json()
        try:
            target = build_target(
                kind="target",
                name=body.get("name") or "embed",
                base_url=body.get("base_url"),
                api_key=body.get("api_key"),
                model=body.get("model"),
                claimed_model=body.get("claimed_model") or None,
                declared_provider=body.get("declared_provider") or None,
                api=validate_api(body.get("api")),
            )
        except ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        # Resolve the expected output dimension: an explicit claimed value wins;
        # otherwise look the claimed (or requested) model up in the KB. 0 = unknown,
        # in which case the auditor records the observed dimension instead.
        claimed_dimensions = _coerce_int(body.get("claimed_dimensions"))
        if claimed_dimensions <= 0:
            claimed_dimensions = _kb_embedding_dimensions(
                target.claimed_model or target.model,
                target.declared_provider,
            )

        # The verdict is already redacted (api_key is fingerprinted in `target`).
        verdict = await audit_embeddings(target, claimed_dimensions)
        return JSONResponse(verdict)

    @app.post("/api/rerank")
    async def rerank_audit(request: Request) -> Any:
        from zing.embed_audit import audit_rerank

        body = await request.json()
        try:
            target = build_target(
                kind="target",
                name=body.get("name") or "rerank",
                base_url=body.get("base_url"),
                api_key=body.get("api_key"),
                model=body.get("model"),
                api=validate_api(body.get("api")),
            )
        except ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        # Use the caller's query/documents when given, else a built-in known-answer
        # probe where one document is unmistakably the most relevant.
        query = body.get("query")
        documents = body.get("documents")
        expected_top_index = body.get("expected_top_index")
        if not (isinstance(query, str) and query.strip()) or not (
            isinstance(documents, list) and len(documents) >= 2
        ):
            query = _RERANK_PROBE_QUERY
            documents = list(_RERANK_PROBE_DOCS)
            expected_top_index = _RERANK_PROBE_TOP
        expected_top_index = _coerce_int(expected_top_index)
        if not 0 <= expected_top_index < len(documents):
            expected_top_index = 0

        verdict = await audit_rerank(target, query, documents, expected_top_index)
        return JSONResponse(verdict)

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
