"""Test-only Linux SQLite identity-ledger model, NOT a Windows fix.

The caller MUST hold an exclusive archive lock across initialization, every
connection and every operation, and stop older/noncooperating writers. The
boolean precondition is an assertion by the caller, not proof of a lock. This
module is deliberately not connected to the downloader or publication path.

This model must not be shipped or integrated until Windows storage, locking,
and production compatibility are independently validated. Only explicitly
initialized simple archives on an allowlisted Linux local
filesystem are accepted, and the caller must explicitly assert the backing
storage is local. A filesystem type alone cannot prove that a block device is
not remotely backed. Windows, remote/network mounts, reparse points,
symlinks, hardlinks, lyrics files, legacy metadata and staging, migration,
force replacement, publication and automatic recovery are unsupported. An
import hashes and preserves pre-existing files; it never moves or adopts an
ambiguous file. Sidecar-listed files are claims, not independent ownership
proof: an explicit caller assertion of ownership review is mandatory. Removing
both database and marker defeats missing-ledger
history detection. Replacing the database with a valid stale copy can also
lose a committed reservation when no output survives; a separate durable
witness would be needed to detect that. A missing SQLite journal alone cannot
be identified as either clean absence or loss of a hot rollback journal.
SQLite PERSIST/EXTRA settings alone do not prove Windows crash durability. A
fsynced DIRTY/CLEAN witness refuses unresolved transactions before opening
SQLite under an honest-fsync, cooperating-writer assumption. Coordinated
rollback of both witness and database remains indistinguishable from an earlier
valid state without an independent monotonic witness; uncooperative writers
and dishonest storage are unsupported.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote

_DB = ".trackie-identity.sqlite3"
_MARKER = ".trackie-identity.marker"
_WITNESS = ".trackie-identity.witness"
_MAX_WITNESS = 16 * 1024 * 1024
_LOCK = ".suno-sync.lock"
_JOURNAL = _DB + "-journal"
_MEDIA = {".m4a", ".mp3", ".mp4", ".wav"}
_COVERS = {".cover.jpg", ".cover.jpeg", ".cover.png"}
_ALLOWED_FS = {"ext4", "xfs", "btrfs", "tmpfs"}
_APP_ID = 0x54494431
_METADATA_SQL = "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID"
_IDENTITIES_SQL = """CREATE TABLE identities (
    clip_id TEXT PRIMARY KEY,
    audio_path TEXT NOT NULL,
    sidecar_path TEXT NOT NULL,
    audio_key TEXT NOT NULL UNIQUE,
    sidecar_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('reserved', 'complete')),
    audio_sha256 TEXT,
    sidecar_sha256 TEXT,
    CHECK((state = 'reserved' AND audio_sha256 IS NULL AND sidecar_sha256 IS NULL)
       OR (state = 'complete' AND audio_sha256 IS NOT NULL AND sidecar_sha256 IS NOT NULL))
) WITHOUT ROWID"""


_MEMBERS_SQL = """CREATE TABLE members (
    clip_id TEXT NOT NULL,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('media', 'cover')),
    sha256 TEXT NOT NULL,
    PRIMARY KEY(clip_id, path_key),
    FOREIGN KEY(clip_id) REFERENCES identities(clip_id)
) WITHOUT ROWID"""


class IdentityLedgerError(RuntimeError):
    """Unsafe, ambiguous, incomplete or unsupported archive/ledger state."""


def _require_lock(archive_lock_held: bool) -> None:
    if archive_lock_held is not True:
        raise IdentityLedgerError("The caller must hold the exclusive archive lock")


def _require_local_attestation(storage_verified_local: bool) -> None:
    if storage_verified_local is not True:
        raise IdentityLedgerError("The caller must verify that backing storage is local")


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _unescape_mount(value: str) -> str:
    for encoded, char in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
        value = value.replace(encoded, char)
    return value


def _filesystem_type(path: Path) -> str:
    if not sys.platform.startswith("linux"):
        raise IdentityLedgerError(
            "Candidate runs only on Linux; Windows filesystem safety is unvalidated"
        )
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        matches = []
        for line in lines:
            left, right = line.split(" - ", 1)
            mountpoint = Path(_unescape_mount(left.split()[4]))
            if path == mountpoint or mountpoint in path.parents:
                matches.append((len(mountpoint.parts), right.split()[0]))
        if not matches:
            raise ValueError("mount not found")
        longest = max(length for length, _fs in matches)
        types = {fs for length, fs in matches if length == longest}
        if len(types) != 1:
            raise ValueError("ambiguous overlapping mounts")
        return next(iter(types))
    except (OSError, IndexError, ValueError) as exc:
        raise IdentityLedgerError("Cannot verify local filesystem type") from exc


def _local(path: Path) -> None:
    if _filesystem_type(path) not in _ALLOWED_FS:
        raise IdentityLedgerError("Unsupported or network filesystem")


def _root(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise IdentityLedgerError("Archive root does not exist") from exc
        if _is_reparse(info) or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise IdentityLedgerError("Unsafe archive root or parent")
    _local(path)
    return path


def _regular(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise IdentityLedgerError(f"Missing or unreadable file: {path}") from exc
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise IdentityLedgerError(f"Symlink, reparse point, hardlink or non-file: {path}")
    return info


def _internal_file(root: Path, path: Path) -> os.stat_result:
    info = _regular(path)
    if info.st_dev != root.stat().st_dev:
        raise IdentityLedgerError("Cross-filesystem ledger state is unsupported")
    _local(path)
    return info


def _path(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise IdentityLedgerError("Invalid archive path")
    p = PurePosixPath(value)
    if p.is_absolute() or p.as_posix() != value or any(part in (".", "..") for part in p.parts):
        raise IdentityLedgerError("Archive path escapes or is not normalized")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    for part in p.parts:
        if (
            not part
            or part.endswith((" ", "."))
            or part.split(".")[0].upper() in reserved
            or any(
                c in ':*?"<>|'
                or ord(c) < 32
                or 127 <= ord(c) <= 159
                or 0xD800 <= ord(c) <= 0xDFFF
                or 0x202A <= ord(c) <= 0x202E
                or 0x2066 <= ord(c) <= 0x2069
                for c in part
            )
        ):
            raise IdentityLedgerError("Unsafe cross-platform archive name")
    result = root.joinpath(*p.parts)
    current = root
    for part in p.parts[:-1]:
        current /= part
        info = current.lstat()
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise IdentityLedgerError("Unsafe archive subdirectory")
        if info.st_dev != root.stat().st_dev:
            raise IdentityLedgerError("Cross-filesystem archive is unsupported")
        _local(current)
    return result


def _uuid(value: str) -> str:
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError("noncanonical")
    except (ValueError, AttributeError, TypeError) as exc:
        raise IdentityLedgerError("Invalid canonical track UUID") from exc
    return value


def _hash(path: Path) -> str:
    before = _regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    after = _regular(path)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise IdentityLedgerError(f"File changed during hashing: {path}")
    return digest.hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise IdentityLedgerError("Duplicate sidecar JSON key")
        result[key] = value
    return result


def _names(root: Path, clip_id: str, audio: str, sidecar: str) -> tuple[Path, Path]:
    _uuid(clip_id)
    a, s = _path(root, audio), _path(root, sidecar)
    if (
        a.suffix.lower() not in _MEDIA
        or a.parent != s.parent
        or s.name != a.name.rsplit(".", 1)[0] + ".json"
    ):
        raise IdentityLedgerError("Unsupported audio/sidecar names")
    return a, s


def _sidecar(path: Path, clip_id: str, expected_names: set[str]) -> None:
    info = _regular(path)
    if info.st_size > 256 * 1024:
        raise IdentityLedgerError("Oversized sidecar")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (OSError, ValueError, UnicodeError) as exc:
        raise IdentityLedgerError("Invalid sidecar") from exc
    saved = data.get("saved_files") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("id") != clip_id
        or not isinstance(saved, list)
        or not all(isinstance(n, str) for n in saved)
        or len(saved) != len(expected_names)
        or set(saved) != expected_names
    ):
        raise IdentityLedgerError("Sidecar does not prove exact member identity")


def _walk(root: Path, *, initialized: bool) -> list[Path]:
    paths = []
    root_dev = root.stat().st_dev

    def error(exc: OSError) -> None:
        raise IdentityLedgerError("Cannot inventory entire archive") from exc

    for name, dirs, files in os.walk(root, followlinks=False, onerror=error):
        directory = Path(name)
        _local(directory)
        for item in dirs + files:
            path = directory / item
            info = path.lstat()
            if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
                raise IdentityLedgerError(f"Symlink or reparse point: {path}")
            if info.st_dev != root_dev:
                raise IdentityLedgerError("Cross-filesystem archive is unsupported")
            if item in dirs and (not stat.S_ISDIR(info.st_mode) or item.startswith(".")):
                raise IdentityLedgerError(f"Unsupported staging or metadata directory: {path}")
        for item in files:
            path = directory / item
            _regular(path)
            if directory == root and item == _LOCK:
                continue
            if initialized and directory == root and item in {_DB, _MARKER, _JOURNAL, _WITNESS}:
                continue
            if directory == root and item in {
                _DB,
                _MARKER,
                _JOURNAL,
                _WITNESS,
                _DB + "-wal",
                _DB + "-shm",
            }:
                raise IdentityLedgerError("Existing or ambiguous ledger state")
            paths.append(path)
    return paths


def _scan(
    root: Path, *, initialized: bool
) -> dict[str, tuple[str, str, tuple[tuple[str, str, str], ...]]]:
    files = _walk(root, initialized=initialized)
    paths = set(files)
    claimed: set[Path] = set()
    ids: dict[str, tuple[str, str, tuple[tuple[str, str, str], ...]]] = {}
    names: set[str] = set()
    for sidecar in sorted(p for p in files if p.suffix.lower() == ".json"):
        if sidecar.suffix != ".json":
            raise IdentityLedgerError("Sidecar extension must be lowercase .json")
        info = _regular(sidecar)
        if info.st_size > 256 * 1024:
            raise IdentityLedgerError("Oversized sidecar")
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
            if not isinstance(data, dict) or not isinstance(data.get("id"), str):
                raise ValueError("bad sidecar")
            clip_id = _uuid(data["id"])
            saved = data.get("saved_files")
            if (
                not isinstance(saved, list)
                or not saved
                or not all(isinstance(n, str) for n in saved)
            ):
                raise ValueError("ambiguous members")
            if len({n.casefold() for n in saved}) != len(saved):
                raise ValueError("duplicate members")
        except (OSError, ValueError, UnicodeError) as exc:
            raise IdentityLedgerError(f"Invalid sidecar: {sidecar}") from exc
        sidecar_rel = sidecar.relative_to(root).as_posix()
        _path(root, sidecar_rel)
        sidecar_key = sidecar_rel.casefold()
        if clip_id in ids or sidecar_key in names:
            raise IdentityLedgerError("Conflicting UUID or path identities")
        names.add(sidecar_key)
        group = []
        media_count = 0
        stem = sidecar.name[:-5]
        for name in saved:
            if Path(name).name != name or "\\" in name or not name.startswith(stem + "."):
                raise IdentityLedgerError("Unsafe or ambiguous sidecar member")
            suffix = name[len(stem) :].lower()
            if suffix in _MEDIA:
                role = "media"
                media_count += 1
            elif suffix in _COVERS:
                role = "cover"
            else:
                raise IdentityLedgerError("Unsupported sidecar member")
            rel = (sidecar.parent / name).relative_to(root).as_posix()
            member = _path(root, rel)
            key = rel.casefold()
            if member not in paths or member in claimed or key in names or member == sidecar:
                raise IdentityLedgerError("Missing, conflicting or multiply claimed member")
            size = _regular(member).st_size
            if (role == "media" and size <= 1024) or (role == "cover" and size == 0):
                raise IdentityLedgerError("Invalid media or cover file size")
            names.add(key)
            claimed.add(member)
            group.append((rel, role, _hash(member)))
        if not media_count:
            raise IdentityLedgerError("Sidecar has no media member")
        _sidecar(sidecar, clip_id, set(saved))
        claimed.add(sidecar)
        ids[clip_id] = (sidecar_rel, _hash(sidecar), tuple(sorted(group)))
    if claimed != paths:
        raise IdentityLedgerError("Unindexed or unsupported archive files")
    return ids


def _sync_dir(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _marker(root: Path) -> str:
    path = root / _MARKER
    info = _internal_file(root, path)
    if info.st_size != 33:
        raise IdentityLedgerError("Invalid identity marker")
    value = path.read_text(encoding="ascii")
    if (
        not value.endswith("\n")
        or len(value[:-1]) != 32
        or any(c not in "0123456789abcdef" for c in value[:-1])
    ):
        raise IdentityLedgerError("Invalid identity marker")
    return value[:-1]


def _witness_bytes(data: dict[str, object]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _witness_read(root: Path, marker: str) -> list[dict]:
    path = root / _WITNESS
    info = _internal_file(root, path)
    if info.st_size > _MAX_WITNESS:
        raise IdentityLedgerError("Witness exceeds maximum size")
    data = path.read_bytes()
    if not data or not data.endswith(b"\n"):
        raise IdentityLedgerError("Witness is empty or truncated")
    result: list[dict] = []
    previous = "0" * 64
    used_tokens: set[str] = set()
    for seq, line in enumerate(data.splitlines(keepends=True)):
        try:
            record = json.loads(line, object_pairs_hook=_pairs)
        except (ValueError, UnicodeError) as exc:
            raise IdentityLedgerError("Invalid witness record") from exc
        if not isinstance(record, dict) or _witness_bytes(record) + b"\n" != line:
            raise IdentityLedgerError("Noncanonical witness record")
        if set(record) != {"seq", "previous", "marker", "state", "token", "checksum"}:
            raise IdentityLedgerError("Invalid witness record fields")
        body = {key: value for key, value in record.items() if key != "checksum"}
        token = record["token"]
        expected_state = "dirty" if seq % 2 == 0 else "clean"
        if (
            record["seq"] != seq
            or record["previous"] != previous
            or record["marker"] != marker
            or record["state"] != expected_state
            or not isinstance(token, str)
            or len(token) != 32
            or any(c not in "0123456789abcdef" for c in token)
            or record["checksum"] != hashlib.sha256(_witness_bytes(body)).hexdigest()
        ):
            raise IdentityLedgerError("Witness integrity check failed")
        if expected_state == "dirty":
            if token in used_tokens:
                raise IdentityLedgerError("Repeated witness transaction token")
            used_tokens.add(token)
        elif not result or token != result[-1]["token"]:
            raise IdentityLedgerError("Witness CLEAN does not match DIRTY")
        previous = record["checksum"]
        result.append(record)
    return result


def _witness_write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise IdentityLedgerError("Short witness write")
        offset += written
    os.fsync(fd)


def _witness_append(
    root: Path, marker: str, state: str, token: str, *, create: bool = False
) -> None:
    path = root / _WITNESS
    if create:
        if state != "dirty":
            raise IdentityLedgerError("Witness must start DIRTY")
        records: list[dict] = []
        previous = "0" * 64
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    else:
        records = _witness_read(root, marker)
        if (state == "dirty" and records[-1]["state"] != "clean") or (
            state == "clean" and (records[-1]["state"] != "dirty" or records[-1]["token"] != token)
        ):
            raise IdentityLedgerError("Unexpected witness state transition")
        previous = records[-1]["checksum"]
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    body: dict[str, object] = {
        "seq": len(records),
        "previous": previous,
        "marker": marker,
        "state": state,
        "token": token,
    }
    body["checksum"] = hashlib.sha256(_witness_bytes(body)).hexdigest()
    encoded = _witness_bytes(body) + b"\n"
    if not create and (root / _WITNESS).stat().st_size + len(encoded) > _MAX_WITNESS:
        raise IdentityLedgerError("Witness exceeds maximum size")
    fd = os.open(path, flags, 0o600)
    try:
        _witness_write_all(fd, encoded)
    finally:
        os.close(fd)
    if create:
        _sync_dir(root)


def _fault_point(_name: str) -> None:
    """Test-only process-interruption hook; no behavior in normal operation."""


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=0")
    mode = conn.execute("PRAGMA journal_mode=PERSIST").fetchone()[0]
    conn.execute("PRAGMA synchronous=EXTRA")
    conn.execute("PRAGMA foreign_keys=ON")
    if (
        str(mode).lower() != "persist"
        or conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "persist"
        or conn.execute("PRAGMA synchronous").fetchone()[0] != 3
        or conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1
    ):
        raise IdentityLedgerError("Required SQLite durability settings unavailable")


def _connect(root: Path) -> sqlite3.Connection:
    db = root / _DB
    _internal_file(root, db)
    for name in (_JOURNAL, _DB + "-wal", _DB + "-shm"):
        path = root / name
        if path.exists() or path.is_symlink():
            _internal_file(root, path)
            if name != _JOURNAL:
                raise IdentityLedgerError("WAL state is unsupported")
    uri = "file:" + quote(str(db), safe="/") + "?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0)
        try:
            _configure(conn)
            return conn
        except Exception:
            conn.close()
            raise
    except sqlite3.Error as exc:
        raise IdentityLedgerError("Cannot open SQLite identity database") from exc


def _check_schema(conn: sqlite3.Connection) -> None:
    try:
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise IdentityLedgerError("SQLite integrity check failed")
        if (
            conn.execute("PRAGMA application_id").fetchone()[0] != _APP_ID
            or conn.execute("PRAGMA user_version").fetchone()[0] != 1
        ):
            raise IdentityLedgerError("Unknown SQLite identity schema")
        objects = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        expected = {
            ("table", "metadata", _METADATA_SQL),
            ("table", "identities", _IDENTITIES_SQL),
            ("table", "members", _MEMBERS_SQL),
        }
        if set(objects) != expected:
            raise IdentityLedgerError("Unexpected SQLite schema objects")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise IdentityLedgerError("SQLite identity foreign key violation")
    except sqlite3.Error as exc:
        raise IdentityLedgerError("SQLite identity database is corrupt") from exc


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


class IdentityLedger:
    """Single-connection candidate; caller must retain the archive lock."""

    def __init__(self, root: Path, conn: sqlite3.Connection, *, archive_lock_held: bool) -> None:
        _require_lock(archive_lock_held)
        self.root = root
        self._conn = conn
        self._closed = False
        self._opening_previous: str | None = None
        self.reconcile(archive_lock_held=archive_lock_held)

    def __enter__(self) -> IdentityLedger:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def _check(self, archive_lock_held: bool) -> None:
        _require_lock(archive_lock_held)
        if self._closed:
            raise IdentityLedgerError("Ledger is closed")
        marker = _marker(self.root)
        events = _witness_read(self.root, marker)
        if self._opening_previous is None:
            if events[-1]["state"] != "clean":
                raise IdentityLedgerError("DIRTY witness requires manual review")
            expected_token = events[-1]["token"]
        else:
            if (
                len(events) < 3
                or events[-1]["state"] != "dirty"
                or events[-2]["state"] != "clean"
                or events[-2]["token"] != self._opening_previous
            ):
                raise IdentityLedgerError("Invalid witness while opening")
            expected_token = self._opening_previous
        if (
            self._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "persist"
            or self._conn.execute("PRAGMA synchronous").fetchone()[0] != 3
            or self._conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        ):
            raise IdentityLedgerError("SQLite durability settings changed")
        _check_schema(self._conn)
        meta = dict(self._conn.execute("SELECT key, value FROM metadata"))
        root_info = self.root.stat()
        if meta != {
            "marker": marker,
            "root_device": str(root_info.st_dev),
            "root_inode": str(root_info.st_ino),
            "token": expected_token,
        }:
            raise IdentityLedgerError("Ledger does not belong to this archive or witness is stale")
        _internal_file(self.root, self.root / _DB)

    def reconcile(self, *, archive_lock_held: bool = False) -> None:
        """Check every listed member and sidecar in both directions."""
        try:
            self._check(archive_lock_held)
            found = _scan(self.root, initialized=True)
            rows = self._conn.execute(
                "SELECT clip_id,audio_path,sidecar_path,audio_key,sidecar_key,state,"
                "audio_sha256,sidecar_sha256 FROM identities"
            ).fetchall()
            member_rows = self._conn.execute(
                "SELECT clip_id,path,path_key,role,sha256 FROM members"
            ).fetchall()
            groups: dict[str, list[tuple[str, str, str]]] = {}
            for clip_id, path, key, role, digest in member_rows:
                _uuid(clip_id)
                _path(self.root, path)
                if (
                    key != path.casefold()
                    or role not in {"media", "cover"}
                    or not _valid_hash(digest)
                ):
                    raise IdentityLedgerError("Corrupt identity member")
                groups.setdefault(clip_id, []).append((path, role, digest))
            complete = {}
            all_keys: set[str] = set()
            seen_ids = set()
            for clip_id, audio, sidecar, akey, skey, state, ahash, shash in rows:
                a, sidecar_path = _names(self.root, clip_id, audio, sidecar)
                seen_ids.add(clip_id)
                if akey != audio.casefold() or skey != sidecar.casefold() or skey in all_keys:
                    raise IdentityLedgerError("Corrupt or overlapping reservation")
                all_keys.add(skey)
                members = groups.get(clip_id, [])
                if state == "reserved":
                    if (
                        members
                        or akey in all_keys
                        or ahash is not None
                        or shash is not None
                        or a.exists()
                        or a.is_symlink()
                        or sidecar_path.exists()
                        or sidecar_path.is_symlink()
                    ):
                        raise IdentityLedgerError(
                            "Incomplete reserved output requires manual recovery"
                        )
                    all_keys.add(akey)
                elif state == "complete":
                    if not _valid_hash(ahash) or not _valid_hash(shash):
                        raise IdentityLedgerError("Invalid completed identity hash")
                    if (audio, "media", ahash) not in members:
                        raise IdentityLedgerError("Missing primary media identity")
                    for path, _role, _digest in members:
                        key = path.casefold()
                        if key in all_keys:
                            raise IdentityLedgerError("Overlapping member paths")
                        all_keys.add(key)
                    complete[clip_id] = (sidecar, shash, tuple(sorted(members)))
                else:
                    raise IdentityLedgerError("Invalid identity state")
            if set(groups) - seen_ids:
                raise IdentityLedgerError("Orphan ledger member")
            if found != complete:
                raise IdentityLedgerError("Unindexed, stale, missing or changed archive members")
        except sqlite3.Error as exc:
            raise IdentityLedgerError("SQLite identity database is corrupt") from exc

    def reserve(
        self, clip_id: str, audio: str, sidecar: str, *, archive_lock_held: bool = False
    ) -> None:
        """Durably reserve an identity and paths; publication is unsupported."""
        self.reconcile(archive_lock_held=archive_lock_held)
        a, s = _names(self.root, clip_id, audio, sidecar)
        if a.exists() or a.is_symlink() or s.exists() or s.is_symlink():
            raise IdentityLedgerError("Reservation destination already exists")
        keys = {audio.casefold(), sidecar.casefold()}
        if len(keys) != 2:
            raise IdentityLedgerError("Overlapping paths")
        occupied = self._conn.execute("SELECT audio_key, sidecar_key FROM identities").fetchall()
        member_keys = self._conn.execute("SELECT path_key FROM members").fetchall()
        if (
            any(key in keys for pair in occupied for key in pair)
            or any(key in keys for (key,) in member_keys)
            or self._conn.execute("SELECT 1 FROM identities WHERE clip_id=?", (clip_id,)).fetchone()
        ):
            raise IdentityLedgerError("Identity or paths already reserved")
        marker = _marker(self.root)
        token = uuid.uuid4().hex
        _witness_append(self.root, marker, "dirty", token)
        _fault_point("reserve_dirty")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            _fault_point("reserve_begin")
            self._conn.execute(
                "INSERT INTO identities VALUES (?,?,?,?,?,'reserved',NULL,NULL)",
                (clip_id, audio, sidecar, audio.casefold(), sidecar.casefold()),
            )
            self._conn.execute("UPDATE metadata SET value=? WHERE key='token'", (token,))
            _fault_point("reserve_before_commit")
            self._conn.execute("COMMIT")
            _fault_point("reserve_after_commit")
            _witness_append(self.root, marker, "clean", token)
            _fault_point("reserve_clean")
        except Exception as exc:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise IdentityLedgerError(
                "Reservation failed; DIRTY witness requires manual review"
            ) from exc


def initialize(
    root: Path,
    *,
    import_existing: bool = False,
    ownership_reviewed: bool = False,
    archive_lock_held: bool = False,
    storage_verified_local: bool = False,
) -> IdentityLedger:
    """Explicit offline initialization; populated imports require explicit opt-in."""
    _require_lock(archive_lock_held)
    _require_local_attestation(storage_verified_local)
    root = _root(root)
    for name in (_DB, _MARKER, _JOURNAL, _WITNESS, _DB + "-wal", _DB + "-shm"):
        path = root / name
        if path.exists() or path.is_symlink():
            raise IdentityLedgerError("Existing or ambiguous ledger state")
    found = _scan(root, initialized=False)
    if found and not import_existing:
        raise IdentityLedgerError("Import of existing media requires explicit opt-in")
    if found and ownership_reviewed is not True:
        raise IdentityLedgerError("Import requires explicit review of member ownership claims")
    marker_value = uuid.uuid4().hex
    marker_fd = os.open(
        root / _MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        with os.fdopen(marker_fd, "wb") as stream:
            stream.write((marker_value + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        pass
    _sync_dir(root)
    token = uuid.uuid4().hex
    _witness_append(root, marker_value, "dirty", token, create=True)
    _fault_point("init_dirty")
    fd = os.open(
        root / _DB, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_dir(root)
    conn = _connect(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"PRAGMA application_id={_APP_ID}")
        conn.execute("PRAGMA user_version=1")
        conn.execute(_METADATA_SQL)
        conn.execute(_IDENTITIES_SQL)
        conn.execute(_MEMBERS_SQL)
        root_info = root.stat()
        conn.executemany(
            "INSERT INTO metadata VALUES (?,?)",
            [
                ("marker", marker_value),
                ("root_device", str(root_info.st_dev)),
                ("root_inode", str(root_info.st_ino)),
                ("token", token),
            ],
        )
        identity_rows: list[tuple[str, str, str, str, str, str, str]] = []
        member_rows: list[tuple[str, str, str, str, str]] = []
        for clip_id, (sidecar, shash, members) in found.items():
            audio, _role, ahash = next(member for member in members if member[1] == "media")
            identity_rows.append(
                (clip_id, audio, sidecar, audio.casefold(), sidecar.casefold(), ahash, shash)
            )
            member_rows.extend(
                (clip_id, path, path.casefold(), role, digest) for path, role, digest in members
            )
        conn.executemany("INSERT INTO identities VALUES (?,?,?,?,?,'complete',?,?)", identity_rows)
        conn.executemany("INSERT INTO members VALUES (?,?,?,?,?)", member_rows)
        if _scan(root, initialized=True) != found:
            raise IdentityLedgerError("Archive changed during import")
        _fault_point("init_before_commit")
        conn.execute("COMMIT")
        _fault_point("init_after_commit")
        _witness_append(root, marker_value, "clean", token)
        _fault_point("init_clean")
        return IdentityLedger(root, conn, archive_lock_held=archive_lock_held)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise


def open_ledger(
    root: Path, *, archive_lock_held: bool = False, storage_verified_local: bool = False
) -> IdentityLedger:
    """Refuse uncertain witness state before opening SQLite, then reconcile."""
    _require_lock(archive_lock_held)
    _require_local_attestation(storage_verified_local)
    root = _root(root)
    marker = _marker(root)
    records = _witness_read(root, marker)
    if records[-1]["state"] != "clean":
        raise IdentityLedgerError("DIRTY witness requires manual review before SQLite open")
    previous = records[-1]["token"]
    token = uuid.uuid4().hex
    _witness_append(root, marker, "dirty", token)
    _fault_point("open_dirty")
    conn = _connect(root)
    try:
        candidate = IdentityLedger.__new__(IdentityLedger)
        candidate.root = root
        candidate._conn = conn
        candidate._closed = False
        candidate._opening_previous = previous
        candidate.reconcile(archive_lock_held=archive_lock_held)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE metadata SET value=? WHERE key='token'", (token,))
        _fault_point("open_before_commit")
        conn.execute("COMMIT")
        _fault_point("open_after_commit")
        _witness_append(root, marker, "clean", token)
        _fault_point("open_clean")
        candidate._opening_previous = None
        candidate.reconcile(archive_lock_held=archive_lock_held)
        return candidate
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise
