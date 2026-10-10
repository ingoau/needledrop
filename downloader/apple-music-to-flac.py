#!/srv/scripts/music/.venv/bin/python
"""Download Apple Music songs with Gamdl and safely produce verified FLACs."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

try:
    import structlog
    from gamdl import __version__ as GAMDL_VERSION
    from gamdl.api import AppleMusicApi
    from gamdl.api.wrapper import WrapperApi
    from gamdl.cli.cli import main as gamdl_command
    from gamdl.cli.config_file import ConfigFile
    from gamdl.cli.interactive_prompts import InteractivePrompts
    from gamdl.downloader import (
        AppleMusicBaseDownloader,
        AppleMusicDownloader,
        AppleMusicMusicVideoDownloader,
        AppleMusicSongDownloader,
        AppleMusicUploadedVideoDownloader,
    )
    from gamdl.interface import (
        AppleMusicBaseInterface,
        AppleMusicInterface,
        AppleMusicMusicVideoInterface,
        AppleMusicSongInterface,
        AppleMusicUploadedVideoInterface,
    )
    from gamdl.interface.enums import ArtistMediaType, SongCodec, SyncedLyricsFormat
    from gamdl.interface.exceptions import (
        GamdlInterfaceDecryptionNotAvailableError,
        GamdlInterfaceFormatNotAvailableError,
        GamdlInterfaceMediaNotAllowedError,
        GamdlInterfaceMediaNotStreamableError,
        GamdlInterfaceUrlParseError,
    )
    from gamdl.interface.types import Lyrics
except ImportError as exc:  # pragma: no cover - exercised only on a broken install
    raise SystemExit(f"Gamdl's Python package is required: {exc}") from exc


LOG = logging.getLogger("apple-music-to-flac")
DEFAULT_CONFIG = Path(__file__).with_suffix(".ini")
DEFAULT_FREACCMD = Path("/home/ingowolf/.local/bin/freaccmd")
DEFAULT_LOG_DIR = Path(__file__).parent / "logs"
DEFAULT_HISTORY_DB = Path(__file__).parent / "download-history.sqlite3"
JSONL_SCHEMA_VERSION = 3
HISTORY_SCHEMA_VERSION = 1
MINIMUM_FREE_BYTES = 512 * 1024 * 1024
FILENAME_BYTE_LIMIT = 240
APPLE_MUSIC_ID_TAG = "apple_music_id"
EXPECTED_DURATION_TOLERANCE_SECONDS = Fraction(2, 1)

CORE_METADATA_TAGS = {
    "album",
    "album_artist",
    "artist",
    "compilation",
    "composer",
    "copyright",
    "date",
    "disc",
    "gapless_playback",
    "genre",
    "lyrics",
    "media_type",
    "rating",
    "sort_album",
    "sort_artist",
    "sort_composer",
    "sort_name",
    "title",
    "track",
}


class PipelineError(RuntimeError):
    """A checked download, conversion, or verification step failed."""


class IdentityMismatch(PipelineError):
    """An existing path belongs to a different Apple Music track."""


class JsonlRunLogger:
    """Append-only, flushed structured recovery log for one pipeline run."""

    def __init__(self, path: Path | None = None) -> None:
        self.run_id = uuid.uuid4().hex
        if path is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = DEFAULT_LOG_DIR / (
                f"apple-music-to-flac-{timestamp}-{os.getpid()}-"
                f"{self.run_id[:8]}.jsonl"
            )
        self.path = path.expanduser().resolve()
        self.degraded = False
        self._warning_emitted = False
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        self.file = os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)

    @staticmethod
    def timestamp() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def emit(self, event: str, *, durable: bool = False, **fields: Any) -> None:
        if self.degraded:
            return
        record = {
            "schema_version": JSONL_SCHEMA_VERSION,
            "timestamp": self.timestamp(),
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        try:
            self.file.write(
                json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                + "\n"
            )
            self.file.flush()
            if durable:
                os.fsync(self.file.fileno())
        except OSError as exc:
            self.degraded = True
            if not self._warning_emitted:
                LOG.error("JSONL logging stopped after an I/O error: %s", exc)
                self._warning_emitted = True

    def close(self) -> None:
        if self.file.closed:
            return
        try:
            if not self.degraded:
                self.file.flush()
                os.fsync(self.file.fileno())
        except OSError as exc:
            LOG.error("Could not finish JSONL log %s: %s", self.path, exc)
        finally:
            self.file.close()


@dataclass(frozen=True)
class HistoryMatch:
    media_id: str
    isrc: str | None
    album_id: str | None
    outcome: str | None
    output_path: str | None
    completed_at: str
    matched_by: str


class DownloadHistory:
    """Durable ledger of audio that completed the verified download pipeline."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        existed = self.path.exists()
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()
        if not existed:
            os.chmod(self.path, 0o600)

    @staticmethod
    def timestamp() -> str:
        return JsonlRunLogger.timestamp()

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS history_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tracks (
                    media_id TEXT PRIMARY KEY,
                    isrc TEXT,
                    album_id TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    audio_status TEXT NOT NULL,
                    lyrics_status TEXT NOT NULL,
                    codec TEXT,
                    outcome TEXT,
                    output_path TEXT,
                    output_size INTEGER,
                    pcm_sha256 TEXT,
                    source_url TEXT,
                    first_completed_at TEXT NOT NULL,
                    last_completed_at TEXT NOT NULL,
                    last_run_id TEXT,
                    seeded_from_log TEXT
                );

                CREATE INDEX IF NOT EXISTS tracks_isrc_complete
                    ON tracks(isrc, audio_status);
                CREATE INDEX IF NOT EXISTS tracks_album_complete
                    ON tracks(album_id, audio_status);

                CREATE TABLE IF NOT EXISTS albums (
                    album_id TEXT PRIMARY KEY,
                    storefront TEXT,
                    title TEXT,
                    artist TEXT,
                    track_ids_json TEXT NOT NULL,
                    track_count INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    last_run_id TEXT,
                    source_url TEXT
                );

                CREATE TABLE IF NOT EXISTS seeded_logs (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    imported_at TEXT NOT NULL,
                    completed_events INTEGER NOT NULL,
                    malformed_lines INTEGER NOT NULL
                );
                """
            )
            row = self.connection.execute(
                "SELECT value FROM history_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row and int(row["value"]) != HISTORY_SCHEMA_VERSION:
                raise PipelineError(
                    "Unsupported download-history schema version "
                    f"{row['value']}; expected {HISTORY_SCHEMA_VERSION}"
                )
            self.connection.execute(
                """
                INSERT INTO history_metadata(key, value)
                VALUES('schema_version', ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (str(HISTORY_SCHEMA_VERSION),),
            )

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def codec_from_outcome(outcome: str | None, output_path: str | None) -> str | None:
        if outcome == "retained_aac":
            return "aac"
        if output_path:
            suffix = Path(output_path).suffix.casefold()
            if suffix == ".flac":
                return "flac"
            if suffix == ".m4a":
                return "aac"
        return None

    def _upsert_track(
        self,
        *,
        media_id: str,
        isrc: str | None,
        album_id: str | None,
        title: str | None,
        artist: str | None,
        album: str | None,
        lyrics_status: str,
        codec: str | None,
        outcome: str | None,
        output_path: str | None,
        output_size: int | None,
        pcm_sha256: str | None,
        source_url: str | None,
        completed_at: str,
        run_id: str | None,
        seeded_from_log: str | None,
    ) -> None:
        isrc = isrc.strip().upper() if isrc and isrc.strip() else None
        self.connection.execute(
            """
            INSERT INTO tracks(
                media_id, isrc, album_id, title, artist, album,
                audio_status, lyrics_status, codec, outcome, output_path,
                output_size, pcm_sha256, source_url, first_completed_at,
                last_completed_at, last_run_id, seeded_from_log
            ) VALUES(?, ?, ?, ?, ?, ?, 'complete', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_id) DO UPDATE SET
                isrc = COALESCE(NULLIF(excluded.isrc, ''), tracks.isrc),
                album_id = COALESCE(NULLIF(excluded.album_id, ''), tracks.album_id),
                title = COALESCE(NULLIF(excluded.title, ''), tracks.title),
                artist = COALESCE(NULLIF(excluded.artist, ''), tracks.artist),
                album = COALESCE(NULLIF(excluded.album, ''), tracks.album),
                audio_status = 'complete',
                lyrics_status = CASE
                    WHEN excluded.lyrics_status = 'complete' THEN 'complete'
                    ELSE tracks.lyrics_status
                END,
                codec = COALESCE(NULLIF(excluded.codec, ''), tracks.codec),
                outcome = COALESCE(NULLIF(excluded.outcome, ''), tracks.outcome),
                output_path = COALESCE(
                    NULLIF(excluded.output_path, ''), tracks.output_path
                ),
                output_size = COALESCE(excluded.output_size, tracks.output_size),
                pcm_sha256 = COALESCE(
                    NULLIF(excluded.pcm_sha256, ''), tracks.pcm_sha256
                ),
                source_url = COALESCE(
                    NULLIF(excluded.source_url, ''), tracks.source_url
                ),
                first_completed_at = MIN(
                    tracks.first_completed_at, excluded.first_completed_at
                ),
                last_completed_at = MAX(
                    tracks.last_completed_at, excluded.last_completed_at
                ),
                last_run_id = COALESCE(
                    NULLIF(excluded.last_run_id, ''), tracks.last_run_id
                ),
                seeded_from_log = COALESCE(
                    tracks.seeded_from_log, excluded.seeded_from_log
                )
            """,
            (
                media_id,
                isrc,
                album_id,
                title,
                artist,
                album,
                lyrics_status,
                codec,
                outcome,
                output_path,
                output_size,
                pcm_sha256,
                source_url,
                completed_at,
                completed_at,
                run_id,
                seeded_from_log,
            ),
        )

    def record_completed(
        self,
        *,
        media_id: str,
        isrc: str | None,
        album_id: str | None,
        title: str | None,
        artist: str | None,
        album: str | None,
        lyrics_status: str,
        codec: str | None,
        outcome: str,
        output_path: str,
        output_size: int,
        pcm_sha256: str | None,
        source_url: str | None,
        run_id: str,
    ) -> None:
        with self.connection:
            self._upsert_track(
                media_id=media_id,
                isrc=isrc,
                album_id=album_id,
                title=title,
                artist=artist,
                album=album,
                lyrics_status=lyrics_status,
                codec=codec,
                outcome=outcome,
                output_path=output_path,
                output_size=output_size,
                pcm_sha256=pcm_sha256,
                source_url=source_url,
                completed_at=self.timestamp(),
                run_id=run_id,
                seeded_from_log=None,
            )

    def seed_from_logs(self, log_directory: Path) -> dict[str, int]:
        result = {
            "logs_seen": 0,
            "logs_imported": 0,
            "completed_events": 0,
            "malformed_lines": 0,
        }
        if not log_directory.is_dir():
            return result

        for path in sorted(log_directory.glob("*.jsonl")):
            result["logs_seen"] += 1
            stat = path.stat()
            previous = self.connection.execute(
                "SELECT size, mtime_ns FROM seeded_logs WHERE path = ?",
                (str(path.resolve()),),
            ).fetchone()
            if (
                previous
                and previous["size"] == stat.st_size
                and previous["mtime_ns"] == stat.st_mtime_ns
            ):
                continue

            completed_events = 0
            malformed_lines = 0
            with self.connection:
                with path.open("r", encoding="utf-8") as file:
                    for line in file:
                        try:
                            record = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            malformed_lines += 1
                            continue
                        if record.get("event") != "track_completed":
                            continue
                        media_id = str(record.get("media_id") or "")
                        if not media_id:
                            continue
                        output_path = record.get("output_path")
                        outcome = record.get("outcome")
                        self._upsert_track(
                            media_id=media_id,
                            isrc=record.get("isrc"),
                            album_id=record.get("album_id"),
                            title=record.get("title"),
                            artist=record.get("artist"),
                            album=record.get("album"),
                            lyrics_status=(
                                "failed"
                                if record.get("finalization_failed")
                                else "complete"
                            ),
                            codec=self.codec_from_outcome(outcome, output_path),
                            outcome=outcome,
                            output_path=output_path,
                            output_size=record.get("output_size"),
                            pcm_sha256=record.get("pcm_sha256"),
                            source_url=record.get("url"),
                            completed_at=(
                                record.get("timestamp") or self.timestamp()
                            ),
                            run_id=record.get("run_id"),
                            seeded_from_log=str(path.resolve()),
                        )
                        completed_events += 1
                self.connection.execute(
                    """
                    INSERT INTO seeded_logs(
                        path, size, mtime_ns, imported_at,
                        completed_events, malformed_lines
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        imported_at = excluded.imported_at,
                        completed_events = excluded.completed_events,
                        malformed_lines = excluded.malformed_lines
                    """,
                    (
                        str(path.resolve()),
                        stat.st_size,
                        stat.st_mtime_ns,
                        self.timestamp(),
                        completed_events,
                        malformed_lines,
                    ),
                )
            result["logs_imported"] += 1
            result["completed_events"] += completed_events
            result["malformed_lines"] += malformed_lines
        return result

    def find_completed(
        self,
        media_id: str,
        isrc: str | None,
        expected_path: Path | str | None = None,
    ) -> HistoryMatch | None:
        row = self.connection.execute(
            """
            SELECT * FROM tracks
            WHERE media_id = ? AND audio_status = 'complete'
            """,
            (media_id,),
        ).fetchone()
        matched_by = "media_id"
        if not row and isrc and expected_path is not None:
            # An ISRC names a recording, not a release, so one ISRC covers the
            # album, its deluxe edition, the single, and every compilation that
            # carries the track. Accept the match only when the earlier file
            # sits at the exact path this item would write to. That is the
            # re-issue case this fallback exists for: the audio is already
            # there under an older media ID, and re-downloading it would trip
            # the identity check and quarantine a good file. An ISRC hit
            # anywhere else belongs to a different release and needs its own
            # copy.
            isrc = isrc.strip().upper()
            wanted = os.path.normpath(str(expected_path))
            candidates = self.connection.execute(
                """
                SELECT * FROM tracks
                WHERE isrc = ? AND audio_status = 'complete'
                ORDER BY last_completed_at DESC
                """,
                (isrc,),
            ).fetchall()
            row = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate["output_path"]
                    and os.path.normpath(candidate["output_path"]) == wanted
                ),
                None,
            )
            matched_by = "isrc"
        if not row:
            return None
        return HistoryMatch(
            media_id=row["media_id"],
            isrc=row["isrc"],
            album_id=row["album_id"],
            outcome=row["outcome"],
            output_path=row["output_path"],
            completed_at=row["last_completed_at"],
            matched_by=matched_by,
        )

    def record_coverage_alias(
        self,
        *,
        media_id: str,
        isrc: str,
        album_id: str | None,
        title: str | None,
        artist: str | None,
        album: str | None,
        match: HistoryMatch,
        source_url: str,
        run_id: str,
    ) -> None:
        with self.connection:
            self._upsert_track(
                media_id=media_id,
                isrc=isrc,
                album_id=album_id,
                title=title,
                artist=artist,
                album=album,
                lyrics_status="complete",
                codec=None,
                outcome=f"covered_by_isrc:{match.media_id}",
                output_path=match.output_path,
                output_size=None,
                pcm_sha256=None,
                source_url=source_url,
                completed_at=self.timestamp(),
                run_id=run_id,
                seeded_from_log=None,
            )

    def completed_album(self, album_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM albums WHERE album_id = ?",
            (album_id,),
        ).fetchone()

    def mark_album_complete(
        self,
        *,
        album_id: str,
        storefront: str,
        title: str | None,
        artist: str | None,
        track_ids: set[str],
        run_id: str,
        source_url: str,
    ) -> bool:
        if not track_ids:
            return False
        placeholders = ",".join("?" for _ in track_ids)
        completed = self.connection.execute(
            f"""
            SELECT COUNT(*) AS count FROM tracks AS track
            WHERE track.media_id IN ({placeholders})
              AND track.audio_status = 'complete'
              AND NOT EXISTS (
                  SELECT 1 FROM tracks AS source
                  WHERE track.outcome LIKE 'covered_by_isrc:%'
                    AND source.media_id = substr(track.outcome, 17)
                    AND source.album_id IS NOT track.album_id
              )
            """,
            tuple(sorted(track_ids)),
        ).fetchone()["count"]
        if completed != len(track_ids):
            return False
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO albums(
                    album_id, storefront, title, artist, track_ids_json,
                    track_count, completed_at, last_run_id, source_url
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(album_id) DO UPDATE SET
                    storefront = excluded.storefront,
                    title = COALESCE(NULLIF(excluded.title, ''), albums.title),
                    artist = COALESCE(NULLIF(excluded.artist, ''), albums.artist),
                    track_ids_json = excluded.track_ids_json,
                    track_count = excluded.track_count,
                    completed_at = excluded.completed_at,
                    last_run_id = excluded.last_run_id,
                    source_url = excluded.source_url
                """,
                (
                    album_id,
                    storefront,
                    title,
                    artist,
                    json.dumps(sorted(track_ids)),
                    len(track_ids),
                    self.timestamp(),
                    run_id,
                    source_url,
                ),
            )
        return True

    def repair_isrc_aliases(self) -> dict[str, int]:
        """Drop cross-release ISRC skips and reopen the albums they closed.

        Before the ISRC fallback was scoped to the output path, a track that
        appeared on two releases was recorded complete on the second one while
        pointing at the first one's file. Those rows also satisfy the later
        media-ID lookup, so the missing audio never downloads on a re-run.
        """
        aliases = self.connection.execute(
            """
            SELECT media_id, album_id, outcome FROM tracks
            WHERE outcome LIKE 'covered_by_isrc:%'
            """
        ).fetchall()
        stale: set[str] = set()
        for alias in aliases:
            source_id = alias["outcome"].split(":", 1)[1]
            source = self.connection.execute(
                "SELECT album_id FROM tracks WHERE media_id = ?",
                (source_id,),
            ).fetchone()
            if source is None or source["album_id"] != alias["album_id"]:
                stale.add(alias["media_id"])
        if not stale:
            return {"tracks_removed": 0, "albums_reopened": 0}
        albums = self.connection.execute(
            "SELECT album_id, track_ids_json FROM albums"
        ).fetchall()
        reopened = [
            album["album_id"]
            for album in albums
            if stale & set(json.loads(album["track_ids_json"]))
        ]
        with self.connection:
            self.connection.executemany(
                "DELETE FROM tracks WHERE media_id = ?",
                [(media_id,) for media_id in sorted(stale)],
            )
            self.connection.executemany(
                "DELETE FROM albums WHERE album_id = ?",
                [(album_id,) for album_id in reopened],
            )
        return {"tracks_removed": len(stale), "albums_reopened": len(reopened)}

    def status(self) -> dict[str, int]:
        return {
            "tracks": self.connection.execute(
                "SELECT COUNT(*) FROM tracks WHERE audio_status = 'complete'"
            ).fetchone()[0],
            "albums": self.connection.execute(
                "SELECT COUNT(*) FROM albums"
            ).fetchone()[0],
            "seeded_logs": self.connection.execute(
                "SELECT COUNT(*) FROM seeded_logs"
            ).fetchone()[0],
        }


class SyllableLyricsAppleMusicApi(AppleMusicApi):
    """Apple Music API client that does not request Gamdl's lyrics relationship."""

    async def get_song(
        self,
        song_id: str,
        extend: str = "extendedAssetUrls",
        include: str = "albums",
    ) -> dict:
        return await super().get_song(
            song_id=song_id,
            extend=extend,
            include=include,
        )


class SyllableLyricsSongInterface(AppleMusicSongInterface):
    """Defer all lyric acquisition to Apple's syllable-lyrics endpoint."""

    async def get_lyrics(self, song_metadata: dict) -> None:
        return None


@dataclass(frozen=True)
class MediaProbe:
    codec: str
    sample_rate: str
    channels: int
    time_base: str
    duration_ts: int
    bits_per_sample: int
    tags: dict[str, str]
    has_artwork: bool

    @property
    def duration_seconds(self) -> Fraction:
        return Fraction(self.duration_ts) * Fraction(self.time_base)

    @property
    def sample_count(self) -> int:
        return round(self.duration_seconds * int(self.sample_rate))

    @property
    def audio_shape(self) -> tuple[str, int, int, int]:
        return (
            self.sample_rate,
            self.channels,
            self.bits_per_sample,
            self.sample_count,
        )


@dataclass
class Statistics:
    downloaded: int = 0
    converted: int = 0
    aac_retained: int = 0
    skipped: int = 0
    history_skipped: int = 0
    failed: int = 0
    finalization_failed: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download Apple Music song, album, playlist, or artist URLs as "
            "verified FLAC files, retaining AAC/M4A when lossless audio is unavailable."
        )
    )
    parser.add_argument("urls", nargs="*", help="Apple Music URLs")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Gamdl configuration file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Override Gamdl's output_path",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retries after the first attempt for each track (default: 3)",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        help="Initial retry delay in seconds; exponential backoff is used",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download and replace existing output without downgrading FLAC to AAC",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Ignore all completed download-history records; combine with --overwrite "
            "to replace files that remain in the output directory"
        ),
    )
    parser.add_argument(
        "--force-track",
        action="append",
        default=[],
        metavar="APPLE_TRACK_ID",
        help="Ignore history for this Apple Music track ID; may be repeated",
    )
    parser.add_argument(
        "--force-album",
        action="append",
        default=[],
        metavar="APPLE_ALBUM_ID",
        help="Ignore history for every track on this Apple Music album ID; may be repeated",
    )
    parser.add_argument(
        "--history-db",
        type=Path,
        default=DEFAULT_HISTORY_DB,
        help=f"SQLite download-history database (default: {DEFAULT_HISTORY_DB})",
    )
    parser.add_argument(
        "--repair-isrc-aliases",
        action="store_true",
        help=(
            "Delete download-history rows that skipped a track because the same "
            "recording had already been downloaded from a different release, "
            "reopen the albums those rows marked complete, and exit"
        ),
    )
    parser.add_argument(
        "--seed-history-only",
        action="store_true",
        help="Import successful track_completed events from JSONL logs and exit",
    )
    parser.add_argument(
        "--audio-policy",
        choices=("prefer-lossless", "lossless-only", "aac-only"),
        default="prefer-lossless",
        help=(
            "Codec selection: ALAC with AAC fallback, ALAC only, or AAC only "
            "(default: prefer-lossless)"
        ),
    )
    parser.add_argument(
        "--artist-auto-select",
        choices=[item.value for item in ArtistMediaType],
        help="Automatically select this content category for artist URLs",
    )
    parser.add_argument(
        "--playlist-albums",
        action="store_true",
        help=(
            "For each playlist URL, download every track from each unique album "
            "represented in the playlist"
        ),
    )
    parser.add_argument(
        "--lyrics-format",
        choices=[item.value for item in SyncedLyricsFormat],
        default=SyncedLyricsFormat.TTML.value,
        help="Synced-lyrics sidecar format (default: ttml)",
    )
    parser.add_argument(
        "--freaccmd",
        type=Path,
        default=DEFAULT_FREACCMD,
        help=f"fre:ac command-line launcher (default: {DEFAULT_FREACCMD})",
    )
    parser.add_argument(
        "--jsonl-log",
        type=Path,
        help=(
            "Override the JSONL run-log path; by default a unique file is created "
            f"under {DEFAULT_LOG_DIR}"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.urls and not args.seed_history_only and not args.repair_isrc_aliases:
        parser.error("at least one Apple Music URL is required")
    if args.retries < 0:
        parser.error("--retries must be zero or greater")
    if args.retry_delay < 0:
        parser.error("--retry-delay must be zero or greater")
    for option, values in (
        ("--force-track", args.force_track),
        ("--force-album", args.force_album),
    ):
        invalid = [value for value in values if not str(value).isdecimal()]
        if invalid:
            parser.error(f"{option} requires a numeric Apple Music ID: {invalid[0]}")
    return args


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.WARNING
        )
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


def load_gamdl_config(config_path: Path, urls: list[str]) -> Any:
    if not config_path.is_file():
        raise PipelineError(f"Gamdl config does not exist: {config_path}")

    # Gamdl's ConfigFile class relies on the Click command context for its own
    # enum, CSV, path, and boolean conversions. Using it avoids maintaining a
    # second, subtly different parser for Gamdl's configuration format.
    context = gamdl_command.make_context(
        "gamdl",
        ["--config-path", str(config_path), *urls],
    )
    with context:
        return ConfigFile(str(config_path)).load()


def resolve_executable(value: str | Path, label: str) -> str:
    value_str = str(value)
    resolved = shutil.which(value_str)
    if not resolved and Path(value_str).is_file() and os.access(value_str, os.X_OK):
        resolved = str(Path(value_str).resolve())
    if not resolved:
        raise PipelineError(f"Required {label} executable was not found: {value}")
    return resolved


def resolve_matching_ffprobe(ffmpeg: str) -> str:
    sibling = Path(ffmpeg).resolve().with_name("ffprobe")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return resolve_executable("ffprobe", "FFprobe")


def normalize_metadata_value(value: Any) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFC", str(value)).replace("\r\n", "\n")
    return "\n".join(line.strip() for line in normalized.strip().split("\n"))


def metadata_values_equal(key: str, expected: Any, actual: Any) -> bool:
    expected_value = normalize_metadata_value(expected)
    actual_value = normalize_metadata_value(actual)
    if key in {"disc", "track"}:
        expected_number = re.match(r"\d+", expected_value)
        actual_number = re.match(r"\d+", actual_value)
        if expected_number and actual_number:
            return expected_number.group() == actual_number.group()
    if key == "date" and expected_value and actual_value:
        try:
            expected_date = datetime.fromisoformat(expected_value.replace("Z", "+00:00"))
            actual_date = datetime.fromisoformat(actual_value.replace("Z", "+00:00"))
            return expected_date == actual_date
        except ValueError:
            pass
    return expected_value == actual_value


def truncate_utf8(value: str, max_characters: int | None, max_bytes: int) -> str:
    if max_characters is not None:
        value = value[:max_characters]
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def shorten_component(
    component: str,
    max_characters: int | None,
    max_bytes: int,
    *,
    preserve_extension: bool,
    identity_suffix: str = "",
) -> str:
    extension = Path(component).suffix if preserve_extension else ""
    stem = component[: -len(extension)] if extension else component
    reserved_characters = len(extension) + len(identity_suffix)
    reserved_bytes = len((identity_suffix + extension).encode("utf-8"))
    available_characters = (
        max(1, max_characters - reserved_characters)
        if max_characters is not None
        else None
    )
    available_bytes = max_bytes - reserved_bytes
    if available_bytes < 1:
        raise PipelineError(f"Filename suffix is too long for this filesystem: {component}")
    stem = truncate_utf8(stem, available_characters, available_bytes).rstrip(" .")
    return f"{stem or '_'}{identity_suffix}{extension}"


def shorten_path(path: Path, root: Path, max_characters: int | None) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PipelineError(f"Output path escaped the configured output root: {path}") from exc
    try:
        filesystem_limit = os.pathconf(root, "PC_NAME_MAX")
    except (OSError, ValueError):
        filesystem_limit = 255
    max_bytes = min(filesystem_limit, FILENAME_BYTE_LIMIT)
    parts = [
        shorten_component(
            part,
            max_characters,
            max_bytes,
            preserve_extension=index == len(relative.parts) - 1,
        )
        for index, part in enumerate(relative.parts)
    ]
    result = root.joinpath(*parts)
    try:
        path_limit = os.pathconf(root, "PC_PATH_MAX")
    except (OSError, ValueError):
        path_limit = 4096
    if len(os.fsencode(result)) >= path_limit:
        raise PipelineError(f"Output path exceeds the filesystem limit: {result}")
    return result


def collision_path(path: Path, media_id: str, max_characters: int | None) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9]", "", media_id)
    if not safe_id or len(safe_id) > 16:
        safe_id = hashlib.sha256(media_id.encode("utf-8")).hexdigest()[:12]
    marker = f" [am-{safe_id}]"
    try:
        filesystem_limit = os.pathconf(path.parent, "PC_NAME_MAX")
    except (OSError, ValueError):
        filesystem_limit = 255
    name = shorten_component(
        path.name,
        max_characters,
        min(filesystem_limit, FILENAME_BYTE_LIMIT),
        preserve_extension=True,
        identity_suffix=marker,
    )
    return path.with_name(name)


