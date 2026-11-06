"""Read-only terminal-success gate for the exact verified-sync protocol.

Container inspection must come from the authoritative host. This gate performs
no process control, network requests, library writes or automatic migration.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .sidecar import _load_identity_json
from .urls import UUID_RE


class SyncNotReady(RuntimeError):
    """The selected sync has not established a safe successful terminal state."""


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise SyncNotReady("invalid sync count")
    return value


def verify_terminal_success(
    audit: Path,
    run_id: str,
    expected_container_id: str,
    inspection: dict,
) -> dict:
    if not isinstance(run_id, str) or not UUID_RE.fullmatch(run_id) or run_id != run_id.lower():
        raise SyncNotReady("invalid selected sync UUID")
    if not isinstance(expected_container_id, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_container_id
    ):
        raise SyncNotReady("invalid selected container ID")
    if not isinstance(inspection, dict) or not isinstance(inspection.get("State"), dict):
        raise SyncNotReady("invalid container inspection")
    state = inspection["State"]
    if (
        inspection.get("Id") != expected_container_id
        or state.get("Status") != "exited"
        or state.get("Running") is not False
        or type(state.get("ExitCode")) is not int
        or state["ExitCode"] != 0
    ):
        raise SyncNotReady("selected sync container has not exited successfully")
    # Bind the read to the checked inode; reject a replacement or concurrent append.
    try:
        info = audit.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32 * 1024 * 1024:
            raise SyncNotReady("unsafe sync audit file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(audit, flags)
        with os.fdopen(fd, "rb") as stream:

            def identity(value):
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                    value.st_nlink,
                    value.st_mode,
                )

            if identity(os.fstat(stream.fileno())) != identity(info):
                raise SyncNotReady("sync audit changed before reading")
            raw = stream.read(32 * 1024 * 1024 + 1)
            if (
                len(raw) != info.st_size
                or identity(os.fstat(stream.fileno())) != identity(info)
                or identity(audit.lstat()) != identity(info)
            ):
                raise SyncNotReady("sync audit changed while reading")
    except OSError as exc:
        raise SyncNotReady("cannot safely read sync audit") from exc
    if not raw or not raw.endswith(b"\n"):
        raise SyncNotReady("sync audit is empty or has a torn final record")
    try:
        records = [_load_identity_json(line) for line in raw.decode("utf-8").splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise SyncNotReady("invalid sync audit JSON") from exc
    if any(not isinstance(r, dict) for r in records):
        raise SyncNotReady("sync audit records must be objects")
    starts = [
        i for i, r in enumerate(records) if r.get("event") == "started" and r.get("id") == run_id
    ]
    if len(starts) != 1:
        raise SyncNotReady("exact selected sync start is absent or repeated")
    selected = records[starts[0] :]
    if any(r.get("event") == "started" for r in selected[1:]):
        raise SyncNotReady("a newer sync run is present")
    if selected[-1].get("event") != "finished":
        raise SyncNotReady("selected sync has no final completion record")
    if any(r.get("event") not in {"started", "downloaded", "finished"} for r in selected):
        raise SyncNotReady("selected sync recorded a failure or unknown state")
    if sum(r.get("event") == "finished" for r in selected) != 1:
        raise SyncNotReady("sync completion is ambiguous")
    first, last = selected[0], selected[-1]
    if (
        _count(last.get("failed")) != 0
        or _count(last.get("total")) != _count(first.get("total"))
        or _count(first.get("existing")) + _count(first.get("missing")) != first["total"]
    ):
        raise SyncNotReady("selected sync summary is not a complete success")
    downloads = [r for r in selected if r.get("event") == "downloaded"]
    ids = [r.get("id") for r in downloads]
    if (
        any(not isinstance(x, str) or not UUID_RE.fullmatch(x) or x != x.lower() for x in ids)
        or len(ids) != len(set(ids))
        or _count(last.get("downloaded")) != len(downloads)
        or len(downloads) != first["missing"]
    ):
        raise SyncNotReady("selected sync download accounting is inconsistent")
    return {
        "run_id": run_id,
        "container_id": expected_container_id,
        "start": first,
        "finish": last,
        "downloads": downloads,
    }
