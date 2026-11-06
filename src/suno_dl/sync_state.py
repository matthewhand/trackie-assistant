"""Small status snapshot for readers; never an authority for download recovery."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

PHASES = frozenset(
    {
        "starting",
        "reading_playlists",
        "checking",
        "waiting",
        "downloading",
        "complete",
        "partial",
        "stopped",
        "offline",
    }
)


def publish_status(root: Path, values: dict) -> Path:
    phase = values.get("phase")
    if phase not in PHASES:
        raise ValueError("Unknown sync phase")
    directory = root / ".library"
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Invalid sync status directory")
    target = directory / "sync-status.json"
    if target.is_symlink():
        raise ValueError("Linked sync status")
    data = dict(values, schema=1, updated_at=datetime.now(UTC).isoformat())
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("Sync status is too large")
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".sync-status-", delete=False) as f:
            name = Path(f.name)
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o644)
        os.replace(name, target)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)
    return target
