"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}


class ConfigError(ValueError):
    pass


def _str(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    nc_url: str
    nc_username: str
    nc_app_password: str
    nc_folders: tuple[str, ...]
    nc_verify_tls: bool

    onyx_url: str
    onyx_api_key: str
    onyx_doc_public: bool
    onyx_verify_tls: bool

    sync_interval_seconds: int
    max_file_bytes: int
    state_path: Path
    log_level: str

    request_timeout: float = 60.0
    max_retries: int = 3

    @property
    def nc_dav_root(self) -> str:
        return f"{self.nc_url.rstrip('/')}/remote.php/dav/files/{self.nc_username}"

    @staticmethod
    def from_env() -> Config:
        folders = tuple(
            f.strip().strip("/") for f in os.environ.get("NC_FOLDERS", "").split(",") if f.strip()
        )
        if not folders:
            raise ConfigError("NC_FOLDERS must list at least one folder")
        onyx_url = _str("ONYX_URL").rstrip("/")
        if onyx_url.endswith("/onyx-api/ingestion"):
            raise ConfigError(
                "ONYX_URL must be the API base (e.g. https://host/api), not an endpoint path"
            )
        max_file_mb = _int("MAX_FILE_MB", 25)
        if max_file_mb <= 0:
            raise ConfigError("MAX_FILE_MB must be positive")
        interval = _int("SYNC_INTERVAL_SECONDS", 600)
        if interval <= 0:
            raise ConfigError("SYNC_INTERVAL_SECONDS must be positive")
        log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        return Config(
            nc_url=_str("NC_URL").rstrip("/"),
            nc_username=_str("NC_USERNAME"),
            nc_app_password=_str("NC_APP_PASSWORD"),
            nc_folders=folders,
            nc_verify_tls=_bool("NC_VERIFY_TLS", True),
            onyx_url=onyx_url,
            onyx_api_key=_str("ONYX_API_KEY"),
            onyx_doc_public=_bool("ONYX_DOC_PUBLIC", False),
            onyx_verify_tls=_bool("ONYX_VERIFY_TLS", True),
            sync_interval_seconds=interval,
            max_file_bytes=max_file_mb * 1024 * 1024,
            state_path=Path(os.environ.get("STATE_PATH", "/data/state.db")),
            log_level=log_level,
        )