class TrackFileLock:
    """Cross-process advisory lock for one Apple Music media ID."""

    def __init__(self, output_root: Path, media_id: str) -> None:
        digest = hashlib.sha256(media_id.encode("utf-8")).hexdigest()
        self.path = output_root / ".apple-music-to-flac-locks" / f"{digest}.lock"
        self.descriptor: int | None = None

    def _open(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)

    async def __aenter__(self) -> TrackFileLock:
        self._open()
        assert self.descriptor is not None
        try:
            while True:
                try:
                    fcntl.flock(
                        self.descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.2)
            os.ftruncate(self.descriptor, 0)
            os.write(self.descriptor, f"{os.getpid()}\n".encode())
            return self
        except BaseException:
            os.close(self.descriptor)
            self.descriptor = None
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.descriptor is None:
            return
        fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        os.close(self.descriptor)
        self.descriptor = None


NONRETRYABLE_RESOLUTION_ERRORS = (
    GamdlInterfaceDecryptionNotAvailableError,
    GamdlInterfaceFormatNotAvailableError,
    GamdlInterfaceMediaNotAllowedError,
    GamdlInterfaceMediaNotStreamableError,
    GamdlInterfaceUrlParseError,
)


async def run_checked(
    *arguments: str | Path,
    env: dict[str, str] | None = None,
) -> str:
    command = [str(argument) for argument in arguments]
    LOG.debug("Running: %s", " ".join(command))
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await process.communicate()
    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        detail = stderr_text or stdout_text or "no diagnostic output"
        raise PipelineError(
            f"{Path(command[0]).name} exited with {process.returncode}: {detail}"
        )
    if stderr_text:
        LOG.debug("%s stderr: %s", Path(command[0]).name, stderr_text)
    return stdout_text


