"""Needledrop web app: search Apple Music, rip it, and triage the inbox."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import beets_setup, db, history
from .apple import catalog
from .config import settings
from .downloads import downloads
from .inbox import inbox

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.conn()
    for folder in (settings.inbox_dir, settings.downloading_dir):
        folder.mkdir(parents=True, exist_ok=True)
    beets_setup.prepare()
    db.log("info", "system", "Needledrop started")
    tasks = [asyncio.create_task(downloads.run()), asyncio.create_task(inbox.run())]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Needledrop", lifespan=lifespan)


# ------------------------------------------------------------------ catalog


@app.get("/api/status")
async def status() -> dict[str, Any]:
    try:
        account = await catalog.status()
        account["ok"] = True
    except Exception as exc:
        account = {"ok": False, "error": str(exc)}
    counts = {
        row["status"]: row["n"]
        for row in db.query("SELECT status, COUNT(*) AS n FROM inbox GROUP BY status")
    }
    return {
        "account": account,
        "auto_apply": settings.auto_apply,
        "buffer_seconds": settings.tagger_buffer_seconds,
        "log_retention_days": settings.log_retention_days,
        "inbox_dir": str(settings.inbox_dir),
        "downloading": downloads.current,
        "tagging": inbox.current,
        "tag_queue": inbox.queue.qsize(),
        "inbox_counts": counts,
    }


@app.get("/api/search")
async def search(q: str, types: str = "songs,albums,artists,playlists"):
    q = q.strip()
    if not q:
        return {"results": {}}
    try:
        if "music.apple.com" in q:
            item = await catalog.resolve(q)
            results = {item["type"]: [item]} if item else {}
        else:
            results = await catalog.search(q, types)
        for items in results.values():
            history.annotate(items)
        return {"results": results}
    except Exception as exc:
        raise HTTPException(502, f"Apple Music search failed: {exc}")


@app.get("/api/artist/{artist_id}")
async def artist(artist_id: str):
    data = await _catalog_call(catalog.artist, artist_id)
    for section in data["sections"]:
        history.annotate(section["items"])
    return data


@app.get("/api/album/{album_id}")
async def album(album_id: str):
    data = await _catalog_call(catalog.album, album_id)
    history.annotate([data["album"]])
    history.annotate(data["tracks"])
    return data


@app.get("/api/playlist/{playlist_id}")
async def playlist(playlist_id: str):
    data = await _catalog_call(catalog.playlist, playlist_id)
    history.annotate(data["tracks"])
    return data


async def _catalog_call(func, ident: str):
    try:
        return await func(ident)
    except Exception as exc:
        raise HTTPException(502, f"Apple Music lookup failed: {exc}")


# ------------------------------------------------------------------ jobs


class JobItem(BaseModel):
    type: str
    id: str
    url: str
    name: str | None = None
    artist: str | None = None
    album: str | None = None
    album_id: str | None = None
    artwork: str | None = None
    track_count: int | None = None


class JobRequest(BaseModel):
    items: list[JobItem]
    playlist_albums: bool = True
    force: bool = False


@app.post("/api/jobs")
async def create_jobs(request: JobRequest):
    created = []
    for item in request.items:
        if item.type not in ("song", "album", "playlist"):
            raise HTTPException(400, f"Can't download a {item.type}")
        expected = item.track_count
        options: dict[str, Any] = {
            "force": request.force,
            "playlist_albums": request.playlist_albums,
        }
        if item.type == "song":
            expected = 1
            # A song from a one-track single tags fine as an album; one taken
            # out of a bigger album goes in as a singleton.
            album_tracks = None
            if item.album_id:
                try:
                    album_tracks = (await catalog.album(item.album_id))["album"]["track_count"]
                except Exception:
                    pass
            if album_tracks is None:
                album_tracks = 1 if re.search(r"\s-\s(Single|EP)$", item.album or "") else 2
            options["album_track_count"] = album_tracks
            options["album_id"] = item.album_id
        elif item.type == "album":
            try:
                detail = await catalog.album(item.id)
                expected = sum(
                    1 for t in detail["tracks"]
                    if t["type"] == "song" and t["streamable"]
                ) or expected
            except Exception:
                pass
        elif item.type == "playlist" and request.playlist_albums:
            expected = None
        created.append(downloads.create(
            kind=item.type,
            apple_id=item.id,
            url=item.url,
            title=item.name,
            subtitle=item.artist,
            artwork=item.artwork,
            expected_tracks=expected,
            options=options,
        ))
    return {"created": created}


@app.get("/api/jobs")
async def list_jobs():
    jobs = db.query("SELECT * FROM jobs ORDER BY created DESC LIMIT 100")
    for job in jobs:
        meta = json.loads(job.pop("output") or "{}")
        job["options"] = meta.get("options", {})
        job["summary"] = meta.get("summary")
        job["tail"] = meta.get("tail") or []
        if job["id"] == downloads.current:
            job["progress"] = {
                k: v for k, v in downloads.progress.items() if k != "summary"
            }
    return {"jobs": jobs}


class JobAction(BaseModel):
    force: bool = False


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str, body: JobAction):
    _job_or_404(job_id)
    downloads.retry(job_id, force=body.force)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    _job_or_404(job_id)
    await downloads.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/release")
async def release_job(job_id: str):
    _job_or_404(job_id)
    await downloads.release(job_id)
    inbox.poke()
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    _job_or_404(job_id)
    await downloads.discard(job_id)
    return {"ok": True}


@app.post("/api/jobs/clear")
async def clear_jobs():
    db.execute(
        "DELETE FROM jobs WHERE status IN ('done', 'skipped', 'cancelled')"
    )
    return {"ok": True}


def _job_or_404(job_id: str) -> dict[str, Any]:
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        raise HTTPException(404, "No such job")
    return job


# ------------------------------------------------------------------ history


@app.get("/api/history")
async def history_stats():
    return history.stats()


@app.post("/api/history/import")
async def history_import(request: Request):
    """Body: the raw bytes of a history SQLite file."""
    body = await request.body()
    if not body:
        raise HTTPException(400, "Upload a history database file")
    upload = settings.data_dir / f"import-{uuid.uuid4().hex}.sqlite3"
    upload.write_bytes(body)
    try:
        return await asyncio.to_thread(history.import_file, str(upload))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        upload.unlink(missing_ok=True)


class ForgetRequest(BaseModel):
    media_ids: list[str] = []
    album_id: str | None = None


@app.post("/api/history/forget")
async def history_forget(body: ForgetRequest):
    removed = history.forget(body.media_ids, body.album_id)
    db.log("info", "system", f"Removed {removed} track(s) from the download history")
    return {"removed": removed}


# ------------------------------------------------------------------ inbox


@app.get("/api/inbox")
async def list_inbox():
    rows = db.query("SELECT * FROM inbox")
    root = str(settings.inbox_dir).rstrip("/") + "/"
    for row in rows:
        report = json.loads(row.pop("report") or "null") or {}
        row["relpath"] = row["path"][len(root):] if row["path"].startswith(root) else row["path"]
        row["tasks"] = report.get("tasks") or []
        row["imported"] = report.get("imported") or []
        row["mode"] = report.get("mode")
        row["auto_undone"] = bool(report.get("auto_undone"))
        row["pending"] = json.loads(row["pending"]) if row.get("pending") else None
        best = next((t["candidates"][0] for t in row["tasks"] if t.get("candidates")), None)
        row["best_similarity"] = best["similarity"] if best else None
        if row["status"] == "error":
            row["stderr"] = report.get("stderr")
            row["report_error"] = report.get("error")
    # Triage order: highest match first, unmatched last.
    rows.sort(key=lambda r: (
        r["best_similarity"] is None, -(r["best_similarity"] or 0), -r["last_change"]
    ))
    return {"items": rows, "undo_seconds": settings.undo_seconds}


class InboxAction(BaseModel):
    path: str
    action: str  # apply | asis | search | retag | singletons | albums
    candidate: str | None = None
    search_id: str | None = None
    duplicate_action: str = "skip"


@app.post("/api/inbox/action")
async def inbox_action(body: InboxAction):
    row = db.query_one("SELECT * FROM inbox WHERE path = ?", (body.path,))
    if row is None:
        raise HTTPException(404, "Not in the inbox")
    if row["status"] in ("queued", "tagging", "pending"):
        raise HTTPException(409, "Already being tagged")
    if body.duplicate_action not in ("skip", "keep", "remove", "merge"):
        raise HTTPException(400, "Bad duplicate action")

    result: dict[str, Any] = {"ok": True}
    if body.action == "apply":
        if not body.candidate:
            raise HTTPException(400, "candidate is required")
        result["pending_until"] = inbox.schedule(body.path, {
            "mode": "apply", "candidate": body.candidate,
            "duplicate_action": body.duplicate_action,
        })
    elif body.action == "asis":
        result["pending_until"] = inbox.schedule(body.path, {
            "mode": "asis", "duplicate_action": body.duplicate_action,
        })
    elif body.action == "search":
        search_id = _clean_id(body.search_id or "")
        if not search_id:
            raise HTTPException(400, "Give a MusicBrainz or Spotify ID or link")
        inbox.enqueue(body.path, "candidates", manual=True, search_id=search_id)
    elif body.action == "retag":
        inbox.enqueue(body.path, "auto" if settings.auto_apply else "candidates",
                      manual=True)
    elif body.action in ("singletons", "albums"):
        singleton = body.action == "singletons"
        db.update("inbox", "path", body.path, singleton=int(singleton))
        inbox.enqueue(body.path, "candidates", manual=True, singletons=singleton)
    else:
        raise HTTPException(400, "Unknown action")
    db.log("info", "inbox", f"Requested {body.action}: {row['artist']} / {row['album']}",
           ref=body.path,
           details={k: v for k, v in body.model_dump().items() if v and k != "path"})
    return result


class UndoRequest(BaseModel):
    path: str | None = None  # default: the most recent pending action


@app.post("/api/inbox/undo")
async def inbox_undo(body: UndoRequest):
    row = inbox.undo(body.path)
    if row is None:
        raise HTTPException(409, "Nothing to undo (it may already have been applied)")
    restored = db.query_one("SELECT status FROM inbox WHERE path = ?", (row["path"],))
    return {"ok": True, "path": row["path"], "status": restored and restored["status"]}


def _clean_id(value: str) -> str:
    """Accept bare IDs or MusicBrainz/Spotify links."""
    value = value.strip()
    match = re.search(
        r"musicbrainz\.org/(?:release|recording)/([0-9a-f-]{36})", value
    ) or re.search(r"open\.spotify\.com/(?:intl-[a-z]+/)?(?:album|track)/([A-Za-z0-9]+)", value) \
        or re.search(r"spotify:(?:album|track):([A-Za-z0-9]+)", value)
    return match.group(1) if match else value


# ------------------------------------------------------------------ log


@app.get("/api/events")
async def events(limit: int = 200, kind: str | None = None, ref: str | None = None):
    sql = "SELECT * FROM events"
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if ref:
        clauses.append("ref = ?")
        params.append(ref)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(min(limit, 1000))
    rows = db.query(sql, tuple(params))
    for row in rows:
        row["details"] = json.loads(row["details"]) if row["details"] else None
    return {"events": rows}


# ------------------------------------------------------------------ UI


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
