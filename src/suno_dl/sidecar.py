"""Per-track JSON sidecar: the permanent clip id and the original title.

Archive filenames are lossy. Sanitising drops ``: / ?``, accents and nbsp,
``feat.`` becomes ``feat_`` (#70), and clips that share a title collide on
``_2``-style suffixes. The Suno clip id (the UUID in ``/song/<id>``) is the one
stable key, so every saved track gets ``<same stem>.json`` next to its audio
recording that id, the *original* title and a little public metadata.

A sidecar holds public metadata only — never cookies, tokens or signed URLs.
The same file is the source of truth for "do I already have this id?" when
re-running a sync, even if the filename the track would get today differs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from . import __version__
from .models import TrackInfo
from .urls import UUID_RE, song_page_url

logger = logging.getLogger(__name__)

SIDECAR_SCHEMA = 1
SIDECAR_SUFFIX = ".json"

# A sidecar is a small JSON record of public metadata. Refuse to parse one that
# is absurdly large (#157): the size is checked before the bytes are read, so a
# hostile or truncated file costs a stat(), not an allocation.
MAX_SIDECAR_BYTES = 256 * 1024

# Media files whose existence makes a sidecar's claim ("I saved this") true.
_MEDIA_EXTS = (".m4a", ".mp3", ".wav", ".mp4")
# A real download is always larger than this; matches the downloader's check.
_MIN_MEDIA_BYTES = 1024


def sidecar_path(base: Path) -> Path:
    """Sidecar for a media base (``<stem>``) or media file (``<stem>.m4a``).

    Only the *last* suffix is replaced when a known media extension is present,
    so a legacy stem with a mid-name period (``Song (feat. X).m4a``) keeps it.
    """
    if base.suffix.lower() in _MEDIA_EXTS:
        base = base.with_suffix("")
    return Path(str(base) + SIDECAR_SUFFIX)


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_sidecar(
    track: TrackInfo,
    *,
    handle: str,
    saved_files: list[str],
    downloaded_at: str | None = None,
    source: str = "download",
) -> dict[str, Any]:
    """Assemble the sidecar dict. Every value is public track metadata."""
    return {
        "schema": SIDECAR_SCHEMA,
        "id": track.id.lower(),
        "url": song_page_url(track.id.lower()),
        "title": track.title,
        "artist": None if track.artist_is_fallback else track.artist,
        "handle": handle or track.artist or None,
        "creator_handle": track.creator_handle,  # distinct from source handle
        "created_at": track.created_at,
        "model_name": track.model_name,
        "model_version": track.model_version,
        "duration_sec": track.duration_sec,
        "tags": track.tags,
        # Tracks come from public pages; an explicit False is respected.
        "is_public": True if track.is_public is None else track.is_public,
        "saved_files": sorted(saved_files),
        "suno_dl_version": __version__,
        "downloaded_at": downloaded_at or utc_now_iso(),
        "source": source,
    }


def write_sidecar(path: Path, data: dict[str, Any], *, overwrite: bool = True) -> bool:
    """Atomically write ``data`` to ``path``; return True if a file was written.

    Written as UTF-8 with real characters (``ensure_ascii=False``) so original
    titles stay human-readable. With ``overwrite=False`` an existing sidecar is
    left untouched.
    """
    if path.exists() and not overwrite:
        return False
    tmp: Path | None = None
    try:
        # Exclusive, unpredictable temporary creation prevents a pre-planted
        # .json.part symlink from redirecting the write outside the archive.
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".part",
            delete=False,
        ) as stream:
            tmp = Path(stream.name)
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # The exclusive temporary is 0600 by default; a sidecar is public
        # metadata that SMB/Music Assistant tooling may also need to read.
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o644)
        if overwrite:
            tmp.replace(path)
        else:
            os.link(tmp, path, follow_symlinks=False)
            tmp.unlink()
        tmp = None  # no cleanup after the committed atomic publication
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not write sidecar %s: %s", path.name, exc)
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return True


def _unique_json_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate fields in each object when interpreting identity evidence."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _finite_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _load_identity_json(raw: str) -> Any:
    """Parse identity-sensitive JSON without duplicate keys or non-finite numbers."""
    return json.loads(
        raw,
        object_pairs_hook=_unique_json_fields,
        parse_float=_finite_json_float,
        parse_constant=_reject_json_constant,
    )


def read_sidecar(path: Path, *, strict_identity: bool = False) -> dict[str, Any] | None:
    """Parse one sidecar; None if it is not a valid suno-dl sidecar.

    Identity-sensitive callers reject duplicate keys and non-finite numbers.
    """
    try:
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            logger.warning("Ignoring oversized sidecar %s", path.name)
            return None
        raw = path.read_text(encoding="utf-8")
        data = _load_identity_json(raw) if strict_identity else json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    clip_id = data.get("id")
    if not isinstance(clip_id, str) or not UUID_RE.fullmatch(clip_id.lower()):
        return None
    if not isinstance(data.get("saved_files"), list):
        return None
    return data


def saved_files_for(base: Path) -> list[str]:
    """Names of the files that exist for ``base``: media plus ``.cover.*``."""
    found: list[str] = []
    for ext in _MEDIA_EXTS:
        candidate = Path(str(base) + ext)
        if candidate.is_file():
            found.append(candidate.name)
    for cover in (".cover.jpg", ".cover.jpeg", ".cover.png"):
        candidate = Path(str(base) + cover)
        if candidate.is_file():
            found.append(candidate.name)
    return found


def known_ids(directory: Path) -> dict[str, Path]:
    """Map clip id -> sidecar for every sidecar in ``directory`` whose media exists.

    A sidecar only counts while at least one file it lists is still on disk, so
    a deleted track is fetched again rather than skipped forever.
    """
    found: dict[str, Path] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*" + SIDECAR_SUFFIX)):
        data = read_sidecar(path)
        if data is None:
            continue
        for name in data["saved_files"]:
            if not isinstance(name, str) or Path(name).suffix.lower() not in _MEDIA_EXTS:
                continue
            media = directory / Path(name).name  # never follow a path out of the dir
            if media.is_file() and media.stat().st_size > _MIN_MEDIA_BYTES:
                found.setdefault(str(data["id"]).lower(), path)
                break
    return found