def verify_writable_directory(path: Path, label: str) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".preflight-", dir=path):
            pass
    except OSError as exc:
        raise PipelineError(f"{label} is not writable: {path}: {exc}") from exc


async def run_preflight(
    args: argparse.Namespace,
    config: Any,
    transcoder: FlacTranscoder,
) -> dict[str, Any]:
    if (
        any("/artist/" in url.lower() for url in args.urls)
        and not config.artist_auto_select
        and not sys.stdin.isatty()
    ):
        raise PipelineError(
            "Artist URLs need --artist-auto-select when standard input is not a terminal"
        )

    output_root = Path(config.output_path).expanduser().resolve()
    temp_root = Path(config.temp_path).expanduser().resolve()
    verify_writable_directory(output_root, "Output directory")
    verify_writable_directory(temp_root, "Gamdl temporary directory")

    checked_devices: set[int] = set()
    free_space: dict[str, int] = {}
    for path in (output_root, temp_root):
        device = path.stat().st_dev
        if device in checked_devices:
            continue
        checked_devices.add(device)
        free_bytes = shutil.disk_usage(path).free
        free_space[str(path)] = free_bytes
        if free_bytes < MINIMUM_FREE_BYTES:
            raise PipelineError(
                f"Less than 512 MiB is free on the filesystem containing {path}"
            )

    ffmpeg_version = (await run_checked(transcoder.ffmpeg, "-version")).splitlines()[0]
    ffprobe_version = (await run_checked(transcoder.ffprobe, "-version")).splitlines()[0]
    ffmpeg_match = re.search(r"version\s+(\d+)", ffmpeg_version)
    ffprobe_match = re.search(r"version\s+(\d+)", ffprobe_version)
    if not ffmpeg_match or not ffprobe_match:
        raise PipelineError("Could not determine FFmpeg and FFprobe versions")
    if ffmpeg_match.group(1) != ffprobe_match.group(1):
        raise PipelineError(
            f"FFmpeg and FFprobe major versions differ: {ffmpeg_version}; "
            f"{ffprobe_version}"
        )

    freac_output = await run_checked(transcoder.freaccmd, "--help=flac")
    if "fre:ac" not in freac_output or "FLAC Audio Encoder" not in freac_output:
        raise PipelineError("fre:ac is installed but its FLAC encoder is unavailable")

    download_mode = getattr(config.download_mode, "value", str(config.download_mode))
    nm3u8dlre = None
    if str(download_mode).lower() == "nm3u8dlre":
        nm3u8dlre = resolve_executable(config.nm3u8dlre_path, "N_m3u8DL-RE")

    return {
        "output_path": str(output_root),
        "temp_path": str(temp_root),
        "free_bytes": free_space,
        "ffmpeg_version": ffmpeg_version,
        "ffprobe_version": ffprobe_version,
        "freac_available": True,
        "nm3u8dlre_path": nm3u8dlre,
    }


