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
        "id": info.album_id,
        "kind": "album",
        "similarity": _num(1 - float(dist)),
        "distance": _num(float(dist)),
        "penalties": [
            {"name": key, "weight": _num(value)} for key, value in dist.items()
        ],
        "artist": info.artist,
        "title": info.album,
        "year": info.year,
        "data_source": info.data_source,
        "media": info.media,
        "mediums": info.mediums,
        "label": info.label,
        "country": info.country,
        "albumdisambig": info.albumdisambig,
        "disambig": match.disambig_string,
        "track_count": len(info.tracks or []),
        "matched": len(match.mapping),
        "extra_items": len(match.extra_items),
        "missing_tracks": len(match.extra_tracks),
        "url": _source_url(info.data_source, info.album_id, "album"),
        "tracks": [
            {
                "item": displayable_path(item.path).rsplit("/", 1)[-1],
                "item_title": item.title,
                "title": track.title,
                "index": track.index,
                "length": _num(track.length),
            }
            for item, track in sorted(
                match.mapping.items(), key=lambda pair: pair[1].index or 0
            )
        ],
    }


def _track_candidate(match: TrackMatch) -> dict[str, Any]:
    info = match.info
    dist = match.distance
    return {
        "id": info.track_id,
        "kind": "track",
        "similarity": _num(1 - float(dist)),
        "distance": _num(float(dist)),
        "penalties": [
            {"name": key, "weight": _num(value)} for key, value in dist.items()
        ],
        "artist": info.artist,
        "title": info.title,
        "album": info.album,
        "year": info.year,
        "data_source": info.data_source,
        "disambig": match.disambig_string,
        "url": _source_url(info.data_source, info.track_id, "track"),
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


class NeedledropSession(importer.ImportSession):
    def __init__(self, *args, mode: str, candidate: str | None,
                 duplicate_action: DuplicateAction, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode
        self.candidate = candidate
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
    )
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
command.parser.add_option("--report", default="/dev/stdout")
command.func = needledrop_import


def main() -> None:
    from beets.ui.commands import default_commands

    default_commands.append(command)
    ui.main(sys.argv[1:])


if __name__ == "__main__":
    main()
