#!/usr/bin/env python3
"""Opt-in Suno clip evidence collection and read-only producer migration plan.

This tool does not apply or deploy anything. Run only after all library writers
have stopped; inventory comparisons can miss concurrent changes and do not
replace that requirement. State is kept in an
operator-selected private directory outside the library. No producer is inferred
from a playlist owner, legacy tag, page title, or an untrusted redirect.

Identity comes from each canonical song page's document head; Flight and
lyrics are ignored. Collection requires an explicit stopped-writers assertion
and holds the common archive lock. Cached pages are revalidated on every resume.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # The collector is intentionally Linux-only.
    fcntl = None  # type: ignore[assignment]

import httpx

from suno_dl.discovery import _validated_identity
from suno_dl.library import LibraryIndex
from suno_dl.producer_lookup import (
    ProducerIdentity,
    ProducerLookupError,
    producer_identity_from_page,
)
from suno_dl.producer_migration import _rename_no_replace, _root, _walk, plan_migration
from suno_dl.sidecar import _load_identity_json
from suno_dl.urls import UUID_RE

_MAX_HTML = 8 * 1024 * 1024
_MAX_STATE = 16 * 1024 * 1024


class EvidenceError(RuntimeError):
    """Evidence or checkpoint did not satisfy a safety precondition."""


class RateLimited(EvidenceError):
    """Stop collection when the service indicates backpressure."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory(root: Path, *, validate_layout: bool = True) -> tuple[list[str], str]:
    """Validate UUID ownership and return a fingerprint of the entire inventory."""
    root = _root(root)
    index = LibraryIndex(root).scan()
    if index.conflicts:
        raise EvidenceError("Conflicting UUID claims")
    ids = sorted(index.ids())
    if validate_layout:
        plan_migration(root, dict.fromkeys(ids))  # validates ownership and the complete layout
    entries = []
    for path in sorted(_walk(root)):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise EvidenceError(f"Unsafe inventory member: {path}")
        item = [
            path.relative_to(root).as_posix(),
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ]
        if path.suffix.lower() == ".json":
            item.append(_digest(path.read_bytes()))
        entries.append(item)
    info = root.stat()
    raw = json.dumps(
        [info.st_dev, info.st_ino, ids, entries], ensure_ascii=False, separators=(",", ":")
    )
    return ids, _digest(raw.encode())


def _require_trusted_framing() -> None:
    # Song identity is now extracted from document head metadata.
    return None


def identity_from_page(clip_id: str, html: str) -> ProducerIdentity:

    try:
        return producer_identity_from_page(clip_id, html)
    except ProducerLookupError as exc:
        raise EvidenceError(str(exc)) from exc


def producer_from_page(clip_id: str, html: str) -> str:
    return identity_from_page(clip_id, html).artist


def fetch_page(client: httpx.Client, clip_id: str) -> str:
    """Fetch only the exact HTTPS song URL; never follow a redirect."""
    if not UUID_RE.fullmatch(clip_id):
        raise EvidenceError("Invalid clip UUID")
    url = f"https://suno.com/song/{clip_id}"
    with client.stream("GET", url, follow_redirects=False) as response:
        if response.status_code == 429 or response.status_code >= 500:
            raise RateLimited(f"Collection stopped (HTTP {response.status_code})")
        if str(response.url) != url or response.status_code != 200:
            raise EvidenceError(f"Song page unavailable (HTTP {response.status_code})")
        if response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/html":
            raise EvidenceError("Song response is not HTML")
        blocks = []
        size = 0
        for block in response.iter_bytes():
            size += len(block)
            if size > _MAX_HTML:
                raise EvidenceError("Song page is too large")
            blocks.append(block)
    try:
        return b"".join(blocks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("Song page is not UTF-8") from exc


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_state_dir(root: Path, state_dir: Path, create: bool) -> Path:
    if not sys.platform.startswith("linux") or fcntl is None:
        raise EvidenceError("Producer evidence collection requires Linux")
    root = _root(root)
    state_dir = Path(state_dir).absolute()
    if state_dir.is_symlink():
        raise EvidenceError("State directory cannot be a symlink")
    resolved = state_dir.resolve(strict=False)
    if resolved == root or root in resolved.parents:
        raise EvidenceError("Evidence state must be outside the music library")
    if not state_dir.exists():
        if not create:
            raise EvidenceError("Evidence state does not exist; run with --fetch to initialize")
        state_dir.mkdir(mode=0o700)
        _sync_dir(state_dir.parent)
    state_dir = state_dir.resolve(strict=True)
    if state_dir == root or root in state_dir.parents:
        raise EvidenceError("Evidence state must be outside the music library")
    st = state_dir.stat()
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.geteuid()
        or stat.S_IMODE(st.st_mode) != 0o700
    ):
        raise EvidenceError("Evidence directory must be owned by you and private (mode 700)")
    return state_dir