class FlacTranscoder:
    def __init__(self, freaccmd: Path, ffmpeg: str, ffprobe: str) -> None:
        self.freaccmd = resolve_executable(freaccmd, "fre:ac")
        self.ffmpeg = resolve_executable(ffmpeg, "FFmpeg")
        self.ffprobe = resolve_executable(ffprobe, "FFprobe")

    async def probe(self, path: Path) -> MediaProbe:
        output = await run_checked(
            self.ffprobe,
            "-v",
            "error",
            "-show_entries",
            (
                "stream=codec_name,codec_type,sample_rate,channels,time_base,"
                "duration_ts,bits_per_sample,bits_per_raw_sample,disposition:format_tags"
            ),
            "-of",
            "json",
            path,
        )
        try:
            data = json.loads(output)
            streams = data.get("streams", [])
            audio = next(stream for stream in streams if stream.get("codec_type") == "audio")
            duration_ts = audio["duration_ts"]
            sample_rate = audio["sample_rate"]
            channels = audio["channels"]
            time_base = audio["time_base"]
            bits_per_sample = int(
                audio.get("bits_per_raw_sample") or audio.get("bits_per_sample") or 0
            )
        except (json.JSONDecodeError, KeyError, StopIteration, TypeError) as exc:
            raise PipelineError(f"Could not read complete audio information from {path}") from exc

        tags = {
            str(key).lower(): str(value)
            for key, value in data.get("format", {}).get("tags", {}).items()
        }
        has_artwork = any(stream.get("codec_type") == "video" for stream in streams)
        return MediaProbe(
            codec=str(audio.get("codec_name", "")),
            sample_rate=str(sample_rate),
            channels=int(channels),
            time_base=str(time_base),
            duration_ts=int(duration_ts),
            bits_per_sample=bits_per_sample,
            tags=tags,
            has_artwork=has_artwork,
        )

    async def pcm_hash(self, path: Path) -> str:
        output = await run_checked(
            self.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        )
        if not output.startswith("SHA256="):
            raise PipelineError(f"FFmpeg did not return a PCM hash for {path}")
        return output

    @staticmethod
    def verify_metadata(source: MediaProbe, output: MediaProbe, path: Path) -> None:
        mismatches = []
        for key in sorted(CORE_METADATA_TAGS):
            source_value = normalize_metadata_value(source.tags.get(key))
            output_value = normalize_metadata_value(output.tags.get(key))
            # MP4 can explicitly store an empty text tag, while FLAC/FFmpeg
            # normally omits that tag. Both representations mean "no value"
            # and must not turn an otherwise valid conversion into a failure.
            if not source_value and not output_value:
                continue
            if key in source.tags and not metadata_values_equal(
                key, source_value, output_value
            ):
                mismatches.append(key)
        if source.has_artwork and not output.has_artwork:
            mismatches.append("cover artwork")
        if mismatches:
            raise PipelineError(
                f"Metadata verification failed for {path}; mismatched: "
                + ", ".join(mismatches)
            )

    @staticmethod
    def expected_duration_seconds(item: Any) -> Fraction | None:
        metadata = item.media.media_metadata or {}
        duration_milliseconds = metadata.get("attributes", {}).get(
            "durationInMillis"
        )
        if duration_milliseconds is None or isinstance(
            duration_milliseconds, bool
        ):
            return None
        try:
            duration = Fraction(str(duration_milliseconds)) / 1000
        except (ValueError, ZeroDivisionError):
            return None
        return duration if duration > 0 else None

    @classmethod
    def verify_expected_duration(
        cls,
        item: Any,
        probe: MediaProbe,
        path: Path,
    ) -> None:
        expected = cls.expected_duration_seconds(item)
        if expected is None:
            return
        difference = abs(probe.duration_seconds - expected)
        if difference > EXPECTED_DURATION_TOLERANCE_SECONDS:
            raise PipelineError(
                f"Audio duration verification failed for {path}; decoded "
                f"{float(probe.duration_seconds):.3f}s, Apple expects "
                f"{float(expected):.3f}s (difference {float(difference):.3f}s)"
            )

    @staticmethod
    def verify_audio_properties(
        source: MediaProbe,
        output: MediaProbe,
        path: Path,
    ) -> None:
        mismatches = []
        if source.sample_rate != output.sample_rate:
            mismatches.append("sample rate")
        if source.channels != output.channels:
            mismatches.append("channel count")
        if (
            source.bits_per_sample
            and output.bits_per_sample
            and source.bits_per_sample != output.bits_per_sample
        ):
            mismatches.append("bit depth")
        if abs(source.sample_count - output.sample_count) > 1:
            mismatches.append("decoded sample count")
        if mismatches:
            raise PipelineError(
                f"Audio verification failed for {path}; mismatched: "
                + ", ".join(mismatches)
            )

    async def convert(
        self,
        source_path: Path,
        final_path: Path,
        media_id: str,
        overwrite: bool,
    ) -> Path:
        token = uuid.uuid4().hex
        temporary_directory = final_path.parent / ".apple-music-to-flac-tmp"
        temporary_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        native_temp = temporary_directory / f"{token}.native.flac"
        final_temp = temporary_directory / f"{token}.final.flac"

        try:
            source = await self.probe(source_path)
            if source.codec != "alac":
                raise PipelineError(
                    f"Expected ALAC but found {source.codec or 'unknown'}: {source_path}"
                )

            await run_checked(
                self.freaccmd,
                "-e",
                "flac",
                "-o",
                native_temp,
                source_path,
            )

            native = await self.probe(native_temp)
            if native.codec != "flac":
                raise PipelineError(f"fre:ac did not produce FLAC: {source_path}")
            self.verify_audio_properties(source, native, native_temp)
            native_hash = await self.pcm_hash(native_temp)

            # Audio comes only from fre:ac's FLAC. The M4A is opened solely for
            # its metadata and optional attached artwork; its ALAC is not decoded.
            await run_checked(
                self.ffmpeg,
                "-nostdin",
                "-y",
                "-v",
                "warning",
                "-xerror",
                "-i",
                native_temp,
                "-i",
                source_path,
                "-map",
                "0:a:0",
                "-map",
                "1:v?",
                "-map_metadata",
                "1",
                "-c",
                "copy",
                "-disposition:v",
                "attached_pic",
                "-metadata:s:v",
                "comment=Cover (front)",
                "-metadata",
                f"{APPLE_MUSIC_ID_TAG}={media_id}",
                final_temp,
            )

            final = await self.probe(final_temp)
            if final.codec != "flac":
                raise PipelineError(f"Final output is not FLAC: {source_path}")
            self.verify_audio_properties(source, final, final_temp)
            self.verify_metadata(source, final, final_temp)
            if final.tags.get(APPLE_MUSIC_ID_TAG) != media_id:
                raise PipelineError(f"Apple Music ID tag was not written to {final_temp}")

            # Computing the final hash is also a strict, complete decode check.
            final_hash = await self.pcm_hash(final_temp)
            if final_hash != native_hash:
                raise PipelineError(f"PCM hash changed during metadata remux: {source_path}")

            if final_path.exists() and not overwrite:
                raise PipelineError(f"FLAC already exists: {final_path}")
            os.replace(final_temp, final_path)
            source_path.unlink()
            return final_path
        finally:
            native_temp.unlink(missing_ok=True)
            final_temp.unlink(missing_ok=True)
            try:
                temporary_directory.rmdir()
            except OSError:
                pass


async def create_gamdl_downloader(
    config: Any,
) -> tuple[AppleMusicDownloader, AppleMusicApi]:
    prompts = InteractivePrompts(artist_auto_select=config.artist_auto_select)

    if config.use_wrapper:
        wrapper_api = await WrapperApi.create(
            base_url=config.wrapper_url,
            decrypt_host=config.wrapper_decrypt_host,
            decrypt_port=config.wrapper_decrypt_port,
            get_credentials_func=InteractivePrompts.get_wrapper_credentials,
            get_2fa_code=InteractivePrompts.get_wrapper_2fa_code,
        )
        apple_music_api = await SyllableLyricsAppleMusicApi.create_from_wrapper(
            wrapper_api=wrapper_api,
            language=config.language,
        )
    else:
        apple_music_api = (
            await SyllableLyricsAppleMusicApi.create_from_netscape_cookies(
                cookies_path=config.cookies_path,
                language=config.language,
            )
        )
        wrapper_api = None

    if not apple_music_api.active_subscription:
        raise PipelineError("No active Apple Music subscription was found")

    base_interface = await AppleMusicBaseInterface.create(
        apple_music_api=apple_music_api,
        cover_format=config.cover_format,
        cover_size=config.cover_size,
        wvd_path=config.wvd_path,
        wrapper_api=wrapper_api,
    )
    song_interface = SyllableLyricsSongInterface(
        base=base_interface,
        synced_lyrics_format=config.synced_lyrics_format,
        codec_priority=config.song_codec_piority,
        use_album_date=config.use_album_date,
        skip_stream_info=False,
        ask_codec_function=prompts.ask_song_codec,
    )
    music_video_interface = AppleMusicMusicVideoInterface(
        base=base_interface,
        resolution=config.music_video_resolution,
        codec_priority=config.music_video_codec_priority,
        ask_video_codec_function=prompts.ask_music_video_video_codec_function,
        ask_audio_codec_function=prompts.ask_music_video_audio_codec_function,
    )
    uploaded_video_interface = AppleMusicUploadedVideoInterface(
        base=base_interface,
        quality=config.uploaded_video_quality,
        ask_quality_function=prompts.ask_uploaded_video_quality_function,
    )
    interface = AppleMusicInterface(
        song=song_interface,
        music_video=music_video_interface,
        uploaded_video=uploaded_video_interface,
        artist_select_media_type_function=prompts.ask_artist_media_type,
        artist_select_items_function=prompts.ask_artist_select_items,
        flat_filter_function=None,
    )

    base_downloader = AppleMusicBaseDownloader(
        interface=interface,
        output_path=config.output_path,
        temp_path=config.temp_path,
        nm3u8dlre_path=config.nm3u8dlre_path,
        ffmpeg_path=config.ffmpeg_path,
        download_mode=config.download_mode,
        album_folder_template=config.album_folder_template,
        compilation_folder_template=config.compilation_folder_template,
        no_album_folder_template=config.no_album_folder_template,
        playlist_folder_template=config.playlist_folder_template,
        single_disc_file_template=config.single_disc_file_template,
        multi_disc_file_template=config.multi_disc_file_template,
        no_album_file_template=config.no_album_file_template,
        playlist_file_template=config.playlist_file_template,
        date_tag_template=config.date_tag_template,
        exclude_tags=config.exclude_tags,
        truncate=config.truncate,
        silent=True,
    )
    downloader = AppleMusicDownloader(
        song=AppleMusicSongDownloader(base=base_downloader),
        music_video=AppleMusicMusicVideoDownloader(
            base=base_downloader,
            remux_format=config.music_video_remux_format,
        ),
        uploaded_video=AppleMusicUploadedVideoDownloader(base=base_downloader),
        overwrite=config.overwrite,
        save_cover=config.save_cover,
        save_playlist=config.save_playlist,
        no_synced_lyrics=config.no_synced_lyrics,
        synced_lyrics_only=False,
    )
    return downloader, apple_music_api


def media_title(item: Any) -> str:
    metadata = item.media.media_metadata or {}
    return metadata.get("attributes", {}).get("name") or item.media.media_id


def item_key(item: Any) -> str:
    playlist_track = (
        item.media.playlist_tags.track if item.media.playlist_tags else ""
    )
    return f"{item.media.media_id}:{item.media.index}:{playlist_track}"


def is_song(item: Any) -> bool:
    metadata = item.media.media_metadata or {}
    return metadata.get("type") in {"songs", "library-songs"}


def is_playlist_url(url: str) -> bool:
    url_info = AppleMusicInterface.get_url_info(url)
    return bool(
        url_info
        and (url_info.type == "playlist" or url_info.library_type == "playlist")
    )


def full_album_id_from_url(url: str) -> str | None:
    url_info = AppleMusicInterface.get_url_info(url)
    if (
        url_info
        and url_info.type == "album"
        and url_info.id
        and not url_info.sub_id
    ):
        return str(url_info.id)
    return None


async def retry_sleep(base_delay: float, retry_number: int) -> None:
    delay = min(30.0, base_delay * (2 ** (retry_number - 1)))
    delay += random.uniform(0, min(1.0, delay * 0.2)) if delay else 0
    if delay:
        await asyncio.sleep(delay)


