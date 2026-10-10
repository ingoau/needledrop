"""Apple Music catalogue access through gamdl's API, authenticated by wrapper-v2."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from gamdl.api import AppleMusicApi
from gamdl.api.wrapper import WrapperApi

from .config import settings

URL_RE = re.compile(
    r"music\.apple\.com/(?P<storefront>[a-z]{2})/"
    r"(?P<type>album|song|playlist|artist|music-video)/"
    r"(?:[^/?#]+/)?(?P<id>[^/?#]+)(?:\?(?P<query>[^#]*))?"
)

TYPE_MAP = {
    "songs": "song",
    "albums": "album",
    "artists": "artist",
    "playlists": "playlist",
}


class Catalog:
    def __init__(self) -> None:
        self._api: AppleMusicApi | None = None
        self._lock = asyncio.Lock()

    async def api(self, refresh: bool = False) -> AppleMusicApi:
        async with self._lock:
            if self._api is None or refresh:
                wrapper = await WrapperApi.create(
                    base_url=settings.wrapper_url,
                    decrypt_host=settings.wrapper_decrypt_host,
                    decrypt_port=settings.wrapper_decrypt_port,
                )
                self._api = await AppleMusicApi.create_from_wrapper(
                    wrapper_api=wrapper, language=settings.language
                )
            return self._api

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> dict:
        api = await self.api()
        try:
            return await getattr(api, name)(*args, **kwargs)
        except Exception:
            # Tokens from the wrapper can expire; retry once with fresh ones.
            api = await self.api(refresh=True)
            return await getattr(api, name)(*args, **kwargs)

    async def status(self) -> dict[str, Any]:
        api = await self.api()
        return {
            "storefront": api.storefront,
            "subscription": api.active_subscription,
        }

    async def search(self, term: str, types: str) -> dict[str, list[dict]]:
        raw = await self._call(
            "get_search_results", term=term, types=types, limit=25
        )
        results: dict[str, list[dict]] = {}
        for key, block in (raw.get("results") or {}).items():
            if key not in TYPE_MAP:
                continue
            results[TYPE_MAP[key]] = [
                normalize(item) for item in block.get("data", [])
            ]
        return results

    async def artist(self, artist_id: str) -> dict[str, Any]:
        raw = await self._call("get_artist", artist_id)
        data = raw["data"][0]
        views = data.get("views", {})
        sections = []
        for key, title in (
            ("full-albums", "Albums"),
            ("singles", "Singles & EPs"),
            ("live-albums", "Live"),
            ("compilation-albums", "Compilations"),
            ("top-songs", "Top songs"),
        ):
            items = (views.get(key) or {}).get("data") or []
            if items:
                sections.append(
                    {"key": key, "title": title,
                     "items": [normalize(item) for item in items]}
                )
        return {"artist": normalize(data), "sections": sections}

    async def album(self, album_id: str) -> dict[str, Any]:
        raw = await self._call("get_album", album_id)
        data = raw["data"][0]
        tracks = (
            (data.get("relationships") or {}).get("tracks") or {}
        ).get("data") or []
        return {
            "album": normalize(data),
            "tracks": [normalize(t) for t in tracks],
        }

    async def playlist(self, playlist_id: str) -> dict[str, Any]:
        raw = await self._call("get_playlist", playlist_id)
        data = raw["data"][0]
        tracks = (
            (data.get("relationships") or {}).get("tracks") or {}
        ).get("data") or []
        return {
            "playlist": normalize(data),
            "tracks": [normalize(t) for t in tracks],
        }

    async def song(self, song_id: str) -> dict[str, Any]:
        raw = await self._call("get_song", song_id)
        return normalize(raw["data"][0])

    async def resolve(self, url: str) -> dict[str, Any] | None:
        """Turn a music.apple.com link into a normalized item."""
        match = URL_RE.search(url)
        if not match:
            return None
        kind, ident = match["type"], match["id"]
        query = match["query"] or ""
        song_in_album = re.search(r"(?:^|&)i=(\d+)", query)
        if kind == "album" and song_in_album:
            return await self.song(song_in_album.group(1))
        if kind == "song":
            return await self.song(ident)
        if kind == "album":
            return (await self.album(ident))["album"]
        if kind == "playlist":
            return (await self.playlist(ident))["playlist"]
        if kind == "artist":
            return (await self.artist(ident))["artist"]
        return None


def artwork(attrs: dict[str, Any], size: int = 300) -> str | None:
    art = attrs.get("artwork") or {}
    url = art.get("url")
    if not url:
        return None
    return url.replace("{w}", str(size)).replace("{h}", str(size)).replace(
        "{f}", "jpg"
    ).replace("{c}", "")


def normalize(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") or {}
    kind = TYPE_MAP.get(item.get("type", ""), item.get("type", ""))
    release = attrs.get("releaseDate") or ""
    out: dict[str, Any] = {
        "type": kind,
        "id": item.get("id"),
        "name": attrs.get("name"),
        "artist": attrs.get("artistName") or attrs.get("curatorName"),
        "year": release[:4] or None,
        "artwork": artwork(attrs),
        "url": attrs.get("url"),
        "explicit": attrs.get("contentRating") == "explicit",
        "streamable": bool(attrs.get("playParams")),
    }
    if kind == "song":
        out["album"] = attrs.get("albumName")
        out["duration_ms"] = attrs.get("durationInMillis")
        out["track_number"] = attrs.get("trackNumber")
        out["disc_number"] = attrs.get("discNumber")
    if kind == "album":
        out["track_count"] = attrs.get("trackCount")
        out["is_single"] = attrs.get("isSingle")
        out["is_compilation"] = attrs.get("isCompilation")
        out["record_label"] = attrs.get("recordLabel")
    if kind == "playlist":
        out["description"] = (attrs.get("description") or {}).get("short")
    if kind == "artist":
        out["genres"] = attrs.get("genreNames")
    return out


catalog = Catalog()
