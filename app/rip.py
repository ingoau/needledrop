"""Needledrop's download engine.

Run as a subprocess per job:

    python -m app.rip --url URL --output DIR --history-db FILE
                      [--playlist-albums] [--force] [--audio-policy POLICY]

It downloads through gamdl (authenticated by wrapper-v2), turns ALAC into
verified FLAC, keeps gamdl's synced lyrics sidecar next to each track, and
reports progress as one JSON object per line on stdout. Human-readable
diagnostics (and any library output) go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import structlog

# Events get their own copy of stdout; everything else that writes to fd 1
# (yt-dlp progress bars, gamdl, ffmpeg) is sent to stderr instead.
_EVENTS = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

from gamdl.api import AppleMusicApi  # noqa: E402
from gamdl.api.wrapper import WrapperApi  # noqa: E402
from gamdl.downloader import (  # noqa: E402
    AppleMusicBaseDownloader,
    AppleMusicDownloader,
    AppleMusicMusicVideoDownloader,
    AppleMusicSongDownloader,
    AppleMusicUploadedVideoDownloader,
)
from gamdl.downloader.enums import DownloadMode, RemuxFormatMusicVideo  # noqa: E402
from gamdl.interface import (  # noqa: E402
    AppleMusicBaseInterface,
    AppleMusicInterface,
    AppleMusicMusicVideoInterface,
    AppleMusicSongInterface,
    AppleMusicUploadedVideoInterface,
)
from gamdl.interface.enums import (  # noqa: E402
    CoverFormat,
    MusicVideoCodec,
    MusicVideoResolution,
    SongCodec,
    SyncedLyricsFormat,
    UploadedVideoQuality,
)

AUDIO_POLICIES = {
    "prefer-lossless": [SongCodec.ALAC, SongCodec.AAC, SongCodec.AAC_WEB,
                        SongCodec.AAC_HE, SongCodec.AAC_HE_WEB],
    "lossless-only": [SongCodec.ALAC],
    "aac-only": [SongCodec.AAC, SongCodec.AAC_WEB, SongCodec.AAC_HE,
                 SongCodec.AAC_HE_WEB],
}

# Opinionated layout, matching what beets and filetote expect downstream.
TEMPLATES = dict(
    album_folder_template="{album_artist}/{album}",
    compilation_folder_template="Compilations/{album}",
    no_album_folder_template="{artist}/Unknown Album",
    playlist_folder_template="Playlists/{playlist_artist}",
    single_disc_file_template="{track:02d} {title}",
    multi_disc_file_template="{disc}-{track:02d} {title}",
    no_album_file_template="{title}",
    playlist_file_template="{playlist_title}",
    date_tag_template="%Y-%m-%dT%H:%M:%SZ",
)

SKIP_ERRORS = (
    "GamdlInterfaceMediaNotStreamableError",
    "GamdlInterfaceFormatNotAvailableError",
    "GamdlInterfaceDecryptionNotAvailableError",
    "GamdlInterfaceMediaNotAllowedError",
    "GamdlInterfaceFlatFilterExcludedError",
)

ATTEMPTS = 3


def emit(event: str, **data: Any) -> None:
    _EVENTS.write(json.dumps({"event": event, "ts": time.time(), **data}, default=str) + "\n")
    _EVENTS.flush()


def note(message: str) -> None:
    print(time.strftime("%H:%M:%S ") + message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- transcoding


class TranscodeError(RuntimeError):
    pass


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        raise TranscodeError(f"{Path(args[0]).name} failed: {' / '.join(tail)}")
    return result.stdout


def probe(path: Path) -> dict[str, Any]:
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,bits_per_raw_sample,"
        "bits_per_sample,sample_fmt:format=duration",
        "-of", "json", str(path),
    ])
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    return {
        "codec": stream.get("codec_name"),
        "sample_rate": stream.get("sample_rate"),
        "channels": stream.get("channels"),
        "bits": stream.get("bits_per_raw_sample") or stream.get("bits_per_sample"),
        "duration": float((data.get("format") or {}).get("duration") or 0),
    }


def pcm_hash(path: Path) -> str:
    out = _run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-map", "0:a:0", "-f", "hash", "-hash", "sha256", "-",
    ])
    return out.strip()


def to_flac(source: Path, freaccmd: str | None,
            tags: dict[str, str] | None = None) -> Path:
    """Convert an ALAC .m4a into a verified FLAC next to it; remove the source.

    `tags` are extra Vorbis comments, e.g. the Apple Music ids that let
    Needledrop find the release again later.
    """
    src = probe(source)
    target = source.with_suffix(".flac")
    scratch = source.parent / ".needledrop-tmp"
    scratch.mkdir(exist_ok=True)
    token = uuid.uuid4().hex[:8]
    native = scratch / f"{token}.native.flac"
    final = scratch / f"{token}.final.flac"
    try:
        # fre:ac's decoder copes with Apple ALAC packet layouts that FFmpeg
        # occasionally rejects; FFmpeg is the fallback.
        if freaccmd:
            _run([freaccmd, "-e", "flac", "-o", str(native), str(source)])
        else:
            _run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(source),
                  "-map", "0:a:0", "-c:a", "flac", str(native)])

        decoded = probe(native)
        for key in ("sample_rate", "channels", "bits"):
            if str(decoded[key]) != str(src[key]):
                raise TranscodeError(
                    f"{key} changed in decode: {src[key]} -> {decoded[key]}"
                )
        if abs(decoded["duration"] - src["duration"]) > 0.05:
            raise TranscodeError(
                f"duration changed in decode: {src['duration']} -> {decoded['duration']}"
            )
        native_hash = pcm_hash(native)

        # Copy tags and artwork from the source; never re-encode the audio.
        _run([
            "ffmpeg", "-nostdin", "-y", "-v", "error", "-xerror",
            "-i", str(native), "-i", str(source),
            "-map", "0:a:0", "-map", "1:v?", "-map_metadata", "1",
            "-c", "copy", "-disposition:v", "attached_pic",
            "-metadata:s:v", "comment=Cover (front)",
            *[arg for key, value in (tags or {}).items() if value
              for arg in ("-metadata", f"{key}={value}")],
            str(final),
        ])
        if pcm_hash(final) != native_hash:
            raise TranscodeError("audio changed while copying metadata")

        os.replace(final, target)
        source.unlink()
        return target
    finally:
        native.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        try:
            scratch.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------- history


class History:
    def __init__(self, path: str | None) -> None:
        self.conn = None
        if path and Path(path).exists():
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)

    def has(self, media_id: str) -> bool:
        if self.conn is None:
            return False
        try:
            row = self.conn.execute(
                "SELECT 1 FROM history WHERE media_id = ?", (media_id,)
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None


# ---------------------------------------------------------------- gamdl


async def build(args: argparse.Namespace, output: Path):
    wrapper = await WrapperApi.create(
        base_url=args.wrapper_url,
        decrypt_host=args.wrapper_decrypt_host,
        decrypt_port=args.wrapper_decrypt_port,
    )
    api = await AppleMusicApi.create_from_wrapper(
        wrapper_api=wrapper, language=args.language
    )
    if not api.active_subscription:
        raise RuntimeError("The wrapper's Apple Music account has no active subscription")

    base_interface = await AppleMusicBaseInterface.create(
        apple_music_api=api,
        cover_format=CoverFormat.JPG,
        cover_size=args.cover_size,
        wrapper_api=wrapper,
    )

    async def never(*_a, **_k):
        raise RuntimeError("interactive selection is not available")

    song_interface = AppleMusicSongInterface(
        base=base_interface,
        synced_lyrics_format=SyncedLyricsFormat(args.lyrics_format),
        codec_priority=AUDIO_POLICIES[args.audio_policy],
        use_album_date=True,
        skip_stream_info=False,
        ask_codec_function=never,
    )
    interface = AppleMusicInterface(
        song=song_interface,
        music_video=AppleMusicMusicVideoInterface(
            base=base_interface,
            resolution=MusicVideoResolution.R1080P,
            codec_priority=[MusicVideoCodec.H264],
            ask_video_codec_function=never,
            ask_audio_codec_function=never,
        ),
        uploaded_video=AppleMusicUploadedVideoInterface(
            base=base_interface,
            quality=UploadedVideoQuality.BEST,
            ask_quality_function=never,
        ),
        disallowed_media_types=["artist"],
    )
    base_downloader = AppleMusicBaseDownloader(
        interface=interface,
        output_path=str(output),
        temp_path=str(output / ".gamdl-temp"),
        nm3u8dlre_path="N_m3u8DL-RE",
        ffmpeg_path="ffmpeg",
        download_mode=DownloadMode.YTDLP,
        exclude_tags=None,
        truncate=args.truncate,
        **TEMPLATES,
    )
    song_downloader = AppleMusicSongDownloader(base=base_downloader)
    downloader = AppleMusicDownloader(
        song=song_downloader,
        music_video=AppleMusicMusicVideoDownloader(
            base=base_downloader, remux_format=RemuxFormatMusicVideo.M4V
        ),
        uploaded_video=AppleMusicUploadedVideoDownloader(base=base_downloader),
        overwrite=False,
        save_cover=False,
        save_playlist=False,
        no_synced_lyrics=False,
        synced_lyrics_only=False,
    )
    return api, interface, downloader


async def playlist_album_urls(api: AppleMusicApi, playlist_id: str) -> list[str]:
    """Albums of every song on a playlist, in playlist order, de-duplicated."""
    data = (await api.get_playlist(playlist_id))["data"][0]
    tracks = list(((data.get("relationships") or {}).get("tracks") or {}).get("data") or [])
    next_href = (((data.get("relationships") or {}).get("tracks") or {}).get("next"))
    while next_href:
        page = await api._amp_request(next_href)
        tracks += page.get("data") or []
        next_href = page.get("next")

    seen: list[str] = []
    for track in tracks:
        if track.get("type") not in ("songs", "library-songs"):
            continue
        url = (track.get("attributes") or {}).get("url") or ""
        match = re.search(r"/album/[^/]+/(\d+)", url)
        if match and match.group(1) not in seen:
            seen.append(match.group(1))
    return [f"https://music.apple.com/{api.storefront}/album/{a}" for a in seen]


# ---------------------------------------------------------------- main loop


async def run(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    history = History(args.history_db)
    freaccmd = args.freaccmd if args.freaccmd and shutil.which(args.freaccmd) else None
    if not freaccmd:
        note("fre:ac not found; decoding ALAC with FFmpeg")

    api, interface, downloader = await build(args, output)
    emit("session", storefront=api.storefront)

    urls = [args.url]
    playlist = re.search(r"/playlist/(?:[^/]+/)?(pl\.[^/?#]+)", args.url)
    if playlist and args.playlist_albums:
        urls = await playlist_album_urls(api, playlist.group(1))
        emit("expanded", albums=len(urls))

    stats = {"completed": 0, "skipped": 0, "failed": 0}
    for url in urls:
        note(f"Processing {url}")
        try:
            async for media in interface.get_media_from_url(url):
                await handle(media, args, downloader, history, freaccmd, stats)
        except Exception as exc:
            stats["failed"] += 1
            emit("url_failed", url=url, error=f"{type(exc).__name__}: {exc}")
            note(f"Could not resolve {url}: {exc}")

    emit("summary", **stats)
    return 0 if stats["failed"] == 0 else 1


async def handle(media, args, downloader, history: History, freaccmd, stats) -> None:
    meta = media.media_metadata or {}
    kind = meta.get("type")
    attrs = meta.get("attributes") or {}

    if media.partial:
        if kind in ("albums", "library-albums"):
            total = attrs.get("trackCount")
            if args.only:
                tracks = ((meta.get("relationships") or {}).get("tracks") or {}).get("data") or []
                total = sum(1 for t in tracks if t.get("id") in args.only)
            emit("album", id=media.media_id, name=attrs.get("name"),
                 artist=attrs.get("artistName"), total=total)
        elif kind in ("playlists", "library-playlists"):
            tracks = ((meta.get("relationships") or {}).get("tracks") or {}).get("data") or []
            emit("playlist", id=media.media_id, name=attrs.get("name"), total=len(tracks))
        if media.error:
            raise media.error
        return

    title = attrs.get("name") or media.media_id
    info = {
        "media_id": media.media_id,
        "title": title,
        "artist": attrs.get("artistName"),
        "album": attrs.get("albumName"),
        "isrc": attrs.get("isrc"),
        "index": media.index + 1,
        "total": media.total,
    }

    if kind not in ("songs", "library-songs"):
        stats["skipped"] += 1
        emit("track_skipped", reason="not a song", **info)
        return
    if args.only and media.media_id not in args.only:
        return
    if not args.force and history.has(media.media_id):
        stats["skipped"] += 1
        emit("track_skipped", reason="history", **info)
        return

    emit("track_started", **info)
    last_error = None
    for attempt in range(1, ATTEMPTS + 1):
        item = None
        try:
            item = await downloader.parse_download_item(media)
            if item.media.error:
                raise item.media.error
            final = Path(item.final_path)
            flac = final.with_suffix(".flac")
            if flac.exists() or final.exists():
                stats["skipped"] += 1
                emit("track_skipped", reason="file exists",
                     path=str(flac if flac.exists() else final), **info)
                return
            await downloader.download(item)
            if not final.exists():
                raise RuntimeError("download produced no file")
            codec = probe(final)["codec"]
            path = final
            if codec == "alac":
                path = await asyncio.to_thread(to_flac, final, freaccmd, {
                    "apple_music_id": media.media_id,
                    "apple_music_album_id": _album_id(item),
                })
                codec = "flac"
            stats["completed"] += 1
            emit("track_completed", path=str(path), codec=codec,
                 album_id=_album_id(item), **info)
            return
        except Exception as exc:
            name = type(exc).__name__
            last_error = f"{name}: {exc}"
            if name in SKIP_ERRORS:
                stats["skipped"] += 1
                emit("track_skipped", reason=str(exc) or name, **info)
                return
            note(f"Attempt {attempt}/{ATTEMPTS} failed for {title}: {last_error}")
            if attempt < ATTEMPTS:
                await asyncio.sleep(2 ** attempt)

    stats["failed"] += 1
    emit("track_failed", error=last_error, **info)


def _album_id(item) -> str | None:
    tags = item.media.tags
    return str(tags.album_id) if tags and tags.album_id else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--history-db")
    parser.add_argument("--playlist-albums", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", type=lambda v: set(v.split(",")), default=None,
                        help="comma-separated song ids: download just these")
    parser.add_argument("--audio-policy", default="prefer-lossless",
                        choices=sorted(AUDIO_POLICIES))
    parser.add_argument("--lyrics-format", default="ttml",
                        choices=[f.value for f in SyncedLyricsFormat])
    parser.add_argument("--cover-size", type=int, default=1200)
    parser.add_argument("--truncate", type=int, default=80)
    parser.add_argument("--freaccmd", default=os.environ.get("FREACCMD", "freaccmd"))
    parser.add_argument("--wrapper-url", default=os.environ.get("WRAPPER_URL", "http://wrapper:80"))
    parser.add_argument("--wrapper-decrypt-host",
                        default=os.environ.get("WRAPPER_DECRYPT_HOST", "wrapper"))
    parser.add_argument("--wrapper-decrypt-port", type=int,
                        default=int(os.environ.get("WRAPPER_DECRYPT_PORT", "10020")))
    parser.add_argument("--language", default=os.environ.get("APPLE_MUSIC_LANGUAGE", "en-US"))
    args = parser.parse_args()

    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        emit("cancelled")
        code = 130
    except Exception as exc:
        emit("fatal", error=f"{type(exc).__name__}: {exc}")
        note(f"Fatal: {type(exc).__name__}: {exc}")
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