class AppleMusicFlacApp:
    def __init__(
        self,
        downloader: AppleMusicDownloader,
        apple_music_api: AppleMusicApi,
        transcoder: FlacTranscoder,
        config: Any,
        run_log: JsonlRunLogger,
        history: DownloadHistory,
        retries: int,
        retry_delay: float,
        force: bool,
        force_track_ids: set[str],
        force_album_ids: set[str],
    ) -> None:
        self.downloader = downloader
        self.apple_music_api = apple_music_api
        self.transcoder = transcoder
        self.config = config
        self.run_log = run_log
        self.history = history
        self.retries = retries
        self.retry_delay = retry_delay
        self.force = force
        self.force_track_ids = force_track_ids
        self.force_album_ids = force_album_ids
        self.statistics = Statistics()
        self.native_lyrics_items: set[str] = set()
        self.item_source_urls: dict[str, str] = {}
        self.item_force_reasons: dict[str, str] = {}
        self.output_root = Path(config.output_path).expanduser().resolve()
        self.filename_char_limit = config.truncate

    @staticmethod
    def album_id_from_metadata(metadata: dict[str, Any]) -> str | None:
        relationships = metadata.get("relationships", {})
        albums = relationships.get("albums", {}).get("data", [])
        for album in albums:
            album_id = str(album.get("id", ""))
            if album_id and (
                album.get("type") == "albums" or album_id.isdecimal()
            ):
                return album_id

        catalog = relationships.get("catalog", {}).get("data", [])
        if catalog:
            album_id = AppleMusicFlacApp.album_id_from_metadata(catalog[0])
            if album_id:
                return album_id

        media_url = metadata.get("attributes", {}).get("url")
        if media_url:
            url_info = AppleMusicInterface.get_url_info(media_url)
            if url_info and url_info.type == "album" and url_info.id:
                return str(url_info.id)
        return None

    @staticmethod
    def isrc_from_metadata(metadata: dict[str, Any]) -> str | None:
        isrc = metadata.get("attributes", {}).get("isrc")
        if isrc:
            return str(isrc)
        catalog = (
            metadata.get("relationships", {})
            .get("catalog", {})
            .get("data", [])
        )
        if catalog:
            return AppleMusicFlacApp.isrc_from_metadata(catalog[0])
        return None

    @classmethod
    def item_album_id(cls, item: Any) -> str | None:
        return cls.album_id_from_metadata(item.media.media_metadata or {})

    @classmethod
    def item_isrc(cls, item: Any) -> str | None:
        return cls.isrc_from_metadata(item.media.media_metadata or {})

    @staticmethod
    def expected_flac_path(item: Any) -> Path | None:
        """The FLAC this item would write, once safe paths have been applied."""
        source_path = getattr(item, "final_path", None)
        if not source_path:
            return None
        return Path(source_path).with_suffix(".flac")

    def force_reason(self, item: Any) -> str | None:
        saved_reason = self.item_force_reasons.get(item_key(item))
        if saved_reason:
            return saved_reason
        media_id = str(item.media.media_id)
        album_id = self.item_album_id(item)
        if self.force:
            return "force_all"
        if media_id in self.force_track_ids:
            return "force_track"
        if album_id and album_id in self.force_album_ids:
            return "force_album"
        return None

    def record_history_completion(
        self,
        item: Any,
        output_path: Path,
        outcome: str,
        lyrics_status: str,
    ) -> None:
        tags = item.media.tags
        self.history.record_completed(
            media_id=str(item.media.media_id),
            isrc=self.item_isrc(item),
            album_id=self.item_album_id(item),
            title=getattr(tags, "title", None),
            artist=getattr(tags, "artist", None),
            album=getattr(tags, "album", None),
            lyrics_status=lyrics_status,
            codec=DownloadHistory.codec_from_outcome(outcome, str(output_path)),
            outcome=outcome,
            output_path=str(output_path),
            output_size=output_path.stat().st_size,
            pcm_sha256=None,
            source_url=self.item_source_urls.get(item_key(item)),
            run_id=self.run_log.run_id,
        )

    async def fetch_playlist_tracks(self, url: str) -> tuple[dict, list[dict]]:
        url_info = AppleMusicInterface.get_url_info(url)
        if not url_info or not is_playlist_url(url):
            raise PipelineError(f"--playlist-albums needs a playlist URL: {url}")

        is_library = url_info.library_type == "playlist"
        playlist_id = url_info.library_id if is_library else url_info.id
        response = await (
            self.apple_music_api.get_library_playlist(playlist_id)
            if is_library
            else self.apple_music_api.get_playlist(playlist_id)
        )
        try:
            playlist = response["data"][0]
            relationship = playlist["relationships"]["tracks"]
            tracks = list(relationship.get("data", []))
            next_uri = relationship.get("next")
            href_uri = relationship.get("href")
        except (IndexError, KeyError, TypeError) as exc:
            raise PipelineError(
                f"Apple Music returned an invalid playlist: {url}"
            ) from exc

        while next_uri:
            if not href_uri:
                raise PipelineError(
                    f"Apple Music omitted the playlist pagination URL: {url}"
                )
            page = await self.apple_music_api.get_extended_api_data(
                next_uri,
                href_uri,
            )
            tracks.extend(page.get("data", []))
            next_uri = page.get("next")
        return playlist, tracks

    async def resolve_playlist_track_album_id(self, track: dict) -> str:
        album_id = self.album_id_from_metadata(track)
        if album_id:
            return album_id

        track_id = str(track.get("id", ""))
        track_type = track.get("type")
        if not track_id:
            raise PipelineError("Playlist track has no Apple Music ID")

        if track_type == "library-songs":
            response = await self.apple_music_api.get_library_song(track_id)
        elif track_type == "songs":
            response = await self.apple_music_api.get_song(track_id)
        else:
            raise PipelineError(f"Playlist item {track_id} is not a song")

        try:
            metadata = response["data"][0]
        except (IndexError, KeyError, TypeError) as exc:
            raise PipelineError(
                f"Apple Music returned no metadata for playlist track {track_id}"
            ) from exc
        album_id = self.album_id_from_metadata(metadata)
        if not album_id and track_type == "library-songs":
            catalog = (
                metadata.get("relationships", {})
                .get("catalog", {})
                .get("data", [])
            )
            catalog_id = str(catalog[0].get("id", "")) if catalog else ""
            if catalog_id:
                response = await self.apple_music_api.get_song(catalog_id)
                try:
                    metadata = response["data"][0]
                except (IndexError, KeyError, TypeError) as exc:
                    raise PipelineError(
                        "Apple Music returned no catalog metadata for library "
                        f"playlist track {track_id}"
                    ) from exc
                album_id = self.album_id_from_metadata(metadata)
        if not album_id:
            raise PipelineError(
                f"Apple Music returned no catalog album for playlist track {track_id}"
            )
        return album_id

    async def expand_playlist_to_album_urls(self, url: str) -> list[str]:
        passes = self.retries + 1
        playlist: dict | None = None
        tracks: list[dict] = []
        self.run_log.emit("playlist_album_expansion_started", url=url)

        for attempt in range(1, passes + 1):
            try:
                playlist, tracks = await self.fetch_playlist_tracks(url)
                break
            except Exception as exc:
                self.run_log.emit(
                    "playlist_resolution_failed",
                    url=url,
                    attempt=attempt,
                    attempts=passes,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if attempt == passes:
                    self.statistics.failed += 1
                    LOG.error(
                        "Could not read playlist after %d attempt(s): %s",
                        passes,
                        exc,
                    )
                    return []
                await retry_sleep(self.retry_delay, attempt)

        playlist_name = (playlist or {}).get("attributes", {}).get("name", url)
        album_ids: list[str] = []
        seen_album_ids: set[str] = set()
        unresolved_tracks = 0
        skipped_items = 0

        for index, track in enumerate(tracks, start=1):
            track_type = track.get("type")
            track_id = str(track.get("id", ""))
            track_name = (
                track.get("attributes", {}).get("name")
                or track_id
                or str(index)
            )
            if track_type not in {"songs", "library-songs"}:
                skipped_items += 1
                self.statistics.skipped += 1
                self.run_log.emit(
                    "playlist_item_skipped",
                    url=url,
                    playlist=playlist_name,
                    playlist_index=index,
                    media_id=track_id,
                    media_type=track_type,
                    title=track_name,
                    reason="non_song",
                )
                continue

            album_id = None
            error: Exception | None = None
            for attempt in range(1, passes + 1):
                try:
                    album_id = await self.resolve_playlist_track_album_id(track)
                    break
                except Exception as exc:
                    error = exc
                    self.run_log.emit(
                        "playlist_track_album_resolution_failed",
                        url=url,
                        playlist=playlist_name,
                        playlist_index=index,
                        media_id=track_id,
                        title=track_name,
                        attempt=attempt,
                        attempts=passes,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    if attempt < passes:
                        await retry_sleep(self.retry_delay, attempt)

            if not album_id:
                unresolved_tracks += 1
                self.statistics.failed += 1
                LOG.error(
                    'Could not find the album for playlist track "%s": %s',
                    track_name,
                    error,
                )
                self.run_log.emit(
                    "playlist_track_album_unresolved",
                    durable=True,
                    url=url,
                    playlist=playlist_name,
                    playlist_index=index,
                    media_id=track_id,
                    title=track_name,
                    error=str(error),
                    recovery_action="rerun_original_playlist_url",
                )
                continue

            if album_id in seen_album_ids:
                self.run_log.emit(
                    "playlist_album_duplicate",
                    url=url,
                    playlist=playlist_name,
                    playlist_index=index,
                    media_id=track_id,
                    album_id=album_id,
                )
                continue

            seen_album_ids.add(album_id)
            album_ids.append(album_id)
            self.run_log.emit(
                "playlist_album_discovered",
                url=url,
                playlist=playlist_name,
                playlist_index=index,
                media_id=track_id,
                title=track_name,
                album_id=album_id,
            )

        storefront = self.apple_music_api.storefront or "us"
        album_urls = [
            f"https://music.apple.com/{storefront}/album/{album_id}"
            for album_id in album_ids
        ]
        LOG.info(
            'Playlist "%s" contains %d unique album(s) across %d item(s)',
            playlist_name,
            len(album_urls),
            len(tracks),
        )
        self.run_log.emit(
            "playlist_album_expansion_completed",
            durable=bool(unresolved_tracks),
            url=url,
            playlist=playlist_name,
            playlist_items=len(tracks),
            unique_albums=len(album_urls),
            skipped_items=skipped_items,
            unresolved_tracks=unresolved_tracks,
            album_urls=album_urls,
        )
        return album_urls

    def set_item_source_path(self, item: Any, source_path: Path) -> None:
        item.final_path = str(source_path)
        item.synced_lyrics_path = str(
            source_path.with_suffix("." + self.config.synced_lyrics_format.value)
        )
        if item.cover_path:
            item.cover_path = str(source_path.parent / Path(item.cover_path).name)

    def apply_safe_item_paths(self, item: Any) -> None:
        source_path = shorten_path(
            Path(item.final_path),
            self.output_root,
            self.filename_char_limit,
        )
        self.set_item_source_path(item, source_path)
        if item.playlist_file_path:
            item.playlist_file_path = str(
                shorten_path(
                    Path(item.playlist_file_path),
                    self.output_root,
                    self.filename_char_limit,
                )
            )

    @staticmethod
    def track_fields(item: Any) -> dict[str, Any]:
        tags = item.media.tags
        metadata = item.media.media_metadata or {}
        source_path = Path(item.final_path) if item.final_path else None
        return {
            "item_key": item_key(item),
            "media_id": str(item.media.media_id),
            "media_type": (item.media.media_metadata or {}).get("type"),
            "title": media_title(item),
            "artist": getattr(tags, "artist", None),
            "album": getattr(tags, "album", None),
            "album_id": AppleMusicFlacApp.album_id_from_metadata(metadata),
            "isrc": AppleMusicFlacApp.isrc_from_metadata(metadata),
            "disc": getattr(tags, "disc", None),
            "track": getattr(tags, "track", None),
            "source_path": str(source_path) if source_path else None,
            "target_flac_path": (
                str(source_path.with_suffix(".flac")) if source_path else None
            ),
            "lyrics_path": item.synced_lyrics_path,
        }

    @staticmethod
    def expected_metadata_mismatches(item: Any, probe: MediaProbe) -> list[str]:
        expected_tags = item.media.tags
        expected = {
            "album": expected_tags.album,
            "album_artist": expected_tags.album_artist,
            "artist": expected_tags.artist,
            "title": expected_tags.title,
            "disc": expected_tags.disc,
            "track": expected_tags.track,
        }
        mismatches = [
            key
            for key, value in expected.items()
            if value is not None
            and not metadata_values_equal(key, value, probe.tags.get(key))
        ]
        if item.media.cover and item.media.cover.url and not probe.has_artwork:
            mismatches.append("cover artwork")
        return mismatches

    async def validate_existing_flac(self, item: Any, path: Path) -> str:
        if not path.is_file() or path.stat().st_size == 0:
            raise PipelineError(f"Existing FLAC is empty or not a regular file: {path}")
        probe = await self.transcoder.probe(path)
        if probe.codec != "flac":
            raise PipelineError(f"Existing .flac path contains {probe.codec}: {path}")
        self.transcoder.verify_expected_duration(item, probe, path)

        media_id = str(item.media.media_id)
        stored_id = probe.tags.get(APPLE_MUSIC_ID_TAG)
        if stored_id and stored_id != media_id:
            raise IdentityMismatch(
                f"Existing FLAC belongs to Apple Music track {stored_id}: {path}"
            )
        mismatches = self.expected_metadata_mismatches(item, probe)
        if mismatches:
            error = f"Existing FLAC metadata differs for {path}: {', '.join(mismatches)}"
            if stored_id:
                raise PipelineError(error)
            raise IdentityMismatch(error)

        pcm_hash = await self.transcoder.pcm_hash(path)
        return pcm_hash.removeprefix("SHA256=")

    async def quarantine_invalid_flac(
        self,
        item: Any,
        path: Path,
        reason: Exception,
    ) -> None:
        quarantine_directory = self.output_root / ".apple-music-to-flac-quarantine"
        quarantine_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine_path = quarantine_directory / f"{timestamp}-{digest}.flac"
        os.replace(path, quarantine_path)
        LOG.warning("Quarantined invalid FLAC %s as %s", path, quarantine_path)
        self.run_log.emit(
            "existing_flac_quarantined",
            durable=True,
            original_path=str(path),
            quarantine_path=str(quarantine_path),
            reason=str(reason),
            **self.track_fields(item),
        )

    async def resolve_existing_path(self, item: Any) -> tuple[Path, Path, str | None]:
        source_path = Path(item.final_path)
        final_path = source_path.with_suffix(".flac")
        existing_hash = None

        if final_path.exists():
            try:
                existing_hash = await self.validate_existing_flac(item, final_path)
            except IdentityMismatch as exc:
                original_path = final_path
                source_path = collision_path(
                    source_path,
                    str(item.media.media_id),
                    self.filename_char_limit,
                )
                self.set_item_source_path(item, source_path)
                final_path = source_path.with_suffix(".flac")
                self.run_log.emit(
                    "filename_collision_resolved",
                    original_path=str(original_path),
                    resolved_path=str(final_path),
                    reason=str(exc),
                    **self.track_fields(item),
                )
                if final_path.exists():
                    try:
                        existing_hash = await self.validate_existing_flac(item, final_path)
                    except IdentityMismatch as collision_exc:
                        raise PipelineError(
                            f"Collision-safe path belongs to another track: {final_path}"
                        ) from collision_exc
                    except PipelineError as invalid_exc:
                        await self.quarantine_invalid_flac(item, final_path, invalid_exc)
                        existing_hash = None
            except PipelineError as exc:
                await self.quarantine_invalid_flac(item, final_path, exc)

        if source_path.exists() and not final_path.exists():
            try:
                source_probe = await self.transcoder.probe(source_path)
                mismatches = self.expected_metadata_mismatches(item, source_probe)
                if mismatches:
                    raise IdentityMismatch(
                        f"Existing M4A metadata differs: {', '.join(mismatches)}"
                    )
            except IdentityMismatch as exc:
                original_path = source_path
                source_path = collision_path(
                    source_path,
                    str(item.media.media_id),
                    self.filename_char_limit,
                )
                self.set_item_source_path(item, source_path)
                final_path = source_path.with_suffix(".flac")
                existing_hash = None
                self.run_log.emit(
                    "filename_collision_resolved",
                    original_path=str(original_path),
                    resolved_path=str(final_path),
                    reason=str(exc),
                    **self.track_fields(item),
                )
            except PipelineError:
                pass

        return source_path, final_path, existing_hash

    @staticmethod
    def inspect_ttml(lyrics: str) -> tuple[str | None, int]:
        root = ElementTree.fromstring(lyrics)
        timing = root.attrib.get(
            "{http://music.apple.com/lyric-ttml-internal}timing"
        )
        timed_spans = sum(
            1
            for span in root.iter("{http://www.w3.org/ns/ttml}span")
            if span.attrib.get("begin") and span.attrib.get("end")
        )
        return timing, timed_spans

    @staticmethod
    def parse_ttml_time(value: str) -> float:
        parts = value.removesuffix("s").split(":")
        seconds = 0.0
        for part in parts:
            seconds = (seconds * 60) + float(part)
        return seconds

    @staticmethod
    def ttml_line_text(element: ElementTree.Element) -> str:
        return " ".join("".join(element.itertext()).split())

    def render_native_lyrics(self, lyrics_ttml: str) -> Lyrics:
        root = ElementTree.fromstring(lyrics_ttml)
        lines = list(root.iter("{http://www.w3.org/ns/ttml}p"))
        plain_lines = [self.ttml_line_text(line) for line in lines]
        unsynced = "\n".join(line for line in plain_lines if line) or None

        if self.config.synced_lyrics_format == SyncedLyricsFormat.TTML:
            synced = lyrics_ttml
        elif self.config.synced_lyrics_format == SyncedLyricsFormat.LRC:
            rendered = []
            for element, line in zip(lines, plain_lines, strict=True):
                if not line or not element.attrib.get("begin"):
                    continue
                seconds = self.parse_ttml_time(element.attrib["begin"])
                total_centiseconds = round(seconds * 100)
                minutes, centiseconds = divmod(total_centiseconds, 6000)
                secs, centiseconds = divmod(centiseconds, 100)
                rendered.append(
                    f"[{minutes:02d}:{secs:02d}.{centiseconds:02d}]{line}"
                )
            synced = "\n".join(rendered) or None
        else:
            rendered = []
            for element, line in zip(lines, plain_lines, strict=True):
                if (
                    not line
                    or not element.attrib.get("begin")
                    or not element.attrib.get("end")
                ):
                    continue
                begin = self.parse_ttml_time(element.attrib["begin"])
                end = self.parse_ttml_time(element.attrib["end"])

                def srt_time(seconds: float) -> str:
                    milliseconds = round(seconds * 1000)
                    hours, milliseconds = divmod(milliseconds, 3_600_000)
                    minutes, milliseconds = divmod(milliseconds, 60_000)
                    secs, milliseconds = divmod(milliseconds, 1000)
                    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

                rendered.append(
                    f"{len(rendered) + 1}\n{srt_time(begin)} --> {srt_time(end)}"
                    f"\n{line}\n"
                )
            synced = "\n".join(rendered) or None

        return Lyrics(synced=synced, unsynced=unsynced)

    async def acquire_apple_lyrics(self, item: Any) -> None:
        media_id = str(item.media.media_id)
        endpoint = (
            f"/v1/catalog/{self.apple_music_api.storefront}/songs/"
            f"{media_id}/syllable-lyrics"
        )
        try:
            response = await self.apple_music_api._amp_request(  # noqa: SLF001
                endpoint,
                {"l": self.apple_music_api.language},
            )
            resources = response.get("data") or []
            lyrics_ttml = (
                resources[0].get("attributes", {}).get("ttml")
                if resources
                else None
            )
            if not lyrics_ttml:
                LOG.info(
                    'Apple returned no lyric document for "%s"',
                    media_title(item),
                )
                self.run_log.emit(
                    "lyrics_unavailable",
                    reason="no_document",
                    **self.track_fields(item),
                )
                return

            timing, timed_spans = self.inspect_ttml(lyrics_ttml)
            item.media.lyrics = self.render_native_lyrics(lyrics_ttml)
            if item.media.tags is not None:
                item.media.tags.lyrics = item.media.lyrics.unsynced
            self.native_lyrics_items.add(item_key(item))
            self.run_log.emit(
                "lyrics_acquired",
                timing=timing or "unsynced",
                timed_spans=timed_spans,
                has_plain_text=bool(item.media.lyrics.unsynced),
                output_format=self.config.synced_lyrics_format.value,
                **self.track_fields(item),
            )
            LOG.info(
                'Using Apple syllable lyrics for "%s" (%s timing, %d timed spans)',
                media_title(item),
                timing or "unsynced",
                timed_spans,
            )
        except Exception as exc:
            # Lyrics are optional; a private endpoint failure must not turn a
            # valid audio download into a failure or trigger another provider.
            LOG.warning(
                'Could not fetch Apple syllable lyrics for "%s": %s',
                media_title(item),
                exc,
            )
            self.run_log.emit(
                "lyrics_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                **self.track_fields(item),
            )

    def ensure_synced_lyrics(self, item: Any) -> None:
        lyrics = item.media.lyrics.synced if item.media.lyrics else None
        if not lyrics or not item.synced_lyrics_path:
            return
        lyrics_path = Path(item.synced_lyrics_path)
        should_write = self.config.overwrite or not lyrics_path.exists()
        if not should_write and item_key(item) in self.native_lyrics_items:
            try:
                should_write = lyrics_path.read_text(encoding="utf-8") != lyrics
            except OSError:
                should_write = True

        if should_write:
            replaced_existing = lyrics_path.exists()
            self.downloader._write_synced_lyrics(  # noqa: SLF001 - Gamdl API gap
                str(lyrics_path),
                lyrics,
            )
            self.run_log.emit(
                "lyrics_written",
                path=str(lyrics_path),
                replaced_existing=replaced_existing,
                output_format=self.config.synced_lyrics_format.value,
                **self.track_fields(item),
            )

    def update_playlist(self, item: Any, final_path: Path) -> None:
        if (
            self.config.save_playlist
            and item.playlist_file_path
            and item.media.playlist_tags
        ):
            self.downloader._update_playlist_file(  # noqa: SLF001 - Gamdl API gap
                item.playlist_file_path,
                str(final_path),
                item.media.playlist_tags.track,
            )

    @staticmethod
    def verify_gamdl_metadata(item: Any, probe: MediaProbe, path: Path) -> None:
        mismatches = AppleMusicFlacApp.expected_metadata_mismatches(item, probe)
        if mismatches:
            raise PipelineError(
                f"Gamdl metadata verification failed for {path}; mismatched: "
                + ", ".join(mismatches)
            )

    async def process_track(self, item: Any) -> bool:
        media_id = str(item.media.media_id)
        lock = TrackFileLock(self.output_root, media_id)
        self.run_log.emit(
            "track_lock_waiting",
            lock_path=str(lock.path),
            **self.track_fields(item),
        )
        async with lock:
            self.run_log.emit(
                "track_lock_acquired",
                lock_path=str(lock.path),
                **self.track_fields(item),
            )
            if not self.force_reason(item):
                history_match = self.history.find_completed(
                    media_id,
                    self.item_isrc(item),
                    self.expected_flac_path(item),
                )
                if history_match:
                    isrc = self.item_isrc(item)
                    if (
                        history_match.matched_by == "isrc"
                        and history_match.media_id != media_id
                        and isrc
                    ):
                        self.history.record_coverage_alias(
                            media_id=media_id,
                            isrc=isrc,
                            album_id=self.item_album_id(item),
                            title=getattr(item.media.tags, "title", None),
                            artist=getattr(item.media.tags, "artist", None),
                            album=getattr(item.media.tags, "album", None),
                            match=history_match,
                            source_url=self.item_source_urls.get(item_key(item), ""),
                            run_id=self.run_log.run_id,
                        )
                    self.statistics.skipped += 1
                    self.statistics.history_skipped += 1
                    self.run_log.emit(
                        "track_skipped_history_after_lock",
                        durable=True,
                        matched_by=history_match.matched_by,
                        matched_media_id=history_match.media_id,
                        completed_at=history_match.completed_at,
                        **self.track_fields(item),
                    )
                    return True
            # Different media IDs can initially map to the same truncated path.
            # Keep a path reservation for the collision check and audio write.
            path_lock = TrackFileLock(
                self.output_root,
                f"path:{Path(item.final_path).resolve()}",
            )
            async with path_lock:
                return await self.process_track_locked(item)

    async def finalize_track(
        self,
        item: Any,
        output_path: Path,
        outcome: str,
        audio_attempt: int,
    ) -> bool:
        title = media_title(item)
        fields = self.track_fields(item)
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                self.run_log.emit(
                    "finalization_started",
                    attempt=attempt,
                    attempts=attempts,
                    output_path=str(output_path),
                    **fields,
                )
                self.ensure_synced_lyrics(item)
                self.update_playlist(item, output_path)
                self.record_history_completion(
                    item,
                    output_path,
                    outcome,
                    "complete",
                )
                LOG.info('Finished "%s": %s', title, output_path)
                self.run_log.emit(
                    "track_completed",
                    durable=True,
                    outcome=outcome,
                    audio_attempt=audio_attempt,
                    finalization_attempt=attempt,
                    output_path=str(output_path),
                    output_size=output_path.stat().st_size,
                    **fields,
                )
                return True
            except Exception as exc:
                terminal = attempt == attempts
                self.run_log.emit(
                    "finalization_failed",
                    durable=terminal,
                    attempt=attempt,
                    attempts=attempts,
                    terminal=terminal,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    output_path=str(output_path),
                    recovery_action="rerun_to_retry_sidecars_only",
                    **fields,
                )
                if not terminal:
                    LOG.warning(
                        'Finalization attempt %d/%d failed for "%s": %s; retrying',
                        attempt,
                        attempts,
                        title,
                        exc,
                    )
                    await retry_sleep(self.retry_delay, attempt)

        self.statistics.finalization_failed += 1
        LOG.error(
            'Audio is valid for "%s", but lyrics or playlist finalization failed: %s',
            title,
            output_path,
        )
        try:
            self.record_history_completion(
                item,
                output_path,
                outcome,
                "failed",
            )
        except Exception as exc:
            self.run_log.emit(
                "history_write_failed",
                durable=True,
                error_type=type(exc).__name__,
                error=str(exc),
                output_path=str(output_path),
                **fields,
            )
        self.run_log.emit(
            "track_completed",
            durable=True,
            outcome=outcome,
            audio_attempt=audio_attempt,
            finalization_failed=True,
            output_path=str(output_path),
            output_size=output_path.stat().st_size,
            **fields,
        )
        return True

    async def process_track_locked(self, item: Any) -> bool:
        title = media_title(item)
        source_path, final_path, existing_hash = await self.resolve_existing_path(item)
        fields = self.track_fields(item)
        self.run_log.emit("track_started", **fields)

        if existing_hash and not self.config.overwrite:
            LOG.info('Validated existing FLAC: "%s"', title)
            self.statistics.skipped += 1
            self.run_log.emit(
                "existing_flac_validated",
                output_path=str(final_path),
                output_size=final_path.stat().st_size,
                pcm_sha256=existing_hash,
                identity_verified=True,
                **fields,
            )
            return await self.finalize_track(
                item,
                final_path,
                "skipped_valid_existing_flac",
                0,
            )

        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            stage = "prepare"
            output_path: Path | None = None
            try:
                if self.config.overwrite or not source_path.exists():
                    stage = "download"
                    LOG.info(
                        'Downloading "%s" (attempt %d/%d)', title, attempt, attempts
                    )
                    self.run_log.emit(
                        "download_started",
                        attempt=attempt,
                        attempts=attempts,
                        overwrite=self.config.overwrite,
                        **fields,
                    )
                    await self.downloader.download(item)
                    self.statistics.downloaded += 1
                    self.run_log.emit(
                        "download_completed",
                        attempt=attempt,
                        path=str(source_path),
                        size=(
                            source_path.stat().st_size
                            if source_path.is_file()
                            else None
                        ),
                        **fields,
                    )
                else:
                    LOG.info('Using existing M4A for "%s"', title)
                    self.run_log.emit(
                        "source_reused",
                        attempt=attempt,
                        path=str(source_path),
                        size=source_path.stat().st_size,
                        **fields,
                    )

                stage = "validate_source"
                if not source_path.is_file() or source_path.stat().st_size == 0:
                    raise PipelineError(f"Gamdl did not produce {source_path}")

                source_probe = await self.transcoder.probe(source_path)
                self.verify_gamdl_metadata(item, source_probe, source_path)
                expected_duration = self.transcoder.expected_duration_seconds(item)
                self.transcoder.verify_expected_duration(
                    item, source_probe, source_path
                )
                self.run_log.emit(
                    "source_validated",
                    attempt=attempt,
                    codec=source_probe.codec,
                    sample_rate=source_probe.sample_rate,
                    channels=source_probe.channels,
                    time_base=source_probe.time_base,
                    duration_ts=source_probe.duration_ts,
                    expected_duration_seconds=(
                        float(expected_duration)
                        if expected_duration is not None
                        else None
                    ),
                    sample_count=source_probe.sample_count,
                    bits_per_sample=source_probe.bits_per_sample,
                    has_artwork=source_probe.has_artwork,
                    size=source_path.stat().st_size,
                    **fields,
                )

                stage = "select_codec"
                if source_probe.codec == "alac":
                    stage = "convert_flac"
                    LOG.info('Converting "%s" with fre:ac', title)
                    self.run_log.emit(
                        "conversion_started",
                        attempt=attempt,
                        input_codec="alac",
                        converter="fre:ac+ffmpeg",
                        **fields,
                    )
                    output_path = await self.transcoder.convert(
                        source_path,
                        final_path,
                        str(item.media.media_id),
                        overwrite=self.config.overwrite,
                    )
                    self.statistics.converted += 1
                    self.run_log.emit(
                        "conversion_completed",
                        attempt=attempt,
                        output_path=str(output_path),
                        output_size=output_path.stat().st_size,
                        output_codec="flac",
                        audio_shape_verified=True,
                        metadata_verified=True,
                        artwork_verified=True,
                        pcm_hash_verified=True,
                        source_removed=not source_path.exists(),
                        **fields,
                    )
                    outcome = "converted_flac"
                elif source_probe.codec == "aac":
                    # A complete decoded-PCM hash doubles as strict AAC decode
                    # verification. Keep the original M4A to avoid another lossy
                    # generation or a misleading, larger FLAC.
                    stage = "verify_aac"
                    pcm_hash = await self.transcoder.pcm_hash(source_path)
                    if final_path.exists():
                        LOG.warning(
                            'AAC fallback received for "%s"; preserving existing FLAC',
                            title,
                        )
                        source_path.unlink()
                        output_path = final_path
                        self.statistics.skipped += 1
                        outcome = "preserved_existing_flac_over_aac"
                    else:
                        output_path = source_path
                        self.statistics.aac_retained += 1
                        outcome = "retained_aac"
                        LOG.info('Retained AAC/M4A for "%s": %s', title, output_path)
                    self.run_log.emit(
                        "aac_verified",
                        attempt=attempt,
                        outcome=outcome,
                        output_path=str(output_path),
                        output_size=output_path.stat().st_size,
                        pcm_sha256=pcm_hash.removeprefix("SHA256="),
                        **fields,
                    )
                else:
                    raise PipelineError(
                        f"Unsupported downloaded codec {source_probe.codec!r}: "
                        f"{source_path}"
                    )

                return await self.finalize_track(item, output_path, outcome, attempt)
            except Exception as exc:  # Gamdl exposes several third-party errors
                if output_path is not None and output_path.is_file():
                    self.statistics.finalization_failed += 1
                    LOG.error(
                        'Audio is valid for "%s", but post-conversion work failed: %s',
                        title,
                        exc,
                    )
                    self.run_log.emit(
                        "audio_preserved_after_post_conversion_failure",
                        durable=True,
                        stage=stage,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        output_path=str(output_path),
                        recovery_action="rerun_to_retry_sidecars_only",
                        **fields,
                    )
                    return True
                terminal = attempt == attempts
                source_exists = source_path.is_file()
                if terminal:
                    recovery_action = (
                        "rerun_original_url_or_convert_retained_source"
                        if source_exists
                        else "rerun_original_url"
                    )
                else:
                    recovery_action = "automatic_retry"
                self.run_log.emit(
                    "track_attempt_failed",
                    durable=terminal,
                    attempt=attempt,
                    attempts=attempts,
                    stage=stage,
                    terminal=terminal,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    retained_source_path=(str(source_path) if source_exists else None),
                    retained_source_size=(
                        source_path.stat().st_size if source_exists else None
                    ),
                    recovery_target_path=str(final_path),
                    recovery_action=recovery_action,
                    **fields,
                )
                if terminal:
                    LOG.error(
                        'Failed "%s" after %d attempt(s): %s', title, attempts, exc
                    )
                    self.statistics.failed += 1
                    return False

                LOG.warning(
                    'Attempt %d/%d failed for "%s": %s; retrying',
                    attempt,
                    attempts,
                    title,
                    exc,
                )
                # Once output_path is set, the audio stage succeeded and this
                # block must never cause another download. Finalization runs in
                # its own retry loop above.
                removed_source = output_path is None and source_path.exists()
                if removed_source:
                    source_path.unlink()
                self.run_log.emit(
                    "retry_cleanup",
                    attempt=attempt,
                    removed_source=removed_source,
                    path=str(source_path),
                    **fields,
                )
                await retry_sleep(self.retry_delay, attempt)

        return False

    async def process_url(self, url: str) -> None:
        LOG.info("Processing URL: %s", url)
        self.run_log.emit("url_started", url=url)
        url_album_id = full_album_id_from_url(url)
        album_record = (
            self.history.completed_album(url_album_id) if url_album_id else None
        )
        album_history_forced = bool(
            self.force
            or self.force_track_ids
            or (url_album_id and url_album_id in self.force_album_ids)
        )
        if album_record and not album_history_forced:
            track_count = int(album_record["track_count"])
            self.statistics.skipped += track_count
            self.statistics.history_skipped += track_count
            LOG.info(
                'Skipping album "%s"; %d track(s) are complete in download history',
                album_record["title"] or url_album_id,
                track_count,
            )
            self.run_log.emit(
                "album_skipped_history",
                durable=True,
                url=url,
                album_id=url_album_id,
                title=album_record["title"],
                artist=album_record["artist"],
                track_count=track_count,
                completed_at=album_record["completed_at"],
            )
            self.run_log.emit(
                "url_completed",
                url=url,
                completed_items=track_count,
                terminal_failed_items=0,
                history_skipped_items=track_count,
            )
            return

        completed: set[str] = set()
        unresolved: dict[str, str] = {}
        terminal_failures: set[str] = set()
        album_track_ids: set[str] = set()
        album_title: str | None = None
        album_artist: str | None = None
        passes = self.retries + 1

        for resolve_attempt in range(1, passes + 1):
            self.run_log.emit(
                "resolution_pass_started",
                url=url,
                attempt=resolve_attempt,
                attempts=passes,
            )
            unresolved = {}
            generator_failed = False
            try:
                async for item in self.downloader.get_download_item_from_url(url):
                    if item.media.partial:
                        continue
                    key = item_key(item)
                    if key in completed or key in terminal_failures:
                        continue
                    if item.media.error:
                        nonretryable = isinstance(
                            item.media.error,
                            NONRETRYABLE_RESOLUTION_ERRORS,
                        )
                        self.run_log.emit(
                            "item_resolution_failed",
                            url=url,
                            attempt=resolve_attempt,
                            retryable=not nonretryable,
                            error_type=type(item.media.error).__name__,
                            error=str(item.media.error),
                            **self.track_fields(item),
                        )
                        if nonretryable:
                            terminal_failures.add(key)
                            self.statistics.failed += 1
                            LOG.error(
                                'Track "%s" cannot be downloaded: %s',
                                media_title(item),
                                item.media.error,
                            )
                        else:
                            unresolved[key] = str(item.media.error)
                        continue
                    if not is_song(item):
                        LOG.warning(
                            'Skipping non-song item "%s"; only songs can become FLAC',
                            media_title(item),
                        )
                        completed.add(key)
                        self.statistics.skipped += 1
                        self.run_log.emit(
                            "item_skipped",
                            url=url,
                            reason="non_song",
                            **self.track_fields(item),
                        )
                        continue

                    self.apply_safe_item_paths(item)
                    self.item_source_urls[key] = url
                    media_id = str(item.media.media_id)
                    if url_album_id:
                        album_track_ids.add(media_id)
                        album_title = album_title or getattr(
                            item.media.tags,
                            "album",
                            None,
                        )
                        album_artist = album_artist or getattr(
                            item.media.tags,
                            "album_artist",
                            None,
                        )
                    self.run_log.emit(
                        "track_discovered",
                        url=url,
                        resolution_attempt=resolve_attempt,
                        **self.track_fields(item),
                    )
                    force_reason = self.force_reason(item)
                    if (
                        not force_reason
                        and url_album_id
                        and url_album_id in self.force_album_ids
                    ):
                        force_reason = "force_album"
                    history_match = self.history.find_completed(
                        media_id,
                        self.item_isrc(item),
                        self.expected_flac_path(item),
                    )
                    if history_match and not force_reason:
                        isrc = self.item_isrc(item)
                        if (
                            history_match.matched_by == "isrc"
                            and history_match.media_id != media_id
                            and isrc
                        ):
                            self.history.record_coverage_alias(
                                media_id=media_id,
                                isrc=isrc,
                                album_id=self.item_album_id(item),
                                title=getattr(item.media.tags, "title", None),
                                artist=getattr(item.media.tags, "artist", None),
                                album=getattr(item.media.tags, "album", None),
                                match=history_match,
                                source_url=url,
                                run_id=self.run_log.run_id,
                            )
                        self.statistics.skipped += 1
                        self.statistics.history_skipped += 1
                        completed.add(key)
                        LOG.info(
                            'Skipping "%s"; already complete in download history',
                            media_title(item),
                        )
                        self.run_log.emit(
                            "track_skipped_history",
                            durable=True,
                            url=url,
                            matched_by=history_match.matched_by,
                            matched_media_id=history_match.media_id,
                            matched_outcome=history_match.outcome,
                            matched_output_path=history_match.output_path,
                            completed_at=history_match.completed_at,
                            **self.track_fields(item),
                        )
                        continue
                    if force_reason:
                        self.item_force_reasons[key] = force_reason
                        self.run_log.emit(
                            "history_bypassed",
                            url=url,
                            reason=force_reason,
                            had_completed_match=bool(history_match),
                            **self.track_fields(item),
                        )
                    await self.acquire_apple_lyrics(item)
                    if await self.process_track(item):
                        completed.add(key)
                    else:
                        terminal_failures.add(key)
            except Exception as exc:
                if isinstance(exc, NONRETRYABLE_RESOLUTION_ERRORS):
                    self.statistics.failed += 1
                    self.run_log.emit(
                        "url_resolution_exception",
                        durable=True,
                        url=url,
                        attempt=resolve_attempt,
                        retryable=False,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    LOG.error("URL cannot be downloaded: %s", exc)
                    return
                generator_failed = True
                unresolved["<url>"] = str(exc)
                self.run_log.emit(
                    "url_resolution_exception",
                    url=url,
                    attempt=resolve_attempt,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

            if not unresolved and not generator_failed:
                album_marked_complete = False
                if url_album_id and not terminal_failures:
                    album_marked_complete = self.history.mark_album_complete(
                        album_id=url_album_id,
                        storefront=self.apple_music_api.storefront,
                        title=album_title,
                        artist=album_artist,
                        track_ids=album_track_ids,
                        run_id=self.run_log.run_id,
                        source_url=url,
                    )
                    if album_marked_complete:
                        self.run_log.emit(
                            "album_history_completed",
                            durable=True,
                            url=url,
                            album_id=url_album_id,
                            title=album_title,
                            artist=album_artist,
                            track_count=len(album_track_ids),
                        )
                self.run_log.emit(
                    "url_completed",
                    url=url,
                    completed_items=len(completed),
                    terminal_failed_items=len(terminal_failures),
                    album_history_completed=album_marked_complete,
                )
                return
            if resolve_attempt < passes:
                LOG.warning(
                    "Gamdl could not resolve %d item(s) on pass %d/%d; retrying URL",
                    len(unresolved),
                    resolve_attempt,
                    passes,
                )
                self.run_log.emit(
                    "resolution_retry_scheduled",
                    url=url,
                    attempt=resolve_attempt,
                    attempts=passes,
                    unresolved_items=len(unresolved),
                )
                await retry_sleep(self.retry_delay, resolve_attempt)

        for key, detail in unresolved.items():
            LOG.error("Unresolved Gamdl item %s: %s", key, detail)
            self.statistics.failed += 1
            self.run_log.emit(
                "item_unresolved",
                durable=True,
                url=url,
                item_key=key,
                error=detail,
                recovery_action="rerun_original_url",
            )
        self.run_log.emit(
            "url_completed",
            durable=bool(unresolved),
            url=url,
            completed_items=len(completed),
            terminal_failed_items=len(terminal_failures),
            unresolved_items=len(unresolved),
        )


async def async_main(
    args: argparse.Namespace,
    run_log: JsonlRunLogger,
    history: DownloadHistory,
) -> int:
    config = load_gamdl_config(args.config.resolve(), args.urls)
    if args.output:
        output_path = args.output.resolve()
        config.output_path = str(output_path)
        config.temp_path = str(output_path / ".gamdl-temp")
    if args.artist_auto_select:
        config.artist_auto_select = ArtistMediaType(args.artist_auto_select)
    config.overwrite = args.overwrite
    if args.audio_policy == "lossless-only":
        config.song_codec_piority = [SongCodec.ALAC]
    elif args.audio_policy == "aac-only":
        config.song_codec_piority = [
            SongCodec.AAC,
            SongCodec.AAC_WEB,
            SongCodec.AAC_HE,
            SongCodec.AAC_HE_WEB,
        ]
    else:
        config.song_codec_piority = [
            SongCodec.ALAC,
            SongCodec.AAC,
            SongCodec.AAC_WEB,
            SongCodec.AAC_HE,
            SongCodec.AAC_HE_WEB,
        ]
    config.synced_lyrics_only = False
    config.no_synced_lyrics = False
    config.synced_lyrics_format = SyncedLyricsFormat(args.lyrics_format)

    # N_m3u8DL-RE 0.6.0 needs a real terminal size even when Gamdl captures it.
    os.environ.setdefault("COLUMNS", "120")
    os.environ.setdefault("LINES", "40")
    os.environ.setdefault("TERM", "xterm-256color")

    ffmpeg = resolve_executable(config.ffmpeg_path, "FFmpeg")
    ffprobe = resolve_matching_ffprobe(ffmpeg)
    transcoder = FlacTranscoder(args.freaccmd, ffmpeg, ffprobe)
    preflight = await run_preflight(args, config, transcoder)
    run_log.emit("preflight_completed", durable=True, **preflight)

    run_log.emit(
        "run_configuration",
        gamdl_version=GAMDL_VERSION,
        config_path=str(args.config.resolve()),
        output_path=str(config.output_path),
        temp_path=str(config.temp_path),
        audio_policy=args.audio_policy,
        lyrics_format=config.synced_lyrics_format.value,
        retries=args.retries,
        retry_delay=args.retry_delay,
        overwrite=args.overwrite,
        force=args.force,
        force_track_ids=sorted(set(args.force_track)),
        force_album_ids=sorted(set(args.force_album)),
        history_db=str(history.path),
        playlist_albums=args.playlist_albums,
        download_mode=str(config.download_mode),
        use_wrapper=bool(config.use_wrapper),
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        freaccmd_path=transcoder.freaccmd,
    )
    LOG.info(
        "Using Gamdl %s with %s audio policy", GAMDL_VERSION, args.audio_policy
    )
    LOG.info("Synced lyrics sidecars: %s", config.synced_lyrics_format.value)
    LOG.info("Detailed JSONL log: %s", run_log.path)
    downloader, apple_music_api = await create_gamdl_downloader(config)
    run_log.emit(
        "apple_music_session_validated",
        durable=True,
        active_subscription=bool(apple_music_api.active_subscription),
        storefront=apple_music_api.storefront,
    )
    app = AppleMusicFlacApp(
        downloader=downloader,
        apple_music_api=apple_music_api,
        transcoder=transcoder,
        config=config,
        run_log=run_log,
        history=history,
        retries=args.retries,
        retry_delay=args.retry_delay,
        force=args.force,
        force_track_ids=set(args.force_track),
        force_album_ids=set(args.force_album),
    )
    expanded_album_ids: set[str] = set()
    for url in args.urls:
        if not args.playlist_albums or not is_playlist_url(url):
            await app.process_url(url)
            continue

        album_urls = await app.expand_playlist_to_album_urls(url)
        for album_url in album_urls:
            url_info = AppleMusicInterface.get_url_info(album_url)
            album_id = str(url_info.id) if url_info and url_info.id else album_url
            if album_id in expanded_album_ids:
                run_log.emit(
                    "playlist_album_skipped",
                    source_playlist_url=url,
                    album_url=album_url,
                    album_id=album_id,
                    reason="already_expanded_in_this_run",
                )
                continue
            expanded_album_ids.add(album_id)
            run_log.emit(
                "playlist_album_processing_started",
                source_playlist_url=url,
                album_url=album_url,
                album_id=album_id,
            )
            await app.process_url(album_url)

    stats = app.statistics
    LOG.info(
        "Complete: %d download attempts, %d FLAC, %d AAC/M4A, %d skipped "
        "(%d from history), %d audio failed, %d finalization failed",
        stats.downloaded,
        stats.converted,
        stats.aac_retained,
        stats.skipped,
        stats.history_skipped,
        stats.failed,
        stats.finalization_failed,
    )
    exit_code = 1 if stats.failed or stats.finalization_failed else 0
    run_log.emit(
        "run_summary",
        durable=True,
        download_attempts=stats.downloaded,
        flac_converted=stats.converted,
        aac_retained=stats.aac_retained,
        skipped=stats.skipped,
        history_skipped=stats.history_skipped,
        failed=stats.failed,
        finalization_failed=stats.finalization_failed,
        exit_code=exit_code,
    )
    return exit_code


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)
    try:
        history = DownloadHistory(args.history_db)
    except (OSError, sqlite3.Error, PipelineError) as exc:
        LOG.error("Could not open download history: %s", exc)
        return 1

    if args.repair_isrc_aliases:
        try:
            repair = history.repair_isrc_aliases()
        except (sqlite3.Error, ValueError) as exc:
            LOG.error("Could not repair ISRC aliases: %s", exc)
            history.close()
            return 1
        LOG.info(
            "Repair complete: removed %d cross-release track row(s), "
            "reopened %d album(s)",
            repair["tracks_removed"],
            repair["albums_reopened"],
        )
        if repair["albums_reopened"]:
            LOG.info("Re-run those album URLs to download the missing tracks")
        history.close()
        return 0

    if args.seed_history_only:
        try:
            seed_result = history.seed_from_logs(DEFAULT_LOG_DIR)
        except (OSError, sqlite3.Error, PipelineError) as exc:
            LOG.error("Could not seed download history: %s", exc)
            history.close()
            return 1
        status = history.status()
        LOG.info(
            "History seed complete: %d log(s) imported, %d completed event(s), "
            "%d unique track(s), %d album(s)",
            seed_result["logs_imported"],
            seed_result["completed_events"],
            status["tracks"],
            status["albums"],
        )
        if seed_result["malformed_lines"]:
            LOG.warning(
                "Skipped %d malformed JSONL line(s)",
                seed_result["malformed_lines"],
            )
        history.close()
        return 0

    try:
        run_log = JsonlRunLogger(args.jsonl_log)
    except OSError as exc:
        LOG.error("Could not create JSONL run log: %s", exc)
        history.close()
        return 1

    run_log.emit(
        "history_opened",
        durable=True,
        history_db=str(history.path),
        **history.status(),
    )
    run_log.emit(
        "run_started",
        durable=True,
        pid=os.getpid(),
        hostname=os.uname().nodename,
        working_directory=str(Path.cwd()),
        script_path=str(Path(__file__).resolve()),
        urls=args.urls,
        playlist_albums=args.playlist_albums,
        history_db=str(history.path),
        force=args.force,
        force_track_ids=sorted(set(args.force_track)),
        force_album_ids=sorted(set(args.force_album)),
    )
    try:
        exit_code = asyncio.run(async_main(args, run_log, history))
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        exit_code = 130
        run_log.emit("run_interrupted", durable=True, exit_code=exit_code)
    except Exception as exc:
        LOG.error("Fatal error: %s", exc)
        exit_code = 1
        run_log.emit(
            "run_fatal_error",
            durable=True,
            error_type=type(exc).__name__,
            error=str(exc),
            exit_code=exit_code,
        )
    finally:
        run_log.emit("run_finished", durable=True, exit_code=exit_code)
        run_log.close()
        history.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
