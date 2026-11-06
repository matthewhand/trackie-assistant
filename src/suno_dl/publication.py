"""Linux no-overwrite publication and recovery of one prepared clip group.

The caller holds the common archive lock and excludes other library writers.
Only inodes named in a durable UUID intent may be published or cleaned up.
Recovery accepts a missing stage only when its exact recorded inode and bytes
are already durable at the destination. The intent is removed last.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from .sidecar import MAX_SIDECAR_BYTES, _load_identity_json
from .urls import UUID_RE


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def relative_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid publication path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or value.startswith("/"):
        raise ValueError("unsafe publication path")
    path = root
    for part in parts:
        path /= part
        if path.is_symlink():
            raise ValueError("symlink publication path")
    if not path.resolve().is_relative_to(root):
        raise ValueError("publication path escapes archive")
    return path


def file_hash(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("nonregular publication file")
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.fstat(fd)
        named = path.lstat()

        def identity(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_nlink,
                info.st_mode,
            )

        if identity(before) != identity(after) or identity(before) != identity(named):
            raise ValueError("publication file changed while reading")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _read_json(path: Path, limit: int) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("unsafe or oversized publication JSON")
    raw = path.read_bytes()
    if len(raw) != info.st_size:
        raise ValueError("publication JSON changed")
    data = _load_identity_json(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("publication JSON must be an object")
    return data


def publish_prepared(root: Path, marker: Path) -> str:
    """Recover either the deployed v1 pair or a v2 audio/sidecar/adjunct group."""
    root = Path(root)
    if root.is_symlink() or root.resolve(strict=True) != root:
        raise ValueError("publication root must be canonical")
    marker = relative_path(root, marker.relative_to(root).as_posix())
    if marker.parent != root / ".library" / "verified-sync-intents":
        raise ValueError("publication intent is outside the owned directory")
    marker_info = marker.lstat()
    if marker_info.st_nlink != 1:
        raise ValueError("linked publication intent")
    data = _read_json(marker, 65536)
    version, clip_id, profile = data.get("schema"), data.get("id"), data.get("profile")
    if (
        type(version) is not int
        or version not in {1, 2}
        or not isinstance(clip_id, str)
        or not UUID_RE.fullmatch(clip_id)
        or clip_id != clip_id.lower()
        or marker.stem != clip_id
        or not isinstance(profile, str)
        or not (re.fullmatch(r"[A-Za-z0-9_-]{1,255}", profile) or (version == 2 and profile == ""))
    ):
        raise ValueError("invalid publication intent identity")
    output = root / profile if profile else root
    if output.is_symlink() or not output.is_dir():
        raise ValueError("unsafe publication destination directory")
    members = data.get("members")
    if not isinstance(members, list) or not (2 <= len(members) <= 4):
        raise ValueError("invalid publication members")
    if version == 1 and len(members) != 2:
        raise ValueError("invalid v1 publication members")

    checked = []
    destination_names = []
    for item in members:
        if not isinstance(item, dict) or set(item) != {"stage", "destination", "sha256", "inode"}:
            raise ValueError("invalid publication member record")
        source = relative_path(root, item["stage"])
        destination = relative_path(root, item["destination"])
        if (
            not source.is_relative_to(root / ".suno-staging")
            or destination.parent != output
            or source == destination
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
            or not isinstance(item["inode"], list)
            or len(item["inode"]) != 2
            or any(type(n) is not int or n < 0 for n in item["inode"])
        ):
            raise ValueError("publication path ownership mismatch")
        source_info = source.lstat() if source.exists() else None
        dest_info = destination.lstat() if destination.exists() else None
        if source_info is None and dest_info is None:
            raise ValueError("prepared file and published destination are both missing")
        expected_links = 2 if source_info is not None and dest_info is not None else 1
        for path, info in ((source, source_info), (destination, dest_info)):
            if info is None:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != expected_links
                or (info.st_dev, info.st_ino) != tuple(item["inode"])
                or info.st_dev != root.stat().st_dev
                or file_hash(path) != item["sha256"]
            ):
                raise ValueError(
                    "publication destination belongs to another file or staged content changed"
                )
        checked.append((source, destination, source_info, dest_info))
        destination_names.append(destination.name)
    if len({name.casefold() for name in destination_names}) != len(destination_names):
        raise ValueError("duplicate publication destination")
    if len({source for source, _, _, _ in checked}) != len(checked):
        raise ValueError("duplicate publication source")
    if len({source.parent for source, _, _, _ in checked}) != 1:
        raise ValueError("publication stages do not form one owned group")
    media = [d for _, d, _, _ in checked if d.suffix == ".m4a"]
    sidecars = [d for _, d, _, _ in checked if d.suffix == ".json"]
    if len(media) != 1 or len(sidecars) != 1 or media[0].stem != sidecars[0].stem:
        raise ValueError("publication file pair mismatch")
    audio, sidecar = media[0], sidecars[0]
    for _, destination, _, _ in checked:
        if destination not in (audio, sidecar) and (
            version != 2
            or destination.name
            not in {
                audio.stem + ".cover.jpg",
                audio.stem + ".cover.jpeg",
                audio.stem + ".cover.png",
                audio.stem + ".cover.webp",
                audio.stem + ".txt",
                audio.stem + ".lrc",
            }
        ):
            raise ValueError("unsupported publication adjunct")
    side_entry = next(item for item in checked if item[1] == sidecar)
    side = side_entry[0] if side_entry[2] is not None else sidecar
    side_data = _read_json(side, MAX_SIDECAR_BYTES)
    expected_saved = sorted(d.name for _, d, _, _ in checked if d != sidecar)
    if side_data.get("id") != clip_id or side_data.get("saved_files") != expected_saved:
        raise ValueError("publication sidecar ownership mismatch")

    # Validate the entire group before changing any destination.
    for source, destination, _source_info, dest_info in checked:
        if dest_info is None:
            os.link(source, destination, follow_symlinks=False)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
    sync_directory(output)
    # Keep the durable intent through private cleanup. A fresh process can
    # recover missing stages from the exact recorded destination inode/hash.
    stage_dir = checked[0][0].parent
    for source, _, source_info, _ in checked:
        if source_info is not None:
            source.unlink()
    if stage_dir.exists():
        sync_directory(stage_dir)
        if version == 2:
            # Refuse unexplained artifacts rather than deleting them.
            stage_dir.rmdir()
            sync_directory(stage_dir.parent)
        # Deployed v1 callers retain their original incoming file and remove
        # that private directory themselves after this function returns.
    current = marker.lstat()
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
        marker_info.st_dev,
        marker_info.st_ino,
        marker_info.st_size,
        marker_info.st_mtime_ns,
    ):
        raise ValueError("publication intent changed before completion")
    marker.unlink()
    sync_directory(marker.parent)
    return clip_id
