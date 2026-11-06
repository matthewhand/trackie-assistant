"""Global clip-id library: one recording per UUID, across every subfolder.

Why this exists (#158)
----------------------
`sidecar.known_ids()` looks at a *single* directory, so a clip that is already
archived under ``drinko/`` is downloaded again into ``playlist-xyz/`` when the
same recording shows up in a public playlist. The Suno clip UUID is the only
stable key, so the whole archive is indexed by it instead:

* discovery is read-only and never renames, moves or deletes legacy files;
* a clip is fetched only when no valid recording exists **anywhere** in the
  archive, so two playlists referencing one recording cost one transfer and
  one physical file;
* playlist membership is stored separately (:func:`record_membership`) as
  ordered UUID references, never as a second copy of the audio.

Fail-closed rules
-----------------
A sidecar only vouches for a recording when it validates, sits inside the
archive root, is not a symlink, and lists a media file that exists beside it,
is not a symlink and is larger than the downloader's minimum. When two
sidecars claim one UUID for *different* physical files the id is reported as
conflicted and resolves to ``None`` rather than silently picking a winner: a
wrong pick would either overwrite or orphan someone's archive.

Everything here is standard library only and never raises for unreadable
entries; a broken sidecar is simply not part of the library.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .sidecar import SIDECAR_SUFFIX, read_sidecar

logger = logging.getLogger(__name__)

# Media files whose existence makes a sidecar's claim true (same set the
# downloader and `sidecar.known_ids` use).
MEDIA_EXTS = frozenset({".m4a", ".mp3", ".wav", ".mp4"})
MIN_MEDIA_BYTES = 1024

# Directories that never hold published recordings: staging areas, VCS data
# and Python caches. Skipping them keeps a scan cheap and keeps a half-written
# download from ever counting as "already archived".
_SKIP_DIR_NAMES = frozenset({".suno-staging", ".git", "__pycache__", ".venv", "venv"})

MEMBERSHIP_SCHEMA = 1
MEMBERSHIP_DIR = ".library"
MEMBERSHIP_FILE = "membership.json"


@dataclass(frozen=True)
class LibraryEntry:
    """One validated recording, addressed by its clip UUID."""

    clip_id: str
    sidecar: Path
    media: tuple[Path, ...]
    media_identities: tuple[tuple[Path, tuple[int, int, int, int]], ...] = ()
    member_identities: tuple[tuple[Path, tuple[int, int, int, int]], ...] = ()

    @property
    def directory(self) -> Path:
        return self.sidecar.parent

    @property
    def base(self) -> Path:
        """Stem the sidecar and media share (``<stem>.json`` -> ``<stem>``)."""
        return self.sidecar.with_suffix("")


def _valid_media(sidecar: Path, names: Iterable[str], root: Path) -> tuple[Path, ...]:
    """Media listed by ``sidecar`` that really exists beside it, contained in ``root``."""
    found: list[Path] = []
    for name in names:
        if not isinstance(name, str):
            continue
        # Only a bare filename is honoured: a sidecar can never point at a
        # path outside its own directory (`../../etc/passwd`).
        if not name or Path(name).name != name:
            continue
        path = sidecar.parent / name
        if path.suffix.lower() not in MEDIA_EXTS or path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_size <= MIN_MEDIA_BYTES:
                continue
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        found.append(path)
    return tuple(found)


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


class LibraryIndex:
    """Read-only index of every validated recording under an archive root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._entries: dict[str, LibraryEntry] = {}
        self._claims: dict[str, list[LibraryEntry]] = {}
        self.conflicts: set[str] = set()
        self.scanned = False

    def scan(self) -> LibraryIndex:
        """Walk the archive once; safe to call repeatedly (it re-reads)."""
        self._entries.clear()
        self._claims.clear()
        self.conflicts.clear()
        root = self.root
        try:
            root = root.resolve()
        except OSError:
            self.scanned = True
            return self
        if not root.is_dir():
            self.scanned = True
            return self
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            directory = Path(dirpath)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in _SKIP_DIR_NAMES and not (directory / name).is_symlink()
            )
            for filename in sorted(filenames):
                if not filename.endswith(SIDECAR_SUFFIX):
                    continue
                sidecar = directory / filename
                if sidecar.is_symlink():
                    continue
                record = read_sidecar(sidecar)
                if record is None:
                    continue
                media = _valid_media(sidecar, record["saved_files"], root)
                if not media:
                    continue
                identities = []
                try:
                    for path in media:
                        info = path.stat()
                        identities.append(
                            (path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
                        )
                except OSError:
                    continue
                members = []
                for name in record["saved_files"]:
                    if not isinstance(name, str) or Path(name).name != name:
                        continue
                    path = sidecar.parent / name
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        info = path.stat()
                        members.append(
                            (path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
                        )
                    except OSError:
                        continue
                entry = LibraryEntry(
                    str(record["id"]).lower(), sidecar, media, tuple(identities), tuple(members)
                )
                self._claims.setdefault(entry.clip_id, []).append(entry)
        for clip_id, claims in self._claims.items():
            if self._same_recording(claims):
                self._entries[clip_id] = claims[0]
            else:
                self.conflicts.add(clip_id)
                logger.warning(
                    "Conflicting sidecar claims for clip %s; refusing to reuse either recording",
                    clip_id,
                )
        self.scanned = True
        return self

    @staticmethod
    def _same_recording(claims: list[LibraryEntry]) -> bool:
        if len(claims) == 1:
            return True
        identities = {_identity(path) for claim in claims for path in claim.media}
        identities.discard(None)
        # Same physical bytes claimed by several sidecars is one recording,
        # not a conflict (e.g. a hardlinked legacy copy).
        return len(identities) <= 1

    def ensure_scanned(self) -> LibraryIndex:
        if not self.scanned:
            self.scan()
        return self

    def get(self, clip_id: str) -> LibraryEntry | None:
        """The validated recording for ``clip_id``, or None (fail closed)."""
        self.ensure_scanned()
        return self._entries.get(clip_id.lower())

    def __contains__(self, clip_id: object) -> bool:
        return isinstance(clip_id, str) and self.get(clip_id) is not None

    def ids(self) -> set[str]:
        self.ensure_scanned()
        return set(self._entries)

    def entries(self) -> tuple[LibraryEntry, ...]:
        self.ensure_scanned()
        return tuple(self._entries.values())


def probe_media(path: Path) -> str:
    """Read-only integrity probe for an existing recording.

    Returns ``""`` when the file looks like a real media container, otherwise a
    short reason. Never modifies, truncates or locks the file, and never opens
    it for writing — this is what ``--force-redownload`` reports before it
    replaces anything.
    """
    try:
        if not path.is_file() or path.is_symlink():
            return "missing"
        size = path.stat().st_size
        if size <= MIN_MEDIA_BYTES:
            return f"too small ({size} bytes)"
        with path.open("rb") as handle:
            head = handle.read(12)
    except OSError as exc:
        return f"unreadable ({exc.__class__.__name__})"
    if len(head) < 12:
        return "truncated header"
    if head[4:8] == b"ftyp":
        return ""
    if head[:3] == b"ID3":
        return ""
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return ""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ""
    return "unrecognised container"


# ─────────────────────────── playlist membership ────────────────────────────


def membership_path(root: Path) -> Path:
    return Path(root) / MEMBERSHIP_DIR / MEMBERSHIP_FILE


def _read_membership(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": MEMBERSHIP_SCHEMA, "sources": {}}
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return {"schema": MEMBERSHIP_SCHEMA, "sources": {}}
    return data


def load_membership(root: Path) -> dict[str, list[str]]:
    """Map source name -> ordered clip ids, as last recorded.

    Membership is derived data: a missing or corrupt file simply means
    "nothing recorded yet", and is never treated as "no tracks".
    """
    data = _read_membership(membership_path(root))
    out: dict[str, list[str]] = {}
    for source, value in data["sources"].items():
        if not isinstance(source, str) or not isinstance(value, dict):
            continue
        ids = value.get("ids")
        if isinstance(ids, list):
            out[source] = [str(clip_id) for clip_id in ids if isinstance(clip_id, str)]
    return out


def record_membership(root: Path, source: str, clip_ids: Iterable[str]) -> Path | None:
    """Persist the ordered membership of ``source``; audio is never touched.

    Re-running with the same ids is a no-op (the file is not rewritten), so a
    repeated sync cannot churn the archive. The file is replaced atomically:
    a crash leaves either the previous membership or the new one, never a
    truncated JSON document.
    """
    if not source:
        return None
    path = membership_path(root)
    data = _read_membership(path)
    sources = data.setdefault("sources", {})
    ordered = [str(clip_id).lower() for clip_id in clip_ids]
    current = sources.get(source)
    if (
        isinstance(current, dict)
        and current.get("ids") == ordered
        and isinstance(current.get("updated_at"), str)
    ):
        return path
    sources[source] = {
        "ids": ordered,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data["schema"] = MEMBERSHIP_SCHEMA
    tmp = path.with_name(path.name + ".part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(path)
    except OSError as exc:
        # The previous membership file is left exactly as it was.
        logger.warning("Could not record playlist membership (%s): %s", source, exc)
        tmp.unlink(missing_ok=True)
        return None
    return path
