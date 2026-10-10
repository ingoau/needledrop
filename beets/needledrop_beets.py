"""Non-interactive beets import driver for Needledrop.

Runs inside the normal `beet` entry point (same config, plugins and library as
a manual `beet import`), adding one subcommand:

    needledrop-import --mode MODE [--search-id ID] [--candidate ID]
                      [--duplicate-action skip|keep|remove|merge]
                      [--singletons] --report FILE PATH

Modes:
    auto        apply the top candidate only when beets' recommendation is
                strong (the same rule as `import -q`); otherwise leave the
                files where they are
    candidates  look up and report candidates, never change anything
    apply       apply the candidate whose ID is --candidate
    asis        import with the files' existing tags

The report is a JSON document describing every task (album or singleton):
its candidates with similarity and penalties, the decision taken, duplicate
handling and whether it ended up in the library.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

from beets import config, importer, plugins, ui
from beets.autotag import AlbumMatch, Recommendation, TrackMatch
from beets.importer import Action, DuplicateAction
from beets.util import displayable_path

MAX_CANDIDATES = 8

DUPLICATE_ACTIONS = {
    "skip": DuplicateAction.SKIP,
    "keep": DuplicateAction.KEEP,
    "remove": DuplicateAction.REMOVE,
    "merge": DuplicateAction.MERGE,
}


def _num(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    return value


def _album_candidate(match: AlbumMatch) -> dict[str, Any]:
    info = match.info
    dist = match.distance
    return {
        "id": info.get("album_id"),
        "kind": "album",
        "similarity": _num(1 - float(dist)),
        "distance": _num(float(dist)),
        "penalties": [
            {"name": key, "weight": _num(value)} for key, value in dist.items()
        ],
        "artist": info.get("artist"),
        "title": info.get("album"),
        "year": info.get("year"),
        "data_source": info.get("data_source"),
        "media": info.get("media"),
        "mediums": info.get("mediums"),
        "label": info.get("label"),
        "country": info.get("country"),
        "albumdisambig": info.get("albumdisambig"),
        "disambig": match.disambig_string,
        "track_count": len(info.get("tracks") or []),
        "matched": len(match.mapping),
        "extra_items": len(match.extra_items),
        "missing_tracks": len(match.extra_tracks),
        "url": _source_url(info.get("data_source"), info.get("album_id"), "album"),
        "tracks": [
            _track_pair(item, track)
            for item, track in sorted(
                match.mapping.items(), key=lambda pair: pair[1].get("index") or 0
            )
        ],
        "extra_items_list": [
            {"file": _filename(item), "title": item.title, "track": item.track,
             "length": _num(item.length)}
            for item in match.extra_items
        ],
        "missing_tracks_list": [
            {"title": track.get("title"), "index": track.get("index"),
             "length": _num(track.get("length"))}
            for track in match.extra_tracks
        ],
    }


def _filename(item) -> str:
    return displayable_path(item.path).rsplit("/", 1)[-1]


def _track_pair(item, track) -> dict[str, Any]:
    """One file and the track it would become, for the diff view."""
    return {
        "item": _filename(item),
        "item_title": item.title,
        "item_artist": item.artist,
        "item_track": item.track,
        "item_disc": item.disc,
        "item_length": _num(item.length),
        "title": track.get("title"),
        "artist": track.get("artist"),
        "index": track.get("index"),
        "medium": track.get("medium"),
        "medium_index": track.get("medium_index"),
        "length": _num(track.get("length")),
    }


def _track_candidate(match: TrackMatch) -> dict[str, Any]:
    info = match.info
    dist = match.distance
    return {
        "id": info.get("track_id"),
        "kind": "track",
        "similarity": _num(1 - float(dist)),
        "distance": _num(float(dist)),
        "penalties": [
            {"name": key, "weight": _num(value)} for key, value in dist.items()
        ],
        "artist": info.get("artist"),
        "title": info.get("title"),
        "album": info.get("album"),
        "year": info.get("year"),
        "length": _num(info.get("length")),
        "data_source": info.get("data_source"),
        "disambig": match.disambig_string,
        "url": _source_url(info.get("data_source"), info.get("track_id"), "track"),
        "tracks": [_track_pair(match.item, info)],
    }


def _source_url(source: str | None, ident: str | None, kind: str) -> str | None:
    if not ident:
        return None
    if source == "MusicBrainz":
        path = "release" if kind == "album" else "recording"
        return f"https://musicbrainz.org/{path}/{ident}"
    if source == "Spotify":
        return f"https://open.spotify.com/{kind}/{ident}"
    return None


def _current(task) -> dict[str, Any]:
    """The files' existing tags, the left-hand side of the diff."""
    items = list(task.items) if task.is_album else [task.item]
    first = items[0] if items else None

    def common(field: str):
        values = {getattr(i, field, None) for i in items}
        return values.pop() if len(values) == 1 else None

    return {
        "artist": common("albumartist") or common("artist") if task.is_album
        else getattr(first, "artist", None),
        "album": common("album"),
        "title": None if task.is_album else getattr(first, "title", None),
        "year": common("year"),
        "label": common("label"),
        "track_count": len(items),
    }