@contextlib.contextmanager
def _state_lock(state_dir: Path):
    if fcntl is None:
        raise EvidenceError("Producer evidence collection requires Linux")
    lock_path = state_dir / ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            raise EvidenceError("Unsafe evidence lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvidenceError("Another evidence collector is running") from exc
        yield
    finally:
        os.close(fd)


def _save(path: Path, state: dict) -> None:
    payload = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
    if len(payload) > _MAX_STATE:
        raise EvidenceError("Evidence checkpoint is too large")
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if path.exists() or path.is_symlink():
            _load(path)  # refuse unowned/malformed checkpoint files
            os.replace(temp, path)
        else:
            _rename_no_replace(temp, path)  # atomic no-overwrite creation
        _sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _load(path: Path) -> dict:
    st = path.lstat()
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_nlink != 1
        or st.st_uid != os.geteuid()
        or st.st_size > _MAX_STATE
    ):
        raise EvidenceError("Unsafe evidence checkpoint")
    try:
        data = _load_identity_json(path.read_text(encoding="utf-8"))
        if (
            type(data["version"]) is not int
            or data["version"] != 2
            or not isinstance(data["verified"], dict)
        ):
            raise ValueError("invalid checkpoint")
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EvidenceError("Invalid evidence checkpoint") from exc


def _page_path(state_dir: Path, clip_id: str, page_hash: str) -> Path:
    if (
        not UUID_RE.fullmatch(clip_id)
        or len(page_hash) != 64
        or any(c not in "0123456789abcdef" for c in page_hash)
    ):
        raise EvidenceError("Invalid stored page identity")
    return state_dir / f"{clip_id}-{page_hash}.html"


def _read_page(path: Path, expected_hash: str) -> str:
    try:
        st = path.lstat()
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_nlink != 1
            or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_size > _MAX_HTML
        ):
            raise EvidenceError("Unsafe stored evidence page")
        payload = path.read_bytes()
        if _digest(payload) != expected_hash:
            raise EvidenceError("Stored evidence page changed")
        return payload.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise EvidenceError("Stored evidence page unavailable") from exc


def _store_page(state_dir: Path, clip_id: str, html: str) -> str:
    payload = html.encode("utf-8")
    page_hash = _digest(payload)
    path = _page_path(state_dir, clip_id, page_hash)
    if path.exists() or path.is_symlink():
        _read_page(path, page_hash)
        return page_hash
    fd, name = tempfile.mkstemp(prefix=".page-", dir=state_dir)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        _rename_no_replace(temp, path)
        _sync_dir(state_dir)
    finally:
        temp.unlink(missing_ok=True)
    return page_hash


