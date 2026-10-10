"""Download jobs, run one at a time through apple-music-to-flac.py.

Each job gets its own folder under the downloading directory. The rip script
writes verified FLACs (plus .ttml sidecars) there; only when the run finishes
with every track present does the job's content move into the inbox.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import os
import shutil
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from . import db
from .config import AUDIO_EXTENSIONS, settings

# Events in the rip script's JSONL log that end a track.
DONE_EVENTS = {"track_completed"}
SKIP_EVENTS = {"track_skipped_history", "track_skipped_history_after_lock"}


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
        db.log("info", "download", f"Queued {kind}: {_job_label(title, subtitle)}",
               ref=job_id)
        self._wake.set()
        return job_id

    def retry(self, job_id: str, force: bool = False) -> None:
        job = self._get(job_id)
        if job["status"] in ("queued", "downloading", "finishing"):
            return
        meta = json.loads(job["output"] or "{}")
        meta.setdefault("options", {})["force"] = force or meta["options"].get("force")
        db.update("jobs", "id", job_id, status="queued", error=None,
                  updated=time.time(), output=json.dumps(meta))
        db.log("info", "download",
               f"{'Re-downloading' if force else 'Retrying'}: "
               f"{_job_label(job['title'], job['subtitle'])}", ref=job_id)
        self._wake.set()

    async def cancel(self, job_id: str) -> None:
        job = self._get(job_id)
        if job["status"] == "queued":
            db.update("jobs", "id", job_id, status="cancelled", updated=time.time())
            db.log("info", "download",
                   f"Cancelled: {_job_label(job['title'], job['subtitle'])}",
                   ref=job_id)
        elif self.current == job_id and self.process is not None:
            # The rip script cleans up on SIGINT, not on SIGTERM.
            self.process.send_signal(signal.SIGINT)
            self._cancel_requested = job_id

    async def release(self, job_id: str) -> None:
        """Send whatever a stopped job has into the inbox."""
        job = self._get(job_id)
        if job["status"] not in ("failed", "partial", "cancelled"):
            return
        moved = await asyncio.to_thread(self._move_to_inbox, job)
        db.update("jobs", "id", job_id, status="done", updated=time.time(),
                  error=None)
        db.log("info", "download",
               f"Sent {moved} file(s) to the inbox anyway: "
               f"{_job_label(job['title'], job['subtitle'])}", ref=job_id)

    async def discard(self, job_id: str) -> None:
        job = self._get(job_id)
        if job["status"] in ("queued", "downloading", "finishing"):
            return
        await asyncio.to_thread(shutil.rmtree, self.job_dir(job_id), True)
        db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        db.log("info", "download",
               f"Removed: {_job_label(job['title'], job['subtitle'])}", ref=job_id)

    def job_dir(self, job_id: str) -> Path:
        return settings.downloading_dir / job_id

    def log_path(self, job_id: str) -> Path:
        return settings.data_dir / "jobs" / job_id / "run.jsonl"

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
                       f"Job crashed: {_job_label(job['title'], job['subtitle'])}: {exc}",
                       ref=job["id"])
            finally:
                self.current = None
                self.process = None
                self.progress = {}

    async def _run_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        label = _job_label(job["title"], job["subtitle"])
        meta = json.loads(job["output"] or "{}")
        options = meta.get("options", {})

        out_dir = self.job_dir(job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        work_dir = settings.data_dir / "jobs" / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        ini = work_dir / "gamdl.ini"
        _write_job_ini(ini, out_dir)
        log_path = self.log_path(job_id)
        log_path.unlink(missing_ok=True)

        args = [
            settings.rip_python, settings.rip_script,
            "--config", str(ini),
            "--output", str(out_dir),
            "--jsonl-log", str(log_path),
            "--history-db", str(settings.history_db),
            "--freaccmd", settings.freaccmd,
            "--audio-policy", options.get("audio_policy") or settings.audio_policy,
            "--lyrics-format", settings.lyrics_format,
        ]
        if job["kind"] == "playlist" and options.get("playlist_albums"):
            args.append("--playlist-albums")
        if options.get("force"):
            if job["kind"] == "album":
                args += ["--force-album", job["apple_id"]]
            elif job["kind"] == "song":
                args += ["--force-track", job["apple_id"]]
            else:
                args.append("--force")
        args.append(job["url"])

        db.update("jobs", "id", job_id, status="downloading", done_tracks=0,
                  error=None, updated=time.time())
        db.log("info", "download", f"Downloading: {label}", ref=job_id)

        self.progress = {"done": 0, "skipped": 0, "failed": 0, "current": None}
        self.process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(work_dir),
            env={**os.environ, "HOME": str(work_dir)},
        )
        output_tail: list[str] = []

        async def pump() -> None:
            assert self.process and self.process.stdout
            async for raw in self.process.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                output_tail.append(line)
                del output_tail[:-200]

        async def watch() -> None:
            offset = 0
            while self.process and self.process.returncode is None:
                offset = await asyncio.to_thread(
                    self._read_progress, log_path, offset, job_id
                )
                await asyncio.sleep(1)

        watcher = asyncio.create_task(watch())
        await pump()
        returncode = await self.process.wait()
        watcher.cancel()
        await asyncio.to_thread(self._read_progress, log_path, 0, job_id, True)

        summary = self.progress.get("summary") or {}
        meta["summary"] = summary
        meta["returncode"] = returncode
        meta["tail"] = output_tail[-60:]
        meta["fatal"] = self.progress.get("fatal")
        files = await asyncio.to_thread(_audio_files, out_dir)
        done = self.progress["done"]
        failed = self.progress["failed"]

        if self._cancel_requested == job_id or returncode == 130:
            status, error = "cancelled", "Cancelled"
        elif returncode == 0 and files:
            status, error = "finishing", None
        elif returncode == 0 and self.progress["skipped"] and not files:
            status = "skipped"
            error = "Already ripped before (in the download history)"
        elif returncode == 0:
            status, error = "done", "Nothing was downloaded"
        else:
            status = "partial" if files else "failed"
            error = (
                self.progress.get("fatal")
                or self.progress.get("last_error")
                or (output_tail[-1] if output_tail else f"exit code {returncode}")
            )

        db.update("jobs", "id", job_id, status=status, error=error,
                  done_tracks=done, output=json.dumps(meta), updated=time.time())

        if status == "finishing":
            moved = await asyncio.to_thread(self._move_to_inbox, self._get(job_id))
            db.update("jobs", "id", job_id, status="done", updated=time.time())
            db.log("success", "download",
                   f"Downloaded {done} track(s), {moved} file(s) to inbox: {label}",
                   ref=job_id, details={"summary": summary})
        elif status == "skipped":
            db.log("info", "download", f"Skipped, already ripped: {label}",
                   ref=job_id)
        elif status == "cancelled":
            db.log("info", "download", f"Cancelled: {label}", ref=job_id)
        elif status == "done":
            db.log("warning", "download", f"Nothing downloaded: {label}", ref=job_id)
        else:
            db.log("error", "download",
                   f"{'Incomplete' if status == 'partial' else 'Failed'}: {label}: {error}",
                   ref=job_id, details={"summary": summary, "failed": failed})

    # -- progress

    def _read_progress(self, path: Path, offset: int, job_id: str,
                       final: bool = False) -> int:
        if not path.exists():
            return offset
        if final:
            offset = 0
            self.progress.update(done=0, skipped=0, failed=0)
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            for line in fh:
                if not line.endswith("\n"):
                    break
                offset += len(line.encode("utf-8"))
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._apply_event(event)
        if not final:
            db.update("jobs", "id", job_id, done_tracks=self.progress["done"],
                      updated=time.time())
        return offset

    def _apply_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        p = self.progress
        if name == "track_started":
            p["current"] = event.get("title")
        elif name in DONE_EVENTS:
            p["done"] += 1
        elif name in SKIP_EVENTS:
            p["skipped"] += 1
        elif name == "album_skipped_history":
            p["skipped"] += event.get("track_count") or 1
        elif name in ("track_attempt_failed", "finalization_failed") and event.get("terminal"):
            p["failed"] += 1
            p["last_error"] = f"{event.get('title')}: {event.get('error')}"
        elif name in ("item_resolution_failed", "url_resolution_exception") and not event.get("retryable", True):
            p["failed"] += 1
            p["last_error"] = event.get("error")
        elif name == "playlist_album_expansion_completed":
            p["albums"] = event.get("unique_albums")
        elif name == "run_summary":
            p["summary"] = {
                k: event.get(k) for k in (
                    "download_attempts", "flac_converted", "aac_retained",
                    "skipped", "history_skipped", "failed",
                    "finalization_failed", "exit_code",
                )
            }
        elif name == "run_fatal_error":
            p["fatal"] = f"{event.get('error_type')}: {event.get('error')}"

    # -- moving into the inbox

    def _move_to_inbox(self, job: dict[str, Any]) -> int:
        src_root = self.job_dir(job["id"])
        dst_root = settings.inbox_dir
        moved = 0
        album_dirs: set[Path] = set()
        if not src_root.exists():
            return 0
        for dirpath, dirnames, filenames in os.walk(src_root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue
                src = Path(dirpath) / name
                rel = src.relative_to(src_root)
                dst = dst_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    if dst.stat().st_size == src.stat().st_size:
                        src.unlink()
                        continue
                    dst = dst.with_name(f"{dst.stem} [nd-{job['id'][:6]}]{dst.suffix}")
                os.rename(src, dst)
                moved += 1
                if dst.suffix.lower() in AUDIO_EXTENSIONS:
                    album_dirs.add(dst.parent)
        shutil.rmtree(src_root, ignore_errors=True)

        # Single songs pulled out of a larger album go in as singletons.
        singleton = 1 if (
            job["kind"] == "song" and (job.get("expected_tracks") or 1) == 1
            and _album_has_more_tracks(job)
        ) else 0
        now = time.time()
        for album_dir in album_dirs:
            db.execute(
                "INSERT INTO inbox (path, first_seen, last_change, fingerprint, "
                "status, artist, album, job_id, singleton) "
                "VALUES (?, ?, ?, '', 'waiting', ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET job_id = excluded.job_id, "
                "singleton = MAX(inbox.singleton, excluded.singleton)",
                (str(album_dir), now, now, album_dir.parent.name, album_dir.name,
                 job["id"], singleton),
            )
        return moved

    def _get(self, job_id: str) -> dict[str, Any]:
        job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if job is None:
            raise KeyError(job_id)
        return job


def _album_has_more_tracks(job: dict[str, Any]) -> bool:
    meta = json.loads(job.get("output") or "{}")
    return (meta.get("options", {}).get("album_track_count") or 1) > 1


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


def _write_job_ini(path: Path, out_dir: Path) -> None:
    """Per-job copy of the gamdl ini, pointed at the wrapper's compose network."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(settings.gamdl_ini_template)
    section = parser["gamdl"]
    section["use_wrapper"] = "true"
    section["wrapper_url"] = settings.wrapper_url
    section["wrapper_decrypt_host"] = settings.wrapper_decrypt_host
    section["wrapper_decrypt_port"] = str(settings.wrapper_decrypt_port)
    section["output_path"] = str(out_dir)
    section["temp_path"] = str(out_dir / ".gamdl-temp")
    section["language"] = settings.language
    with path.open("w", encoding="utf-8") as fh:
        parser.write(fh)


def _job_label(title: str | None, subtitle: str | None) -> str:
    if title and subtitle:
        return f"{title} — {subtitle}"
    return title or subtitle or "(untitled)"


downloads = Downloads()
