# Needledrop

Search Apple Music, rip it to verified FLAC, and triage what beets makes of
it — from one web page.

```
Download ──► .downloading/<job> ──► inbox/ ──► 30 s quiet ──► beets lookup
 (search)     gamdl + fre:ac → FLAC   moved when                 strong match → applied after a 10 s undo window
                                      every track is there       anything else → Inbox triage
```

It's opinionated: lossless first, FLAC on disk, synced lyrics as `.ttml`
sidecars, MusicBrainz as the main source, and nothing reaches your library
without either a strong match or you saying so.

## What you need

- [wrapper-v2](https://github.com/glomatico/wrapper-v2) running and
  logged in to an Apple Music account with an active subscription.
  Needledrop joins its compose network and talks to `wrapper:80` and
  `wrapper:10020`.
- Docker with Compose.
- Something in front for authentication if it's reachable beyond localhost.
  Needledrop has no logins of its own.

## Quick start

```bash
git clone https://github.com/ingoau/needledrop && cd needledrop
cp .env.example .env        # point MUSIC_PATH at your music
mkdir -p data music beets   # before the first start, so they're yours and not root's
docker compose up -d --build
```

Open http://localhost:8080. Under `MUSIC_PATH` Needledrop uses:

| Folder | |
| --- | --- |
| `.downloading/` | one folder per running job |
| `inbox/` | finished downloads, and anything else you drop in |
| `library/` | where beets files things (the `directory` in its config) |

The beets folder (`BEETS_PATH`) gets a default `config.yaml` on first start:
MusicBrainz + chroma + Spotify + filetote, `move: yes`. Edit it freely.

## Using an existing beets setup

Mount your beets folder as `BEETS_PATH`. Needledrop layers a small overlay on
top (`data/beets-overlay.yaml`): its bundled plugins come first, and a
`library:` path that only exists on the host is pointed at the mounted
folder instead. If your `library.db` stores host paths, mount the music at
the same path inside the container (`MUSIC_MOUNT=$MUSIC_PATH`).

beets is pinned (2.13.1 with filetote, importreplace, pyacoustid) so plugins
built against it keep working. The bundled `searchfix` plugin tidies
MusicBrainz and Spotify queries for Apple Music releases (drops the
" - Single" suffix and requires every search field to match).

## The Inbox

Items are sorted by their best match. The triage view is built for the
keyboard: move with `j`/`k`, flip candidates with `←`/`→`, `a` applies,
`u` imports as-is, `t` switches to single tracks, `i` searches by
MusicBrainz/Spotify ID, `?` shows everything. Every apply — including
automatic ones — waits 10 seconds first, and `Ctrl/⌘+Z` takes it back.

Each candidate shows a beets-style diff of artist, album and every track,
plus the penalties that kept it from being a strong match. Spotify
candidates are penalised in the default config, so they never auto-apply.

## Download history

Tracks Needledrop has downloaded are skipped by later jobs ("already
downloaded"); *Download again* forces them. You can import an existing
history from the Log tab, or:

```bash
docker compose exec needledrop python -m app.history import /data/old.sqlite3
```

Supported: apple-music-to-flac's `download-history.sqlite3` and gamdl's
`--database-path` file.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `MUSIC_PATH` / `MUSIC_MOUNT` | `./music` / `/music` | host folder and where it's mounted |
| `BEETS_PATH` | `./beets` | beets config folder |
| `INBOX_DIR`, `DOWNLOADING_DIR`, `LIBRARY_DIR` | under `MUSIC_MOUNT` | override the layout |
| `AUTO_APPLY` | `true` | `false`: only show matches |
| `UNDO_SECONDS` | `10` | how long an apply can be undone |
| `TAGGER_BUFFER_SECONDS` | `30` | quiet time before an inbox folder is tagged |
| `AUDIO_POLICY` | `prefer-lossless` | or `lossless-only`, `aac-only` |
| `WRAPPER_NETWORK` | `wrapper-v2_default` | wrapper-v2's compose network |
| `PUID` / `PGID` | `1000` | owner of your music files |
| `PORT` / `BIND_ADDRESS` | `8080` / `127.0.0.1` | |
| `LOG_RETENTION_DAYS` | `30` | |

Behind a reverse proxy, see `compose.override.example.yaml`.

## How a download works

`app/rip.py` runs once per job: gamdl fetches and decrypts through
wrapper-v2, then ALAC is decoded with fre:ac (more tolerant of Apple's ALAC
than FFmpeg, which is the fallback) and remuxed to FLAC with the original
tags and artwork. The result must match the source's sample rate, channels,
bit depth and duration, and the audio hash must survive the remux, before
the `.m4a` is deleted. AAC-only tracks stay `.m4a`.