class NeedledropSession(importer.ImportSession):
    def __init__(self, *args, mode: str, candidate: str | None,
                 duplicate_action: DuplicateAction,
                 task_paths: list[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode
        self.candidate = candidate
        # The file(s) the chosen candidate was matched for. A candidate is a
        # match for one task only; it must never be applied to the others.
        self.task_paths = set(task_paths or [])
        self.duplicate_action = duplicate_action
        self.tasks: dict[int, dict[str, Any]] = {}
        self.imported: list[dict[str, Any]] = []

    # Report helpers

    def _record(self, task) -> dict[str, Any]:
        entry = self.tasks.get(id(task))
        if entry is None:
            is_album = task.is_album
            entry = {
                "kind": "album" if is_album else "singleton",
                "paths": [displayable_path(p) for p in task.paths],
                "items": len(task.items) if is_album else 1,
                "cur_artist": getattr(task, "cur_artist", None)
                if is_album else task.item.artist,
                "cur_album": getattr(task, "cur_album", None)
                if is_album else task.item.title,
                "recommendation": rec.name if (rec := getattr(task, "rec", None)) is not None else None,
                "current": _current(task),
                "candidates": [],
                "decision": None,
                "chosen": None,
                "duplicates": [],
                "duplicate_action": None,
            }
            convert = _album_candidate if is_album else _track_candidate
            entry["candidates"] = [
                convert(m)
                for m in list(getattr(task, "candidates", None) or [])[:MAX_CANDIDATES]
            ]
            self.tasks[id(task)] = entry
        return entry

    def _decide(self, task):
        entry = self._record(task)
        candidates = list(task.candidates)

        if self.mode == "asis":
            entry["decision"] = "asis"
            return Action.ASIS

        if self.mode == "apply":
            if self.task_paths and not self.task_paths & {
                displayable_path(p) for p in task.paths
            }:
                entry["decision"] = "skip"
                return Action.SKIP
            for match in candidates:
                if match.info.id == self.candidate:
                    entry["decision"] = "apply"
                    entry["chosen"] = match.info.id
                    return match
            entry["decision"] = "skip"
            entry["error"] = f"candidate {self.candidate} not found"
            return Action.SKIP

        if self.mode == "auto" and task.rec == Recommendation.strong and candidates:
            entry["decision"] = "apply"
            entry["chosen"] = candidates[0].info.id
            return candidates[0]

        entry["decision"] = "skip"
        return Action.SKIP

    # ImportSession interface

    def should_resume(self, path):
        return False

    def choose_match(self, task):
        return self._decide(task)

    def choose_item(self, task):
        return self._decide(task)

    def get_duplicate_action(self, task, found_duplicates):
        entry = self._record(task)
        entry["duplicates"] = [
            {
                "id": dup.id,
                "summary": str(dup),
                "path": displayable_path(
                    dup.item_dir() if hasattr(dup, "item_dir") else dup.path
                ),
            }
            for dup in found_duplicates
        ]
        entry["duplicate_action"] = self.duplicate_action.name.lower()
        return self.duplicate_action


def _track_imports(session: NeedledropSession):
    def on_album(lib, album, **_):
        session.imported.append({
            "kind": "album",
            "id": album.id,
            "summary": f"{album.albumartist} - {album.album}",
            "paths": [displayable_path(i.path) for i in album.items()],
        })

    def on_item(lib, item, **_):
        session.imported.append({
            "kind": "item",
            "id": item.id,
            "summary": f"{item.artist} - {item.title}",
            "paths": [displayable_path(item.path)],
        })

    listeners = plugins.BeetsPlugin.listeners
    listeners["album_imported"].append(on_album)
    listeners["item_imported"].append(on_item)


def needledrop_import(lib, opts, args):
    if len(args) != 1:
        raise ui.UserError("exactly one path is required")
    path = args[0]
    if not os.path.exists(path):
        raise ui.UserError(f"no such path: {path}")
    if opts.mode not in ("auto", "candidates", "apply", "asis"):
        raise ui.UserError(f"unknown mode {opts.mode}")
    if opts.mode == "apply" and not opts.candidate:
        raise ui.UserError("--candidate is required with --mode apply")

    iconfig = config["import"]
    iconfig["quiet"] = True
    iconfig["timid"] = False
    iconfig["resume"] = False
    iconfig["incremental"] = False
    iconfig["singletons"] = bool(opts.singletons)
    iconfig["search_ids"] = opts.search_ids or []
    if opts.mode == "apply":
        iconfig["search_ids"] = [opts.candidate]
    if opts.mode == "asis":
        iconfig["autotag"] = False

    session = NeedledropSession(
        lib,
        None,
        [os.fsencode(path)],
        None,
        mode=opts.mode,
        candidate=opts.candidate,
        duplicate_action=DUPLICATE_ACTIONS[opts.duplicate_action],
        task_paths=opts.task_paths,
    )
    if opts.archive_dir:
        _archive_instead_of_delete(lib, opts.archive_dir)
    _track_imports(session)

    report: dict[str, Any] = {"path": path, "mode": opts.mode}
    try:
        session.run()
        plugins.send("import", lib=lib, paths=[os.fsencode(path)])
    except Exception as exc:  # report, then re-raise for a non-zero exit
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
        raise
    finally:
        report["tasks"] = list(session.tasks.values())
        report["imported"] = session.imported
        _write(opts.report, report)


def _write(target: str, data: dict[str, Any]) -> None:
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=str)
    os.replace(tmp, target)


command = ui.Subcommand(
    "needledrop-import", help="non-interactive import used by Needledrop"
)
command.parser.add_option("--mode", default="auto")
command.parser.add_option("--candidate", default=None)
command.parser.add_option(
    "--search-id", dest="search_ids", action="append", default=[]
)
command.parser.add_option(
    "--duplicate-action", default="skip", choices=list(DUPLICATE_ACTIONS)
)
command.parser.add_option("--singletons", action="store_true", default=False)
command.parser.add_option("--task-path", dest="task_paths", action="append", default=[])
command.parser.add_option("--archive-dir", default=None)
command.parser.add_option("--report", default="/dev/stdout")
command.func = needledrop_import


def _archive_instead_of_delete(lib, archive_dir: str) -> None:
    """Library files beets would delete (e.g. replaced duplicates) are moved
    into the archive instead, keeping their path below the library."""
    from beets import util

    original_remove = util.remove
    library_dir = os.fsdecode(lib.directory)

    def remove(path, soft=True):
        target = os.fsdecode(path)
        if os.path.isfile(target) and target.startswith(library_dir.rstrip("/") + "/"):
            dest = os.path.join(archive_dir, os.path.relpath(target, library_dir))
            base, ext = os.path.splitext(dest)
            n = 2
            while os.path.exists(dest):
                dest = f"{base} ({n}){ext}"
                n += 1
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            util.move(os.fsencode(target), os.fsencode(dest))
            print(f"needledrop: archived {target} -> {dest}", file=sys.stderr)
            return
        return original_remove(path, soft)

    util.remove = remove


def main() -> None:
    from beets.ui.commands import default_commands

    default_commands.append(command)
    ui.main(sys.argv[1:])


if __name__ == "__main__":
    main()
