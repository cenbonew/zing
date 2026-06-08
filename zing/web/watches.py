"""Scheduled watch store — saved re-audit targets for the web monitor, stdlib only.

`zing serve` can keep a list of *watches*: a target plus a cadence (interval),
suite, an alert threshold, and a set of webhooks. A background scheduler in the
web server runs each due watch on its interval and POSTs an alert when the risk
crosses the threshold or regresses versus the previous run. This module is the
persistence layer for those watch definitions — a tiny SQLite store, no new
dependency beyond ``sqlite3``.

Storage lives alongside the audit history in ``$ZING_DATA_DIR`` (default
``~/.zing``) as ``watches.db``. Like :mod:`zing.web.history`, every function
opens a fresh short-lived connection so the module is safe to call from
FastAPI's threadpool, lazily creates its table, and is best-effort: a malformed
row must never crash the scheduler loop.

Secret handling: the stored ``api_key`` is what the scheduler needs to actually
run the audit, so it is kept in the DB. But :func:`list_all` (the listing the
browser sees) NEVER returns it — only :func:`get` and :func:`due`, used
server-side by the scheduler, expose the key.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Full column set, in table order. ``api_key`` lives here for the scheduler, but
# is filtered out of the public listing (see _LIST_COLS).
_ALL_COLS = (
    "id",
    "name",
    "base_url",
    "api_key",
    "model",
    "claimed_model",
    "api",
    "declared_provider",
    "suite",
    "interval_sec",
    "alert_on",
    "webhooks",
    "enabled",
    "created_ts",
    "last_run_ts",
    "last_risk",
    "last_score",
    "last_report_id",
)

# Columns safe to return to the browser — everything except the API key.
_LIST_COLS = tuple(c for c in _ALL_COLS if c != "api_key")


def _data_dir() -> Path:
    return Path(os.environ.get("ZING_DATA_DIR") or (Path.home() / ".zing"))


def _db_path() -> Path:
    return _data_dir() / "watches.db"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a fresh connection with rows as dicts; commit + close on exit."""
    _data_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path()), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_table(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Lazily create the watches table. Idempotent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watches (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            name              TEXT,
            base_url          TEXT,
            api_key           TEXT,
            model             TEXT,
            claimed_model     TEXT,
            api               TEXT,
            declared_provider TEXT,
            suite             TEXT,
            interval_sec      INTEGER,
            alert_on          TEXT,
            webhooks          TEXT,
            enabled           INTEGER DEFAULT 1,
            created_ts        REAL,
            last_run_ts       REAL,
            last_risk         TEXT,
            last_score        REAL,
            last_report_id    INTEGER
        )
        """
    )


def init() -> None:
    """Create the table if it doesn't exist. Idempotent; safe to call often."""
    with _connect():
        pass


def _row_to_dict(row: sqlite3.Row, *, include_key: bool) -> dict[str, Any]:
    """Convert a row to a plain dict, decoding webhooks JSON and the enabled flag.

    When ``include_key`` is False the ``api_key`` column is omitted entirely so it
    can never leak to a caller that only asked for a listing.
    """
    d = dict(row)
    if not include_key:
        d.pop("api_key", None)
    # webhooks is stored as a JSON list; decode defensively.
    raw = d.get("webhooks")
    try:
        d["webhooks"] = json.loads(raw) if isinstance(raw, str) and raw else []
    except (ValueError, TypeError):
        d["webhooks"] = []
    if not isinstance(d["webhooks"], list):
        d["webhooks"] = []
    d["enabled"] = bool(d.get("enabled"))
    return d


def create(cfg: dict[str, Any]) -> int:
    """Insert a new watch from a config dict; return its new id.

    Expected keys: name, base_url, api_key, model, claimed_model, api,
    declared_provider, suite, interval_sec, alert_on, webhooks (list). Unknown
    keys are ignored; missing keys fall back to sensible defaults.
    """
    cfg = cfg or {}
    webhooks = cfg.get("webhooks") or []
    if not isinstance(webhooks, list):
        webhooks = [webhooks]
    webhooks = [str(w).strip() for w in webhooks if str(w).strip()]
    try:
        interval = max(30, int(cfg.get("interval_sec") or 3600))
    except (TypeError, ValueError):
        interval = 3600
    row = (
        cfg.get("name") or "watch",
        cfg.get("base_url"),
        cfg.get("api_key") or "",
        cfg.get("model"),
        cfg.get("claimed_model") or None,
        cfg.get("api") or "auto",
        cfg.get("declared_provider") or None,
        cfg.get("suite") or "standard",
        interval,
        cfg.get("alert_on") or "medium",
        json.dumps(webhooks, ensure_ascii=False),
        1 if cfg.get("enabled", True) else 0,
        time.time(),
    )
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO watches
               (name, base_url, api_key, model, claimed_model, api,
                declared_provider, suite, interval_sec, alert_on, webhooks,
                enabled, created_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )
        return int(cur.lastrowid or -1)


def list_all() -> list[dict[str, Any]]:
    """All watches, newest first, WITHOUT api_key (safe for the browser)."""
    cols = ", ".join(_LIST_COLS)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {cols} FROM watches ORDER BY id DESC"
        ).fetchall()
    return [_row_to_dict(r, include_key=False) for r in rows]


def get(wid: int) -> dict[str, Any] | None:
    """One full watch row INCLUDING api_key, or ``None`` if missing.

    Server-side use only (the scheduler / run-now). Never hand this to a client
    response without stripping the key first.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM watches WHERE id = ?", (int(wid),)
        ).fetchone()
    return _row_to_dict(row, include_key=True) if row else None


def set_enabled(wid: int, enabled: bool) -> None:
    """Enable or disable a watch. No-op if it doesn't exist."""
    with _connect() as conn:
        conn.execute(
            "UPDATE watches SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, int(wid)),
        )


def delete(wid: int) -> None:
    """Remove one watch by id. No-op if it doesn't exist."""
    with _connect() as conn:
        conn.execute("DELETE FROM watches WHERE id = ?", (int(wid),))


def mark_run(
    wid: int,
    risk: str | None,
    score: float | None,
    report_id: int | None,
    ts: float,
) -> None:
    """Record the outcome of a run: last risk/score/report id and run timestamp."""
    with _connect() as conn:
        conn.execute(
            """UPDATE watches
               SET last_run_ts = ?, last_risk = ?, last_score = ?, last_report_id = ?
               WHERE id = ?""",
            (
                float(ts),
                risk,
                float(score) if isinstance(score, (int, float)) else None,
                int(report_id) if report_id is not None else None,
                int(wid),
            ),
        )


def due(now_ts: float) -> list[dict[str, Any]]:
    """Enabled watches whose interval has elapsed — full rows incl. api_key.

    A watch is due when it has never run, or when ``now - last_run >= interval``.
    Used by the scheduler, so the api_key is included to actually run the audit.
    """
    out: list[dict[str, Any]] = []
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM watches WHERE enabled = 1").fetchall()
    for r in rows:
        d = _row_to_dict(r, include_key=True)
        last = d.get("last_run_ts")
        interval = d.get("interval_sec") or 0
        if last is None or (now_ts - float(last)) >= float(interval):
            out.append(d)
    return out
