"""Download jobs, run one at a time through the rip engine (app/rip.py).

Each job gets its own folder under the downloading directory. Only when every
track of the job is present does its content move into the inbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import mutagen

from . import db, history
from .config import AUDIO_EXTENSIONS, settings

ACTIVE = ("queued", "downloading", "finishing")


class Downloads:
    def __init__(self) -> None:
        self._wake = asyncio.Event()
        self.current: str | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.progress: dict[str, Any] = {}
        self._cancel_requested: str | None = None

    # -- public API

    def create(self, *, kind: str, apple_id: str, url: str, title: str | None,
               subtitle: str | None, artwork: str | None,
               expected_tracks: int | None, options: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        db.execute(
            "INSERT INTO jobs (id, created, updated, kind, apple_id, url, title, "
            "subtitle, artwork, expected_tracks, status, output) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
            (job_id, now, now, kind, apple_id, url, title, subtitle, artwork,
             expected_tracks, json.dumps({"options": options})),
        )
        db.log("info", "download", f"Queued {kind}: {_label(title, subtitle)}",
               ref=job_id)
        self._wake.set()
        return job_id

    def retry(self, job_id: str, force: bool = False) -> None:
        job = self._get(job_id)
        if job["status"] in ACTIVE:
            return
        meta = json.loads(job["output"] or "{}")
        options = meta.setdefault("options", {})
        options["force"] = force or options.get("force", False)
        db.update("jobs", "id", job_id, status="queued", error=None,
                  updated=time.time(), output=json.dumps(meta))
        db.log("info", "download",
               f"{'Re-downloading' if force else 'Retrying'}: "
               f"{_label(job['title'], job['subtitle'])}", ref=job_id)
        self._wake.set()

    async def cancel(self, job_id: str) -> None:
        job = self._get(job_id)
        if job["status"] == "queued":
            db.update("jobs", "id", job_id, status="cancelled", updated=time.time())
            db.log("info", "download",
                   f"Cancelled: {_label(job['title'], job['subtitle'])}", ref=job_id)
        elif self.current == job_id and self.process is not None:
            self._cancel_requested = job_id
            self.process.send_signal(signal.SIGINT)

    async def release(self, job_id: str) -> None:
        """Send whatever a stopped job has into the inbox."""
        job = self._get(job_id)
        if job["status"] not in ("failed", "partial", "cancelled"):
            return
        moved = await asyncio.to_thread(self._move_to_inbox, job)
        db.update("jobs", "id", job_id, status="done", updated=time.time(), error=None)
        db.log("info", "download",
               f"Sent {moved} file(s) to the inbox anyway: "
               f"{_label(job['title'], job['subtitle'])}", ref=job_id)

    async def discard(self, job_id: str) -> None:
        job = self._get(job_id)
        if job["status"] in ACTIVE:
            return
        await asyncio.to_thread(shutil.rmtree, self.job_dir(job_id), True)
        if job["status"] != "done":
            # Its files are gone, so don't let the history claim we have them.
            db.execute("DELETE FROM history WHERE source = ?", (f"job:{job_id}",))
        db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        db.log("info", "download",
               f"Removed: {_label(job['title'], job['subtitle'])}", ref=job_id)

    def job_dir(self, job_id: str) -> Path:
        return settings.downloading_dir / job_id

    # -- worker

    async def run(self) -> None:
        for job in db.query(
            "SELECT * FROM jobs WHERE status IN ('downloading', 'finishing')"
        ):
            db.update("jobs", "id", job["id"], status="failed",
                      error="Interrupted by a restart", updated=time.time())
        while True:
            job = db.query_one(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created LIMIT 1"
            )
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), 30)
                except asyncio.TimeoutError:
                    pass
                continue
            self.current = job["id"]
            self._cancel_requested = None
            try:
                await self._run_job(job)
            except Exception as exc:
                db.update("jobs", "id", job["id"], status="failed",
                          error=f"{type(exc).__name__}: {exc}", updated=time.time())
                db.log("error", "download",
                       f"Job crashed: {_label(job['title'], job['subtitle'])}: {exc}",
                       ref=job["id"])
            finally:
                self.current = None
                self.process = None
                self.progress = {}

    async def _run_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        label = _label(job["title"], job["subtitle"])
        meta = json.loads(job["output"] or "{}")
        options = meta.get("options", {})

        out_dir = self.job_dir(job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        work_dir = settings.data_dir / "jobs" / job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        args = [
            sys.executable, "-m", "app.rip",
            "--url", job["url"],
            "--output", str(out_dir),
            "--history-db", str(settings.db_path),
            "--audio-policy", options.get("audio_policy") or settings.audio_policy,
            "--lyrics-format", settings.lyrics_format,
            "--freaccmd", settings.freaccmd,
            "--wrapper-url", settings.wrapper_url,
            "--wrapper-decrypt-host", settings.wrapper_decrypt_host,
            "--wrapper-decrypt-port", str(settings.wrapper_decrypt_port),
            "--language", settings.language,
        ]
        if job["kind"] == "playlist" and options.get("playlist_albums"):
            args.append("--playlist-albums")
        if options.get("force"):
            args.append("--force")
        if options.get("only"):
            args += ["--only", ",".join(options["only"])]

        db.update("jobs", "id", job_id, status="downloading", done_tracks=0,
                  error=None, updated=time.time())
        db.log("info", "download", f"Downloading: {label}", ref=job_id)

        p = self.progress = {
            "done": 0, "skipped": 0, "failed": 0, "current": None,
            "total": 0, "errors": [], "skipped_history": 0,
            "lossy": [], "unavailable": [],
        }
        self.process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).resolve().parent.parent),
            env={**os.environ, "HOME": str(work_dir)},
            limit=1 << 20,
        )
        tail: list[str] = []

        async def read_events() -> None:
            assert self.process and self.process.stdout
            async for raw in self.process.stdout:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._apply(event, job_id)

        async def read_log() -> None:
            assert self.process and self.process.stderr
            buffer = b""
            while chunk := await self.process.stderr.read(65536):
                buffer += chunk
                *lines, buffer = re.split(rb"[\r\n]", buffer)
                for line in lines:
                    text = line.decode("utf-8", "replace").rstrip()
                    if text and not text.startswith("[download]"):
                        tail.append(text[:500])
                del tail[:-200]

        await asyncio.gather(read_events(), read_log())
        returncode = await self.process.wait()

        files = await asyncio.to_thread(_audio_files, out_dir)
        meta.update(
            returncode=returncode, tail=tail[-60:],
            summary=p.get("summary"), errors=p["errors"][-20:],
            lossy=p["lossy"], unavailable=p["unavailable"],
        )

        if self._cancel_requested == job_id or returncode == 130:
            status, error = "cancelled", "Cancelled"
        elif p.get("fatal"):
            status = "partial" if files else "failed"
            error = p["fatal"]
        elif returncode == 0 and files:
            status, error = "finishing", None
        elif returncode == 0 and p["skipped_history"] and not files:
            status, error = "skipped", "Already downloaded before (in the history)"
        elif returncode == 0 and p["unavailable"] and not files:
            status, error = "done", _unavailable_text(p["unavailable"])
        elif returncode == 0:
            status, error = "done", "Nothing to download"
        else:
            status = "partial" if files else "failed"
            error = (p["errors"][-1] if p["errors"]
                     else tail[-1] if tail else f"exit code {returncode}")

        db.update("jobs", "id", job_id, status=status, error=error,
                  done_tracks=p["done"], expected_tracks=p["total"] or job["expected_tracks"],
                  output=json.dumps(meta), updated=time.time())

        if status == "finishing":
            moved = await asyncio.to_thread(self._move_to_inbox, self._get(job_id))
            db.update("jobs", "id", job_id, status="done", updated=time.time())
            notes = []
            if p["lossy"]:
                notes.append(f"{len(p['lossy'])} lossy (AAC)")
            if p["unavailable"]:
                notes.append(_unavailable_text(p["unavailable"]))
            if p["skipped"]:
                notes.append(f"{p['skipped']} already downloaded" if p["skipped"] == p["skipped_history"]
                             else f"{p['skipped']} skipped")
            db.log("warning" if p["lossy"] or p["unavailable"] else "success", "download",
                   f"Downloaded {p['done']} track(s){' — ' + '; '.join(notes) if notes else ''}; "
                   f"{moved} file(s) to the inbox: {label}",
                   ref=job_id, details={"summary": p.get("summary"), "lossy": p["lossy"],
                                        "unavailable": p["unavailable"]})
        elif status == "skipped":
            db.log("info", "download", f"Already downloaded: {label}", ref=job_id)
        elif status == "cancelled":
            db.log("info", "download", f"Cancelled: {label}", ref=job_id)
        elif status == "done":
            db.log("warning", "download", f"Nothing downloaded: {label}: {error}", ref=job_id,
                   details={"unavailable": p["unavailable"]})
        else:
            db.log("error", "download",
                   f"{'Incomplete' if status == 'partial' else 'Failed'}: {label}: {error}",
                   ref=job_id, details={"errors": p["errors"][-5:]})

    def _apply(self, event: dict[str, Any], job_id: str) -> None:
        p = self.progress
        name = event.get("event")
        if name in ("album", "playlist"):
            p["total"] += event.get("total") or 0
        elif name == "expanded":
            p["albums"] = event.get("albums")
        elif name == "track_started":
            p["current"] = event.get("title")
            if not p["total"]:
                p["total"] = event.get("total") or 0
        elif name == "track_completed":
            p["done"] += 1
            if event.get("lossless") is False:
                p["lossy"].append({"title": event.get("title"), "codec": event.get("codec")})
            history.record(event, job_id)
        elif name == "track_unavailable":
            p["unavailable"].append({"title": event.get("title"), "reason": event.get("reason")})
        elif name == "track_skipped":
            p["skipped"] += 1
            if event.get("reason") == "history":
                p["skipped_history"] += 1
        elif name == "track_failed":
            p["failed"] += 1
            p["errors"].append(f"{event.get('title')}: {event.get('error')}")
        elif name == "url_failed":
            p["failed"] += 1
            p["errors"].append(event.get("error"))
        elif name == "summary":
            p["summary"] = {k: event.get(k) for k in
                            ("completed", "lossy", "skipped", "unavailable", "failed")}
        elif name == "fatal":
            p["fatal"] = event.get("error")
        if name in ("track_completed", "track_skipped", "track_unavailable", "track_failed",
                    "album", "playlist"):
            db.update("jobs", "id", job_id, done_tracks=p["done"], updated=time.time(),
                      expected_tracks=p["total"] or None)

    # -- moving into the inbox

    def _move_to_inbox(self, job: dict[str, Any]) -> int:
        src_root = self.job_dir(job["id"])
        dst_root = settings.inbox_dir
        replace = json.loads(job.get("output") or "{}").get("options", {}).get("replace_existing")
        moved = 0
        album_dirs: set[Path] = set()
        if not src_root.exists():
            return 0
        for dirpath, dirnames, filenames in os.walk(src_root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            dst_dir = dst_root / Path(dirpath).relative_to(src_root)
            # Tracks already in the destination folder (by Apple Music id), so
            # a song download and an album download never leave two copies.
            have = _track_ids(dst_dir) if not replace and dst_dir.is_dir() else {}
            skip_stems: set[str] = set()
            for name in sorted(filenames, key=lambda n: Path(n).suffix.lower() not in AUDIO_EXTENSIONS):
                if name.startswith("."):
                    continue
                src = Path(dirpath) / name
                dst = dst_dir / name
                if Path(name).stem in skip_stems:
                    src.unlink()  # sidecar of a track we already have
                    continue
                if src.suffix.lower() in AUDIO_EXTENSIONS and have:
                    track_id = _track_id(src)
                    existing = have.get(track_id) if track_id else None
                    if existing is not None and existing.exists():
                        if existing.suffix.lower() == ".flac" or src.suffix.lower() != ".flac":
                            src.unlink()  # keep the copy that's already there
                            skip_stems.add(src.stem)
                            continue
                        # The new copy is lossless and the old one isn't.
                        _archive_inbox_file(existing)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    if replace:
                        # A re-download for this folder: the new copy wins.
                        os.replace(src, dst)
                        moved += 1
                        if dst.suffix.lower() in AUDIO_EXTENSIONS:
                            album_dirs.add(dst.parent)
                        continue
                    if dst.stat().st_size == src.stat().st_size:
                        src.unlink()
                        continue
                    dst = dst.with_name(f"{dst.stem} [nd-{job['id'][:6]}]{dst.suffix}")
                os.rename(src, dst)
                moved += 1
                if dst.suffix.lower() in AUDIO_EXTENSIONS:
                    album_dirs.add(dst.parent)
        shutil.rmtree(src_root, ignore_errors=True)

        # A single song pulled out of a bigger album is imported as a single
        # track, but only when its folder holds nothing else: the flag must
        # never spill onto an album that shares the folder.
        meta = json.loads(job.get("output") or "{}")
        song_of_album = (
            job["kind"] == "song"
            and (meta.get("options", {}).get("album_track_count") or 1) > 1
        )
        now = time.time()
        for album_dir in album_dirs:
            audio_here = sum(
                1 for name in os.listdir(album_dir)
                if Path(name).suffix.lower() in AUDIO_EXTENSIONS
            )
            singleton = int(song_of_album and audio_here == 1)
            db.execute(
                "INSERT INTO inbox (path, first_seen, last_change, fingerprint, "
                "status, artist, album, job_id, singleton) "
                "VALUES (?, ?, ?, '', 'waiting', ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET job_id = excluded.job_id, "
                "singleton = excluded.singleton",
                (str(album_dir), now, now, album_dir.parent.name, album_dir.name,
                 job["id"], singleton),
            )
        return moved

    def _get(self, job_id: str) -> dict[str, Any]:
        job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if job is None:
            raise KeyError(job_id)
        return job


def _track_id(path: Path) -> str | None:
    try:
        tags = getattr(mutagen.File(path), "tags", None) or {}
    except Exception:
        return None
    for key in ("apple_music_id", "cnID"):
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if value:
            return str(value[0] if isinstance(value, list) else value)
    return None


def _track_ids(folder: Path) -> dict[str, Path]:
    ids: dict[str, Path] = {}
    for entry in folder.iterdir():
        if entry.suffix.lower() in AUDIO_EXTENSIONS and not entry.name.startswith("."):
            track_id = _track_id(entry)
            if track_id:
                ids.setdefault(track_id, entry)
    return ids


def _archive_inbox_file(path: Path) -> None:
    """Move a superseded inbox file (and its sidecars) into the archive."""
    rel = path.parent.relative_to(settings.inbox_dir)
    dest_dir = settings.archive_dir / time.strftime("%Y-%m-%d") / "superseded" / rel
    dest_dir.mkdir(parents=True, exist_ok=True)
    for sibling in path.parent.iterdir():
        if sibling.stem == path.stem:
            shutil.move(str(sibling), str(dest_dir / sibling.name))


def _unavailable_text(items: list[dict[str, Any]]) -> str:
    reasons = sorted({i.get("reason") or "unavailable" for i in items})
    return f"{len(items)} unavailable ({', '.join(reasons)})"


def _audio_files(root: Path) -> list[Path]:
    found = []
    if not root.exists():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in AUDIO_EXTENSIONS and not name.startswith("."):
                found.append(Path(dirpath) / name)
    return found


def _label(title: str | None, subtitle: str | None) -> str:
    if title and subtitle:
        return f"{title} — {subtitle}"
    return title or subtitle or "(untitled)"


downloads = Downloads()
