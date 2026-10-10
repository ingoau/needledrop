"""Inbox watcher and the beets tagger queue.

The watcher treats every directory under the inbox that directly holds audio
files as one item. An item is handed to the tagger once its files have been
unchanged for the buffer period, so a half-written album is never tagged.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db
from .config import AUDIO_EXTENSIONS, settings

APPLIED_VISIBLE_SECONDS = 24 * 3600


# ---------------------------------------------------------------- scanning


def _scan_inbox() -> dict[str, tuple[str, int, str | None, str | None]]:
    """Return {dir: (fingerprint, audio_count, artist, album)} for the inbox."""
    found: dict[str, tuple[str, int, str | None, str | None]] = {}
    root = settings.inbox_dir
    if not root.is_dir():
        return found

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden folders (gamdl temp, the rip script's locks, etc).
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        audio = [
            f for f in filenames
            if not f.startswith(".") and Path(f).suffix.lower() in AUDIO_EXTENSIONS
        ]
        if not audio:
            continue
        digest = hashlib.sha1()
        for name in sorted(filenames):
            try:
                st = os.stat(os.path.join(dirpath, name))
            except FileNotFoundError:
                continue
            digest.update(f"{name}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
        rel = Path(dirpath).relative_to(root)
        parts = rel.parts
        artist = parts[0] if len(parts) >= 2 else None
        album = parts[-1] if parts else None
        found[dirpath] = (digest.hexdigest(), len(audio), artist, album)
    return found


def _has_audio(path: str) -> bool:
    try:
        return any(
            Path(name).suffix.lower() in AUDIO_EXTENSIONS
            for name in os.listdir(path)
        )
    except FileNotFoundError:
        return False


def _remove_empty_dirs(path: str) -> None:
    """Remove `path` and empty parents up to (not including) the inbox."""
    root = settings.inbox_dir.resolve()
    current = Path(path)
    while True:
        try:
            resolved = current.resolve()
        except FileNotFoundError:
            resolved = current
        if resolved == root or root not in resolved.parents:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


# ---------------------------------------------------------------- tagger


@dataclass(order=True)
class TagRequest:
    priority: int
    seq: int
    path: str = field(compare=False)
    mode: str = field(compare=False)
    candidate: str | None = field(compare=False, default=None)
    search_id: str | None = field(compare=False, default=None)
    duplicate_action: str = field(compare=False, default="skip")
    singletons: bool = field(compare=False, default=False)
    manual: bool = field(compare=False, default=False)


class Inbox:
    def __init__(self) -> None:
        self.queue: asyncio.PriorityQueue[TagRequest] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self.current: str | None = None
        self._rescan_needed = asyncio.Event()

    # -- public API used by the web layer

    def enqueue(self, path: str, mode: str, *, manual: bool = False,
                candidate: str | None = None, search_id: str | None = None,
                duplicate_action: str = "skip",
                singletons: bool | None = None) -> None:
        row = db.query_one("SELECT * FROM inbox WHERE path = ?", (path,))
        if row is None:
            raise KeyError(path)
        if singletons is None:
            singletons = bool(row["singleton"])
        db.update("inbox", "path", path, status="queued", error=None)
        self.queue.put_nowait(
            TagRequest(
                priority=0 if manual else 1,
                seq=next(self._seq),
                path=path,
                mode=mode,
                candidate=candidate,
                search_id=search_id,
                duplicate_action=duplicate_action,
                singletons=singletons,
                manual=manual,
            )
        )

    def poke(self) -> None:
        self._rescan_needed.set()

    # -- background loops

    async def run(self) -> None:
        await asyncio.gather(self._scan_loop(), self._tag_loop())

    async def _scan_loop(self) -> None:
        # Anything left mid-flight by a restart goes back through the buffer.
        db.execute(
            "UPDATE inbox SET status = 'waiting' "
            "WHERE status IN ('queued', 'tagging')"
        )
        while True:
            try:
                await self._scan_once()
            except Exception as exc:  # keep the loop alive
                db.log("error", "inbox", f"Inbox scan failed: {exc}")
            try:
                await asyncio.wait_for(
                    self._rescan_needed.wait(), settings.inbox_scan_seconds
                )
            except asyncio.TimeoutError:
                pass
            self._rescan_needed.clear()

    async def _scan_once(self) -> None:
        settings.inbox_dir.mkdir(parents=True, exist_ok=True)
        found = await asyncio.to_thread(_scan_inbox)
        now = time.time()
        known = {row["path"]: row for row in db.query("SELECT * FROM inbox")}

        for path, (fingerprint, count, artist, album) in found.items():
            row = known.get(path)
            if row is None:
                db.execute(
                    "INSERT INTO inbox (path, first_seen, last_change, fingerprint, "
                    "status, artist, album, track_count) "
                    "VALUES (?, ?, ?, ?, 'waiting', ?, ?, ?)",
                    (path, now, now, fingerprint, artist, album, count),
                )
                db.log("info", "inbox", f"Arrived: {_label(artist, album)}",
                       ref=path, details={"tracks": count})
                continue
            if row["fingerprint"] != fingerprint:
                fields: dict[str, Any] = {
                    "fingerprint": fingerprint,
                    "last_change": now,
                    "track_count": count,
                }
                if row["status"] not in ("queued", "tagging"):
                    fields["status"] = "waiting"
                db.update("inbox", "path", path, **fields)
            elif row["status"] == "applied":
                # Audio reappeared under a path we finished with.
                db.update("inbox", "path", path, status="waiting", last_change=now)

        for path, row in known.items():
            if path in found:
                continue
            if row["status"] in ("queued", "tagging"):
                continue
            if row["status"] == "applied":
                if now - row["last_change"] > APPLIED_VISIBLE_SECONDS:
                    db.execute("DELETE FROM inbox WHERE path = ?", (path,))
                continue
            db.execute("DELETE FROM inbox WHERE path = ?", (path,))
            db.log("info", "inbox",
                   f"Left the inbox: {_label(row['artist'], row['album'])}",
                   ref=path)

        # Hand settled items to the tagger.
        for row in db.query("SELECT * FROM inbox WHERE status = 'waiting'"):
            if now - row["last_change"] < settings.tagger_buffer_seconds:
                continue
            if row["analyzed_fingerprint"] == row["fingerprint"] and row["report"]:
                report = json.loads(row["report"])
                if not (settings.auto_apply and _strong_but_unapplied(report)):
                    # Nothing changed since the last verdict; restore it.
                    db.update("inbox", "path", row["path"],
                              status=_status_from_report(report))
                    continue
            mode = "auto" if settings.auto_apply else "candidates"
            self.enqueue(row["path"], mode)

        db.prune_events()

    async def _tag_loop(self) -> None:
        while True:
            request = await self.queue.get()
            row = db.query_one("SELECT * FROM inbox WHERE path = ?", (request.path,))
            if row is None or not os.path.isdir(request.path):
                continue
            self.current = request.path
            try:
                await self._tag(request, row)
            except Exception as exc:
                db.update("inbox", "path", request.path, status="error",
                          error=str(exc))
                db.log("error", "tagger",
                       f"Tagger crashed on {_label(row['artist'], row['album'])}: {exc}",
                       ref=request.path)
            finally:
                self.current = None
                self.poke()

    async def _tag(self, request: TagRequest, row: dict[str, Any]) -> None:
        label = _label(row["artist"], row["album"])
        db.update("inbox", "path", request.path, status="tagging")
        fingerprint = row["fingerprint"]

        report = await run_beets(request)
        error = report.get("error")
        imported = report.get("imported") or []
        tasks = report.get("tasks") or []

        if error and not imported:
            db.update("inbox", "path", request.path, status="error",
                      error=error, report=json.dumps(report),
                      analyzed_fingerprint=fingerprint)
            db.log("error", "tagger", f"beets failed on {label}: {error}",
                   ref=request.path, details={"stderr": report.get("stderr")})
            return

        if imported:
            chosen = _chosen_candidate(tasks)
            how = {
                "auto": "Auto-applied",
                "apply": "Applied",
                "asis": "Imported as-is",
            }.get(request.mode, "Imported")
            message = f"{how}: {label}"
            if chosen:
                message += (
                    f" → {chosen.get('artist')} - {chosen.get('title')}"
                    f" ({chosen.get('data_source')}, "
                    f"{round((chosen.get('similarity') or 0) * 100, 1)}%)"
                )
            db.log("success", "tagger", message, ref=request.path, details={
                "mode": request.mode,
                "manual": request.manual,
                "candidate": chosen,
                "imported": imported,
            })
            await asyncio.to_thread(self._after_import, request.path)
            remaining = _has_audio(request.path)
            db.update(
                "inbox", "path", request.path,
                status="waiting" if remaining else "applied",
                report=json.dumps(report),
                analyzed_fingerprint=None if remaining else fingerprint,
                last_change=time.time(),
            )
            return

        status = _status_from_report(report)
        db.update("inbox", "path", request.path, status=status,
                  report=json.dumps(report), error=None,
                  analyzed_fingerprint=fingerprint)

        best = _best_candidate(tasks)
        if request.mode == "candidates" and request.search_id:
            found = len(best and [best] or [])
            db.log("info", "tagger",
                   f"Looked up {request.search_id} for {label}: "
                   + ("found a candidate" if found else "no match"),
                   ref=request.path)
        elif status == "duplicate":
            db.log("warning", "tagger",
                   f"Already in library, waiting for a decision: {label}",
                   ref=request.path,
                   details={"duplicates": _duplicates(tasks)})
        elif request.mode == "apply":
            db.log("warning", "tagger",
                   f"Apply did not import {label}"
                   + (f": {tasks[0].get('error')}" if tasks and tasks[0].get("error") else ""),
                   ref=request.path)
        else:
            sim = (
                f"{round(best['similarity'] * 100, 1)}% {best.get('data_source')}"
                if best else "no candidates"
            )
            db.log("info", "tagger",
                   f"Needs review ({sim}): {label}", ref=request.path,
                   details={"best": best})

    def _after_import(self, path: str) -> None:
        if not _has_audio(path):
            _remove_empty_dirs(path)


# ---------------------------------------------------------------- beets


async def run_beets(request: TagRequest) -> dict[str, Any]:
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = settings.reports_dir / f"{uuid.uuid4().hex}.json"
    args = [
        settings.beet_python, settings.beet_driver,
        "-c", str(settings.beet_overlay),
        "needledrop-import",
        "--mode", request.mode,
        "--report", str(report_path),
        "--duplicate-action", request.duplicate_action,
    ]
    if request.candidate:
        args += ["--candidate", request.candidate]
    if request.search_id:
        args += ["--search-id", request.search_id]
    if request.singletons:
        args.append("--singletons")
    args.append(request.path)

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
    )
    output, _ = await process.communicate()
    text = output.decode("utf-8", "replace")

    report: dict[str, Any] = {}
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text())
        finally:
            report_path.unlink(missing_ok=True)
    report["returncode"] = process.returncode
    report["stderr"] = text[-6000:]
    if process.returncode != 0 and not report.get("error"):
        last = [line for line in text.strip().splitlines() if line.strip()]
        report["error"] = last[-1] if last else f"beet exited {process.returncode}"
    return report


# ---------------------------------------------------------------- helpers


def _label(artist: str | None, album: str | None) -> str:
    if artist and album:
        return f"{artist} / {album}"
    return album or artist or "(unknown)"


def _status_from_report(report: dict[str, Any]) -> str:
    if report.get("error") and not report.get("tasks"):
        return "error"
    for task in report.get("tasks") or []:
        if task.get("duplicates") and task.get("duplicate_action") == "skip":
            return "duplicate"
    return "review"


def _strong_but_unapplied(report: dict[str, Any]) -> bool:
    """A look-only verdict (auto-apply was off) that auto-apply would take."""
    return report.get("mode") == "candidates" and any(
        task.get("recommendation") == "strong" and not task.get("duplicates")
        for task in report.get("tasks") or []
    )


def _best_candidate(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for task in tasks:
        if task.get("candidates"):
            return task["candidates"][0]
    return None


def _chosen_candidate(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for task in tasks:
        chosen = task.get("chosen")
        for candidate in task.get("candidates") or []:
            if candidate.get("id") == chosen:
                return candidate
    return None


def _duplicates(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dup for task in tasks for dup in task.get("duplicates") or []]


inbox = Inbox()
