"""Find the Apple Music release an inbox folder came from.

Files ripped by Needledrop carry `apple_music_id` / `apple_music_album_id`
Vorbis comments; gamdl's .m4a files carry `cnID` (song) and `plID` (album)
atoms. Either is enough to look the release up again and work out which of
its tracks the folder is missing.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

import mutagen

from .config import AUDIO_EXTENSIONS


def _first(tags: Any, *keys: str) -> str | None:
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if value:
            value = value[0] if isinstance(value, list) else value
            return str(value)
    return None


def read_folder(path: str) -> dict[str, Any]:
    song_ids: list[str] = []
    album_ids: Counter[str] = Counter()
    files = 0
    without = 0
    for name in sorted(os.listdir(path)):
        if Path(name).suffix.lower() not in AUDIO_EXTENSIONS or name.startswith("."):
            continue
        files += 1
        try:
            audio = mutagen.File(os.path.join(path, name))
        except Exception:
            audio = None
        tags = getattr(audio, "tags", None) or {}
        song = _first(tags, "apple_music_id", "cnID")
        album = _first(tags, "apple_music_album_id", "plID")
        if song:
            song_ids.append(song)
        else:
            without += 1
        if album:
            album_ids[album] += 1
    return {
        "files": files,
        "song_ids": song_ids,
        "album_id": album_ids.most_common(1)[0][0] if album_ids else None,
        "files_without_id": without,
    }
