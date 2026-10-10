# Needledrop

Search Apple Music, rip it to verified FLAC, and let beets file it into the
library — with an Inbox for the matches that need a human.

```
Download page ──► .downloading/<job> ──► inbox (rips/apple) ──► buffer ──► beets
   (search)        apple-music-to-flac.py   moved when every      30 s     strong match → library
                                            track is present               otherwise → Inbox page
```

One container, one compose stack. Auth is left to the reverse proxy
(Traefik + the `notoverkill-sso` middleware).

## How it maps onto mini-server

| Piece | Where it comes from |
| --- | --- |
| Downloads | `downloader/apple-music-to-flac.py`, vendored from `/srv/scripts/music` (gamdl 3.8.5 → fre:ac native ALAC decode → verified FLAC + `.ttml`). One job at a time, each in its own folder. |
| wrapper-v2 | The existing `wrapper-v2` stack, reached on its compose network as `wrapper:80` / `wrapper:10020`. |
| Download history | Shared with the `apple-music-to-rip` timer (`/srv/scripts/music/download-history.sqlite3`), so neither re-rips what the other already did. A job for something already ripped ends as **skipped**; use *Download again* to force it. |
| Inbox | `rips/apple` — the same folder the timer writes to, so timer rips show up in the Inbox too. The tagger holds off while the timer's scratch folders show activity. |
| beets | Pinned to the host versions (2.13.1, filetote 1.3.7, importreplace @2069316, pyacoustid 1.3.1) with fpcalc. Uses your `~/.config/beets/config.yaml` and `library.db` unchanged, mounted at the same path. The `searchfix` plugin is baked into the image (`beets/beetsplug/searchfix.py`) instead of loaded from the share. |

## Tagging rules

- The automatic pass applies only beets' **strong** recommendation (distance
  below `strong_rec_thresh`, the same rule as `beet import -q`). Everything
  else waits on the Inbox page with its candidates, similarity and penalties.
- Spotify candidates carry a 1.0 source penalty in your config, so they never
  auto-apply. That's expected: apply them by hand.
- Duplicates are skipped automatically and flagged; from the Inbox you can
  keep both, replace the old one, or merge.
- When nothing fits: search by MusicBrainz or Spotify ID/link, re-run, or
  import as-is. Single songs pulled from a larger album are imported as
  singletons.
- Every action, automatic or manual, goes to the Log (pruned after 30 days).

## Running

```bash
cp .env.example .env   # optional
docker compose up -d --build
```

Served at `needledrop.notoverkill.com` via Traefik's default host rule.

### Settings

| Variable | Default | |
| --- | --- | --- |
| `AUTO_APPLY` | `true` | `false` = only show matches, never import on its own |
| `TAGGER_BUFFER_SECONDS` | `30` | quiet time before an inbox folder is tagged |
| `INBOX_DIR` | `/mnt/mediastackdata/music/rips/apple` | |
| `DOWNLOADING_DIR` | `/mnt/mediastackdata/music/rips/.downloading` | same share as the library so moves are renames |
| `AUDIO_POLICY` | `prefer-lossless` | or `lossless-only`, `aac-only` |
| `HISTORY_DB` | `/srv/scripts/music/download-history.sqlite3` | |
| `LOG_RETENTION_DAYS` | `30` | |

## Updating the vendored pieces

- Rip script: copy `/srv/scripts/music/apple-music-to-flac.py` over
  `downloader/apple-music-to-flac.py`. It uses private gamdl APIs, so bump
  `gamdl` in `downloader/requirements.txt` only together with the script.
- `searchfix`: copy over `beets/beetsplug/searchfix.py`; it targets beets
  2.13.1 internals.
