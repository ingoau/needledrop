"""SQLite persistence for jobs, inbox items and the activity log."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    kind TEXT NOT NULL,            -- song | album | playlist
    apple_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    subtitle TEXT,
    artwork TEXT,
    expected_tracks INTEGER,
    done_tracks INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,          -- queued | downloading | finishing | done | failed | cancelled
    error TEXT,
    output TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS inbox (
    path TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_change REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    analyzed_fingerprint TEXT,
    status TEXT NOT NULL,          -- waiting | queued | tagging | review | duplicate | error | applied
    artist TEXT,
    album TEXT,
    track_count INTEGER,
    report TEXT,
    error TEXT,
    job_id TEXT,
    singleton INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,           -- info | success | warning | error
    kind TEXT NOT NULL,            -- download | inbox | tagger | system
    message TEXT NOT NULL,
    ref TEXT,
    details TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(
            settings.db_path, check_same_thread=False, isolation_level=None
        )
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
    return _conn


def execute(sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
    with _lock:
        return conn().execute(sql, params)


def query(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with _lock:
        return [dict(row) for row in conn().execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def update(table: str, key: str, key_value: Any, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{name} = :{name}" for name in fields)
    execute(
        f"UPDATE {table} SET {assignments} WHERE {key} = :_key",
        {**fields, "_key": key_value},
    )


def log(level: str, kind: str, message: str, ref: str | None = None,
        details: Any = None) -> None:
    execute(
        "INSERT INTO events (ts, level, kind, message, ref, details) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            level,
            kind,
            message,
            ref,
            json.dumps(details, default=str) if details is not None else None,
        ),
    )


def prune_events() -> None:
    cutoff = time.time() - settings.log_retention_days * 86400
    execute("DELETE FROM events WHERE ts < ?", (cutoff,))