def _run_collector(
    root: Path,
    state_dir: Path,
    *,
    fetch: bool = False,
    client: httpx.Client | None = None,
    delay: float = 1.25,
) -> dict:
    """Collect explicitly when requested; always return a read-only dry plan."""
    _require_trusted_framing()
    root = _root(root)
    state_dir = _safe_state_dir(root, state_dir, create=fetch)
    with _state_lock(state_dir):
        ids, fingerprint = inventory(root)
        path = state_dir / "evidence.json"
        if path.is_symlink():
            raise EvidenceError("Unsafe evidence checkpoint")
        if path.exists():
            state = _load(path)
            if state.get("inventory") != fingerprint or state.get("ids") != ids:
                raise EvidenceError("Library inventory changed; evidence session cannot continue")
        else:
            if not fetch:
                raise EvidenceError("Evidence checkpoint does not exist")
            state = {"version": 2, "inventory": fingerprint, "ids": ids, "verified": {}}
            _save(path, state)
        verified = state["verified"]
        if set(verified) - set(ids):
            raise EvidenceError("Checkpoint contains clips outside the inventory")
        for clip_id, record in verified.items():
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("artist"), str)
                or _validated_identity({"display_name": record["artist"]}, ("display_name",))
                != record["artist"]
                or record.get("url") != f"https://suno.com/song/{clip_id}"
                or not isinstance(record.get("page_sha256"), str)
                or len(record["page_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in record["page_sha256"])
                or not isinstance(record.get("fetched_at"), str)
            ):
                raise EvidenceError("Invalid verified producer record")
            saved_page = _read_page(
                _page_path(state_dir, clip_id, record["page_sha256"]), record["page_sha256"]
            )
            identity = identity_from_page(clip_id, saved_page)
            if identity.artist != record["artist"] or identity.handle != record.get(
                "creator_handle"
            ):
                raise EvidenceError("Stored page disagrees with verified producer identity")
        errors = {}
        if fetch:
            if client is None:
                raise EvidenceError("An explicitly configured HTTPS client is required")
            for clip_id in ids:
                if clip_id in verified:
                    continue
                try:
                    html = fetch_page(client, clip_id)
                    identity = identity_from_page(clip_id, html)
                except RateLimited:
                    raise
                except EvidenceError as exc:
                    errors[clip_id] = str(exc)
                    continue
                finally:
                    if delay:
                        time.sleep(delay)
                if inventory(root, validate_layout=False) != (ids, fingerprint):
                    raise EvidenceError("Library inventory changed during evidence collection")
                page_hash = _store_page(state_dir, clip_id, html)
                verified[clip_id] = {
                    "artist": identity.artist,
                    "creator_handle": identity.handle,
                    "url": f"https://suno.com/song/{clip_id}",
                    "page_sha256": page_hash,
                    "fetched_at": datetime.now(UTC).isoformat(),
                }
                _save(path, state)
        if inventory(root, validate_layout=False) != (ids, fingerprint):
            raise EvidenceError("Library inventory changed before planning")
        unverified = [cid for cid in ids if cid not in verified]
        groups = []
        if not unverified:
            mapping = {clip_id: verified[clip_id]["artist"] for clip_id in ids}
            handles = {clip_id: verified[clip_id]["creator_handle"] for clip_id in ids}
            groups = plan_migration(root, mapping, creator_handle_by_id=handles)["groups"]
        return {
            "verified": len(verified),
            "unverified_ids": unverified,
            "plan_available": not unverified,
            "errors": errors,
            "groups": groups,
            "inventory": fingerprint,
        }


def run(
    root: Path,
    state_dir: Path,
    *,
    fetch: bool = False,
    client: httpx.Client | None = None,
    delay: float = 1.25,
    writers_stopped: bool = False,
) -> dict:
    if not fetch:
        return _run_collector(root, state_dir, fetch=False)
    if not writers_stopped:
        raise EvidenceError("All library writers must be stopped before producer collection")
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise EvidenceError("Producer collection requires a dedicated synchronous process")
    from .archive_lock import _exclusive_archive_sync
    from .producer_migration import raise_if_migration_pending

    root = _root(root)
    with _exclusive_archive_sync(root):
        raise_if_migration_pending(root)
        return _run_collector(root, state_dir, fetch=True, client=client, delay=delay)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument(
        "--fetch", action="store_true", help="explicitly collect public song-page evidence"
    )
    parser.add_argument(
        "--writers-stopped",
        action="store_true",
        help="assert every other library writer has been stopped",
    )
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.exit(2, "Producer evidence collection requires Linux\n")
    try:
        if args.fetch:
            with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as client:
                result = run(
                    args.library,
                    args.state_dir,
                    fetch=True,
                    client=client,
                    writers_stopped=args.writers_stopped,
                )
        else:
            result = run(args.library, args.state_dir)
    except (EvidenceError, OSError, httpx.HTTPError) as exc:
        parser.exit(2, f"Evidence plan failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["unverified_ids"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
