"""Download history: which Apple Music tracks have already been ripped.

A track in the history is skipped by later jobs unless the job is forced.
Existing histories can be imported from:

- apple-music-to-flac's `download-history.sqlite3` (a `tracks` table)
- gamdl's own `--database-path` file (a `media` table of id -> path)

Usable from the web UI, or on the command line:

    python -m app.history import /path/to/history.sqlite3
"""

from __future__ import annotations

import sqlite3
import sys
import time
from typing import Any

from . import db

COLUMNS = ("media_id", "isrc", "album_id", "title", "artist", "album",
           "codec", "path", "completed_at", "source")


def record(event: dict[str, Any], job_id: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO history (media_id, isrc, album_id, title, artist, "
        "album, codec, path, completed_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.get("media_id"), event.get("isrc"), event.get("album_id"),
            event.get("title"), event.get("artist"), event.get("album"),
            event.get("codec"), event.get("path"), event.get("ts") or time.time(),
            f"job:{job_id}",
        ),
    )


def stats() -> dict[str, Any]:
    row = db.query_one(
        "SELECT COUNT(*) AS tracks, COUNT(DISTINCT album_id) AS albums, "
        "MAX(completed_at) AS latest FROM history"
    ) or {}
    sources = db.query(
        "SELECT CASE WHEN source LIKE 'job:%' THEN 'needledrop' ELSE source END "
        "AS source, COUNT(*) AS n FROM history GROUP BY 1 ORDER BY n DESC"
    )
    return {**row, "sources": sources}


def annotate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark catalog items that are (partly) in the history."""
    song_ids = [i["id"] for i in items if i.get("type") == "song" and i.get("id")]
    album_ids = [i["id"] for i in items if i.get("type") == "album" and i.get("id")]
    have: set[str] = set()
    if song_ids:
        marks = ",".join("?" for _ in song_ids)
        have = {r["media_id"] for r in db.query(
            f"SELECT media_id FROM history WHERE media_id IN ({marks})", tuple(song_ids))}
    counts: dict[str, int] = {}
    if album_ids:
        marks = ",".join("?" for _ in album_ids)
        counts = {r["album_id"]: r["n"] for r in db.query(
            f"SELECT album_id, COUNT(*) AS n FROM history WHERE album_id IN ({marks}) "
            "GROUP BY album_id", tuple(album_ids))}
    for item in items:
        if item.get("type") == "song":
            item["in_history"] = item.get("id") in have
        elif item.get("type") == "album":
            item["history_tracks"] = counts.get(item.get("id"), 0)
    return items


def forget(media_ids: list[str] | None = None, album_id: str | None = None) -> int:
    count = 0
    for media_id in media_ids or []:
        count += db.execute(
            "DELETE FROM history WHERE media_id = ?", (media_id,)
        ).rowcount
    if album_id:
        count += db.execute(
            "DELETE FROM history WHERE album_id = ?", (album_id,)
        ).rowcount
    return count


def import_file(path: str) -> dict[str, Any]:
    """Merge another history database into ours. Returns what was imported."""
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {
            row[0] for row in src.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Not a SQLite database: {exc}") from exc

    if "tracks" in tables:
        rows, kind = _rows_from_rip_history(src), "apple-music-to-flac"
    elif "media" in tables:
        rows, kind = _rows_from_gamdl(src), "gamdl"
    else:
        src.close()
        raise ValueError(
            "Unrecognised history: expected a 'tracks' (apple-music-to-flac) "
            "or 'media' (gamdl) table"
        )

    added = skipped = 0
    for row in rows:
        row["source"] = f"import:{kind}"
        cursor = db.execute(
            f"INSERT OR IGNORE INTO history ({', '.join(COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in COLUMNS)})",
            tuple(row.get(c) for c in COLUMNS),
        )
        if cursor.rowcount:
            added += 1
        else:
            skipped += 1
    src.close()
    db.log("info", "system",
           f"Imported download history from {kind}: {added} new, "
           f"{skipped} already known")
    return {"format": kind, "added": added, "already_known": skipped}


def _columns(src: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in src.execute(f"PRAGMA table_info({table})")}


def _rows_from_rip_history(src: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = _columns(src, "tracks")

    def col(name: str, fallback: str = "NULL") -> str:
        return name if name in cols else fallback

    where = "WHERE audio_status = 'complete'" if "audio_status" in cols else ""
    completed = (
        "last_completed_at" if "last_completed_at" in cols
        else col("first_completed_at")
    )
    query = (
        f"SELECT media_id, {col('isrc')}, {col('album_id')}, {col('title')}, "
        f"{col('artist')}, {col('album')}, {col('codec')}, {col('output_path')}, "
        f"{completed} FROM tracks {where}"
    )
    rows = []
    for r in src.execute(query):
        rows.append(dict(zip(COLUMNS[:9], r)))
        rows[-1]["completed_at"] = _timestamp(rows[-1]["completed_at"])
    return rows


def _rows_from_gamdl(src: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {"media_id": media_id, "path": path, "completed_at": time.time()}
        for media_id, path in src.execute("SELECT id, path FROM media")
    ]


def _timestamp(value: Any) -> float | None:
    if value is None or isinstance(value, (int, float)):
        return value
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "import":
        sys.exit("usage: python -m app.history import FILE")
    print(import_file(sys.argv[2]))
