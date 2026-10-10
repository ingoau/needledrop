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
import shutil
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
    auto: bool = field(compare=False, default=False)  # may schedule an auto-apply
    task_paths: tuple[str, ...] = field(compare=False, default=())


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
                singletons: bool | None = None, auto: bool = False,
                task_paths: list[str] | None = None) -> None:
        row = db.query_one("SELECT * FROM inbox WHERE path = ?", (path,))
        if row is None:
            raise KeyError(path)
        if singletons is None:
            singletons = bool(row["singleton"])
        db.update("inbox", "path", path, status="queued", error=None, queued_mode=mode)
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
                auto=auto,
                task_paths=tuple(task_paths or ()),
            )
        )

    # -- undoable actions: held for a few seconds before beets runs

    def schedule(self, path: str, action: dict[str, Any]) -> float:
        """Queue an apply/as-is that can still be undone for UNDO_SECONDS."""
        until = time.time() + settings.undo_seconds
        db.update("inbox", "path", path, status="pending",
                  pending=json.dumps(action), pending_until=until, error=None)
        return until

    def undo(self, path: str | None = None) -> dict[str, Any] | None:
        """Cancel a pending action (the newest one without a path). Actions
        scheduled together (a batch) are undone together."""
        if path is None:
            row = db.query_one(
                "SELECT * FROM inbox WHERE status = 'pending' "
                "ORDER BY pending_until DESC LIMIT 1"
            )
        else:
            row = db.query_one(
                "SELECT * FROM inbox WHERE path = ? AND status = 'pending'", (path,)
            )
        if row is None:
            return None
        batch = json.loads(row["pending"] or "{}").get("batch")
        rows = [row]
        if batch:
            rows = [
                r for r in db.query("SELECT * FROM inbox WHERE status = 'pending'")
                if json.loads(r["pending"] or "{}").get("batch") == batch
            ]
        for r in rows:
            action = json.loads(r["pending"] or "{}")
            report = json.loads(r["report"] or "{}")
            if action.get("mode") in ("apply", "asis", "auto"):
                # Whoever applied it, an undo means: don't auto-apply this content.
                report["auto_undone"] = True
            # Never looked up yet: back to the queue rather than an empty review.
            status = _status_from_report(report) if report.get("tasks") else "waiting"
            db.update("inbox", "path", r["path"], status=status,
                      report=json.dumps(report) if report else None,
                      pending=None, pending_until=None)
        what = json.loads(row["pending"] or "{}").get("mode", "apply")
        db.log("info", "tagger",
               f"Undone {what}: " + (
                   f"{len(rows)} items" if len(rows) > 1
                   else _label(row["artist"], row["album"])
               ),
               ref=None if len(rows) > 1 else row["path"])
        row["undone"] = [r["path"] for r in rows]
        return row

    async def _pending_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            now = time.time()
            for row in db.query(
                "SELECT * FROM inbox WHERE status = 'pending' AND pending_until <= ?",
                (now,),
            ):
                action = json.loads(row["pending"] or "{}")
                db.update("inbox", "path", row["path"], pending=None, pending_until=None)
                if action.get("mode") == "archive":
                    await asyncio.to_thread(self._archive, row)
                    continue
                self.enqueue(
                    row["path"], action.get("mode", "apply"),
                    manual=True,
                    candidate=action.get("candidate"),
                    duplicate_action=action.get("duplicate_action", "skip"),
                    task_paths=action.get("task_paths"),
                )

    def poke(self) -> None:
        self._rescan_needed.set()

    # -- background loops

    async def run(self) -> None:
        await asyncio.gather(self._scan_loop(), self._tag_loop(), self._pending_loop())

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

        # Strong matches found while auto-apply was off, or before a restart.
        if settings.auto_apply:
            for row in db.query(
                "SELECT * FROM inbox WHERE status = 'review' AND report IS NOT NULL"
            ):
                report = json.loads(row["report"])
                best = _best_candidate(report.get("tasks") or [])
                if best and _strong_but_unapplied(report):
                    self.schedule(row["path"], {"mode": "auto", "auto": True})
                    db.log("info", "tagger",
                           f"Strong match ({round(best['similarity'] * 100, 1)}%), "
                           f"applying in {settings.undo_seconds}s unless undone: "
                           f"{_label(row['artist'], row['album'])} → "
                           f"{best.get('artist')} - {best.get('title')}",
                           ref=row["path"], details={"candidate": best})

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
            # Look up first; a strong match is then applied after the undo window.
            self.enqueue(row["path"], "candidates", auto=True)

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

        if request.mode in ("apply", "asis", "auto") and row["report"] and not imported:
            # Keep the full candidate list from the lookup; only take over
            # what the attempt learned (duplicates, errors).
            previous = json.loads(row["report"])
            for old, new in zip(previous.get("tasks") or [], tasks):
                for key in ("duplicates", "duplicate_action", "chosen", "error"):
                    if new.get(key):
                        old[key] = new[key]
            previous["last_action"] = request.mode
            report, tasks = previous, previous.get("tasks") or []

        status = _status_from_report(report)
        db.update("inbox", "path", request.path, status=status,
                  report=json.dumps(report), error=None,
                  analyzed_fingerprint=fingerprint)

        best = _best_candidate(tasks)
        if (request.auto and settings.auto_apply and status == "review"
                and best and _strong_but_unapplied(report)):
            # beets' own per-file rule: each album/track gets its own strong
            # match; nothing else is touched.
            self.schedule(request.path, {"mode": "auto", "auto": True})
            db.log("info", "tagger",
                   f"Strong match ({round(best['similarity'] * 100, 1)}%), applying in "
                   f"{settings.undo_seconds}s unless undone: {label} → "
                   f"{best.get('artist')} - {best.get('title')}",
                   ref=request.path, details={"candidate": best})
            return
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

    def _archive(self, row: dict[str, Any]) -> None:
        """Move an inbox folder to the archive, keeping its relative path."""
        src = Path(row["path"])
        label = _label(row["artist"], row["album"])
        try:
            rel = src.relative_to(settings.inbox_dir)
        except ValueError:
            rel = Path(src.name)
        dest = settings.archive_dir / time.strftime("%Y-%m-%d") / rel
        n = 2
        while dest.exists():
            dest = dest.with_name(f"{rel.name} ({n})")
            n += 1
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except OSError as exc:
            db.update("inbox", "path", row["path"], status="error",
                      error=f"Archive failed: {exc}")
            db.log("error", "inbox", f"Couldn't archive {label}: {exc}", ref=row["path"])
            return
        db.execute("DELETE FROM inbox WHERE path = ?", (row["path"],))
        _remove_empty_dirs(str(src.parent))
        db.log("info", "inbox", f"Archived: {label} → {dest}", ref=row["path"],
               details={"archived_to": str(dest)})

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
    for task_path in request.task_paths:
        args += ["--task-path", task_path]
    args += ["--archive-dir", str(
        settings.archive_dir / time.strftime("%Y-%m-%d") / "replaced-in-library"
    )]
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
    return (
        report.get("mode") == "candidates"
        and not report.get("auto_undone")
        and not report.get("last_action")  # an apply was tried and didn't import
    ) and any(
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
