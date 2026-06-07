"""Local audit history + trends — a tiny SQLite store, stdlib only.

`zing serve` persists every finished AuditReport here so the web app can show a
history table and per-target score trends. No new dependency: just `sqlite3`.

Storage lives in ``$ZING_DATA_DIR`` (default ``~/.zing``) as ``history.db``.
Every public function opens a fresh, short-lived connection so the module is
safe to call from FastAPI's threadpool without sharing a connection across
threads. Writes are best-effort: a malformed report must never break the audit
stream, so :func:`save` swallows its own errors and returns ``-1`` on failure.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Columns returned by the list view (everything except the heavy report_json).
_SUMMARY_COLS = (
    "id",
    "ts",
    "base_url",
    "claimed_model",
    "model",
    "mode",
    "suite",
    "risk_level",
    "score",
    "rating",
)


def _data_dir() -> Path:
    return Path(os.environ.get("ZING_DATA_DIR") or (Path.home() / ".zing"))


def _db_path() -> Path:
    return _data_dir() / "history.db"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a fresh connection with rows as dicts; commit + close on exit.

    A short busy_timeout lets concurrent local writers retry instead of raising
    ``database is locked`` — plenty for a single-user local server.
    """
    _data_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path()), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    """Create the table if it doesn't exist. Idempotent; safe to call often."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT,
                base_url      TEXT,
                claimed_model TEXT,
                model         TEXT,
                mode          TEXT,
                suite         TEXT,
                risk_level    TEXT,
                score         REAL,
                rating        TEXT,
                report_json   TEXT
            )
            """
        )
        # Speeds up trend() lookups for a given target+model.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_target "
            "ON history (base_url, claimed_model, id)"
        )


def save(report: dict[str, Any]) -> int:
    """Persist one AuditReport dict; return the new row id (``-1`` on failure).

    Best-effort by contract: this is called from inside the SSE stream, so any
    extraction or DB error is swallowed rather than allowed to abort the audit.
    """
    try:
        report = report or {}
        target = report.get("target") or {}
        verdict = report.get("verdict") or {}
        score = verdict.get("overall_score")
        row = (
            report.get("generated_at"),
            target.get("base_url"),
            # Fall back to the concrete model id when no claim was made, so the
            # trend grouping key is always populated.
            target.get("claimed_model") or target.get("model"),
            target.get("model"),
            report.get("mode"),
            report.get("suite"),
            verdict.get("risk_level"),
            float(score) if isinstance(score, (int, float)) else None,
            verdict.get("rating"),
            json.dumps(report, ensure_ascii=False, default=str),
        )
        with _connect() as conn:
            init_done = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='history'"
            ).fetchone()
            if not init_done:
                # Lazy create so callers don't have to remember init().
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, base_url TEXT,
                        claimed_model TEXT, model TEXT, mode TEXT, suite TEXT,
                        risk_level TEXT, score REAL, rating TEXT, report_json TEXT)"""
                )
            cur = conn.execute(
                """INSERT INTO history
                   (ts, base_url, claimed_model, model, mode, suite,
                    risk_level, score, rating, report_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            return int(cur.lastrowid or -1)
    except Exception:
        return -1


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent audits, newest first — summary columns only (no report)."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    cols = ", ".join(_SUMMARY_COLS)
    with _connect() as conn:
        if not _has_table(conn):
            return []
        rows = conn.execute(
            f"SELECT {cols} FROM history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get(rid: int) -> dict[str, Any] | None:
    """The full saved report for one row, or ``None`` if missing/corrupt."""
    with _connect() as conn:
        if not _has_table(conn):
            return None
        row = conn.execute(
            "SELECT report_json FROM history WHERE id = ?", (int(rid),)
        ).fetchone()
    if not row or row["report_json"] is None:
        return None
    try:
        return json.loads(row["report_json"])
    except (ValueError, TypeError):
        return None


def trend(
    base_url: str, claimed_model: str, limit: int = 30
) -> list[dict[str, Any]]:
    """Score history for one target+model, oldest→newest, for a sparkline."""
    try:
        limit = max(1, min(int(limit), 365))
    except (TypeError, ValueError):
        limit = 30
    with _connect() as conn:
        if not _has_table(conn):
            return []
        # Take the newest `limit`, then flip to chronological order.
        rows = conn.execute(
            """SELECT ts, score, risk_level FROM history
               WHERE base_url = ? AND claimed_model = ?
               ORDER BY id DESC LIMIT ?""",
            (base_url, claimed_model, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def delete(rid: int) -> None:
    """Remove one row by id. No-op if it doesn't exist."""
    with _connect() as conn:
        if not _has_table(conn):
            return
        conn.execute("DELETE FROM history WHERE id = ?", (int(rid),))


def clear() -> None:
    """Wipe all history."""
    with _connect() as conn:
        if not _has_table(conn):
            return
        conn.execute("DELETE FROM history")


def _has_table(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='history'"
        ).fetchone()
        is not None
    )
