"""Local query fixes for the beets MusicBrainz and Spotify plugins.

This plugin changes lookup queries only. It does not alter source-file tags or
the metadata returned by either provider.
"""

from __future__ import annotations

import re
from itertools import starmap

from beets import logging
from beets.metadata_plugins import QueryType
from beets.plugins import BeetsPlugin
from beetsplug._utils.musicbrainz import MusicBrainzAPI
from beetsplug.spotify import SpotifyPlugin


log = logging.getLogger("beets.searchfix")
SINGLE_SUFFIX = re.compile(r"\s+-\s+Single$", flags=re.IGNORECASE)


def _musicbrainz_search_with_and(self, entity, filters, **kwargs):
    """Build a MusicBrainz query whose provider filters are all required."""
    query = " AND ".join(
        filter(None, starmap(self.format_search_term, filters.items()))
    )
    log.debug("Searching for MusicBrainz {}s with fixed query: {!r}", entity, query)
    kwargs["query"] = query
    normalised_entity = entity.replace("-", "_")
    return self._get_resource(entity, **kwargs)[f"{normalised_entity}s"]


def _spotify_query_without_single_suffix(
    self,
    query_type: QueryType,
    items,
    artist: str,
    name: str,
    va_likely: bool,
):
    """Remove Apple's display suffix from Spotify album searches only."""
    if query_type == "album":
        cleaned_name = SINGLE_SUFFIX.sub("", name)
        if cleaned_name != name:
            log.debug(
                "Spotify album query normalized from {!r} to {!r}",
                name,
                cleaned_name,
            )
        name = cleaned_name

    original = SpotifyPlugin._searchfix_original_query_builder
    return original(self, query_type, items, artist, name, va_likely)


class SearchFixPlugin(BeetsPlugin):
    """Install the local provider-query fixes once per beets process."""

    def __init__(self) -> None:
        super().__init__()

        if not hasattr(MusicBrainzAPI, "_searchfix_original_search"):
            MusicBrainzAPI._searchfix_original_search = MusicBrainzAPI.search
            MusicBrainzAPI.search = _musicbrainz_search_with_and

        if not hasattr(SpotifyPlugin, "_searchfix_original_query_builder"):
            SpotifyPlugin._searchfix_original_query_builder = (
                SpotifyPlugin.get_search_query_with_filters
            )
            SpotifyPlugin.get_search_query_with_filters = (
                _spotify_query_without_single_suffix
            )

