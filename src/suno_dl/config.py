"""Configuration loading and validation for suno-dl."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, cast

from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

from .blocklist import DEFAULT_BLOCKED_FILENAME
from .errors import ConfigError

URLType = Literal["profile", "playlist", "song"]


class Config(BaseModel):
    """Runtime configuration, loaded from env vars / .env plus CLI overrides."""

    default_handle: str = Field(default="", description="Suno profile handle without @")
    default_url_type: URLType = Field(default="profile")
    default_url: str = Field(default="", description="Explicit default Suno URL")
    concurrency: int = Field(default=1, gt=0, le=20)
    timeout_sec: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    rate_limit_sec: float = Field(default=1.5, ge=0.0, le=60.0)
    user_agent: str = Field(default="")
    output_dir: Path = Field(default=Path("./downloads"))
    pattern: str = Field(default="{artist}_{title}")
    watch: list[str] = Field(
        default_factory=list,
        description="Handles or URLs synced by ``suno-dl --sync``.",
    )
    blocked_file: Path = Field(
        default=Path(DEFAULT_BLOCKED_FILENAME),
        description="Blocklist file: clip IDs never fetched (see suno_dl.blocklist).",
    )
    download_videos: bool = Field(
        default=False,
        description="When True, allow MP4 video track downloads. Off by default.",
    )
    covers: bool = Field(
        default=True,
        description="Fetch cover art (jpeg/png) alongside audio. On by default.",
    )
    max_tracks: int = Field(
        default=0,
        ge=0,
        description="Keep only the newest N tracks in the archive (library cap). "
        "0 disables the cap. See suno_dl.rotate.",
    )
    max_disk_mb: int = Field(
        default=0,
        ge=0,
        description="Cap the archive at N MiB. 0 disables the cap. See suno_dl.rotate.",
    )
    rotate: bool = Field(
        default=False,
        description="With a cap set, delete the oldest tracks after a sync until "
        "the archive fits. Off (default) never deletes: downloads stop at the cap.",
    )
    min_free_mb: int = Field(
        default=2048, ge=0, description="Minimum free space in MB on target volume before stopping."
    )
    sidecar: bool = Field(
        default=True,
        description="Write a <stem>.json id/metadata sidecar beside each track. On by default.",
    )

    @field_validator("output_dir", mode="before")
    @classmethod
    def _expand_path(cls, v: object) -> Path:
        return Path(str(v)).expanduser()

    @model_validator(mode="after")
    def _require_default_source(self) -> Config:
        if not self.default_url and not self.default_handle and not self.watch:
            raise ValueError(
                "Neither SUNO_DEFAULT_URL nor SUNO_DEFAULT_HANDLE is set; "
                "set one in .env, pass --url explicitly, or set SUNO_WATCH."
            )
        return self

    def build_default_url(self) -> str:
        """Resolve the effective URL when no --url is passed."""
        if self.default_url:
            return self.default_url
        handle = self.default_handle.lstrip("@")
        if self.default_url_type == "profile":
            return f"https://suno.com/@{handle}"
        # playlist/song types need an explicit URL id; handle alone is not enough
        raise ConfigError(
            f"SUNO_DEFAULT_URL_TYPE={self.default_url_type!r} requires SUNO_DEFAULT_URL "
            "(handle alone cannot identify a playlist/song)."
        )


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def ensure_dotenv_loaded() -> None:
    """Load ``.env`` from the working directory into the process environment.

    Searches from the current working directory (walking up), not the package
    location: a .env next to where the user runs the tool must win. Safe to
    call repeatedly and at import time — existing environment variables are
    never overridden. Every CLI entry point calls this so that click's
    ``envvar=`` defaults (resolved before the callback runs) can pick values
    up from .env.
    """
    load_dotenv(find_dotenv(usecwd=True))


def load_config(**overrides: object) -> Config:
    """Load .env then apply keyword overrides (unset/None values ignored)."""
    ensure_dotenv_loaded()
    base: dict[str, object] = {
        "default_handle": _env_str("SUNO_DEFAULT_HANDLE", ""),
        "default_url_type": _env_str("SUNO_DEFAULT_URL_TYPE", "profile"),
        "default_url": _env_str("SUNO_DEFAULT_URL", ""),
        "concurrency": _env_int("SUNO_CONCURRENCY", 1),
        "timeout_sec": _env_float("SUNO_TIMEOUT_SEC", 30.0),
        "max_retries": _env_int("SUNO_MAX_RETRIES", 3),
        "rate_limit_sec": _env_float("SUNO_RATE_LIMIT_SEC", 1.5),
        "user_agent": _env_str("SUNO_USER_AGENT", ""),
        "output_dir": _env_str("SUNO_OUTPUT_DIR", "./downloads"),
        "pattern": "{artist}_{title}",
        "watch": [w.strip() for w in _env_str("SUNO_WATCH", "").split(",") if w.strip()],
        "blocked_file": _env_str("SUNO_BLOCKED_FILE", DEFAULT_BLOCKED_FILENAME),
        "download_videos": _env_bool("SUNO_DOWNLOAD_VIDEOS", False),
        "covers": _env_bool("SUNO_COVERS", True),
        "max_tracks": _env_int("SUNO_MAX_TRACKS", 0),
        "max_disk_mb": _env_int("SUNO_MAX_DISK_MB", 0),
        "rotate": _env_bool("SUNO_ROTATE", False),
        "min_free_mb": _env_int("SUNO_MIN_FREE_MB", 2048),
        "sidecar": _env_bool("SUNO_SIDECAR", True),
    }
    for key, value in overrides.items():
        if value is not None:
            base[key] = value
    return Config(
        default_handle=str(base["default_handle"]),
        default_url_type=cast(URLType, str(base["default_url_type"])),
        default_url=str(base["default_url"]),
        concurrency=int(str(base["concurrency"])),
        timeout_sec=float(str(base["timeout_sec"])),
        max_retries=int(str(base["max_retries"])),
        rate_limit_sec=float(str(base["rate_limit_sec"])),
        user_agent=str(base["user_agent"]),
        output_dir=Path(str(base["output_dir"])),
        pattern=str(base["pattern"]),
        watch=cast("list[str]", base["watch"]),
        blocked_file=Path(str(base["blocked_file"])),
        download_videos=bool(base["download_videos"]),
        covers=bool(base["covers"]),
        max_tracks=int(str(base["max_tracks"])),
        max_disk_mb=int(str(base["max_disk_mb"])),
        rotate=bool(base["rotate"]),
        min_free_mb=int(str(base["min_free_mb"])),
        sidecar=bool(base["sidecar"]),
    )
