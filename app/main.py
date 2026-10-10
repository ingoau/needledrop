"""Needledrop web app: search Apple Music, rip it, and triage the inbox."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .apple import catalog
from .config import settings
from .downloads import downloads
from .inbox import inbox

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.conn()
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
        "rip_active": inbox.rip_active,
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
            return {"results": {item["type"]: [item]} if item else {}}
        return {"results": await catalog.search(q, types)}
    except Exception as exc:
        raise HTTPException(502, f"Apple Music search failed: {exc}")


@app.get("/api/artist/{artist_id}")
async def artist(artist_id: str):
    return await _catalog_call(catalog.artist, artist_id)


@app.get("/api/album/{album_id}")
async def album(album_id: str):
    return await _catalog_call(catalog.album, album_id)


@app.get("/api/playlist/{playlist_id}")
async def playlist(playlist_id: str):
    return await _catalog_call(catalog.playlist, playlist_id)


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
            is_single = bool(re.search(r"\s-\s(Single|EP)$", item.album or ""))
            options["album_track_count"] = 1 if is_single else 2
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


# ------------------------------------------------------------------ inbox


@app.get("/api/inbox")
async def list_inbox():
    rows = db.query(
        "SELECT * FROM inbox ORDER BY "
        "CASE status WHEN 'tagging' THEN 0 WHEN 'duplicate' THEN 1 "
        "WHEN 'review' THEN 2 WHEN 'error' THEN 3 WHEN 'queued' THEN 4 "
        "WHEN 'waiting' THEN 5 ELSE 6 END, last_change DESC"
    )
    root = str(settings.inbox_dir).rstrip("/") + "/"
    for row in rows:
        report = json.loads(row.pop("report") or "null") or {}
        row["relpath"] = row["path"][len(root):] if row["path"].startswith(root) else row["path"]
        row["tasks"] = [
            {k: v for k, v in task.items()} for task in report.get("tasks") or []
        ]
        row["imported"] = report.get("imported") or []
        row["mode"] = report.get("mode")
        if row["status"] == "error":
            row["stderr"] = report.get("stderr")
            row["report_error"] = report.get("error")
    return {"items": rows}


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
    if row["status"] in ("queued", "tagging"):
        raise HTTPException(409, "Already being tagged")
    if body.duplicate_action not in ("skip", "keep", "remove", "merge"):
        raise HTTPException(400, "Bad duplicate action")

    if body.action == "apply":
        if not body.candidate:
            raise HTTPException(400, "candidate is required")
        inbox.enqueue(body.path, "apply", manual=True, candidate=body.candidate,
                      duplicate_action=body.duplicate_action)
    elif body.action == "asis":
        inbox.enqueue(body.path, "asis", manual=True,
                      duplicate_action=body.duplicate_action)
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
    return {"ok": True}


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
