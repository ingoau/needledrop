"""Runtime settings, all overridable through environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(_env("NEEDLEDROP_DATA", "/data"))

    # Folder flow: downloading -> inbox -> (beets) -> library
    downloading_dir: Path = Path(
        _env("DOWNLOADING_DIR", "/mnt/mediastackdata/music/rips/.downloading")
    )
    inbox_dir: Path = Path(_env("INBOX_DIR", "/mnt/mediastackdata/music/rips/apple"))

    # wrapper-v2 (reached over its compose network)
    wrapper_url: str = _env("WRAPPER_URL", "http://wrapper:80")
    wrapper_decrypt_host: str = _env("WRAPPER_DECRYPT_HOST", "wrapper")
    wrapper_decrypt_port: int = _env_int("WRAPPER_DECRYPT_PORT", 10020)
    language: str = _env("APPLE_MUSIC_LANGUAGE", "en-US")

    # beets
    beet_python: str = _env("BEET_PYTHON", "/opt/beets/bin/python")
    beet_driver: str = _env(
        "BEET_DRIVER", "/opt/needledrop/beets/needledrop_beets.py"
    )
    beet_overlay: str = _env(
        "BEET_OVERLAY", "/opt/needledrop/beets/overlay.yaml"
    )
    auto_apply: bool = _env_bool("AUTO_APPLY", True)
    tagger_buffer_seconds: int = _env_int("TAGGER_BUFFER_SECONDS", 30)
    inbox_scan_seconds: int = _env_int("INBOX_SCAN_SECONDS", 10)

    # Optional Navidrome rescan after something reaches the library
    navidrome_url: str = _env("NAVIDROME_URL", "")
    navidrome_user: str = _env("NAVIDROME_USER", "")
    navidrome_password: str = _env("NAVIDROME_PASSWORD", "")

    log_retention_days: int = _env_int("LOG_RETENTION_DAYS", 30)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "needledrop.db"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"


settings = Settings()

AUDIO_EXTENSIONS = {
    ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".aif", ".aiff", ".alac",
    ".wma", ".ape", ".wv",
}
