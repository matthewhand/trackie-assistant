"""Track blocklist: clip IDs the tool must never fetch.

Why this exists
---------------
Some tracks are permanently unavailable to anonymous fetches — Suno answers
HTTP 403 for those specific objects on every format while identical requests
for sibling tracks succeed. Repeatedly knocking on a blocked object looks
like automation pressure and risks the rate-limiting the human-paced
defaults exist to avoid. A blocklist makes the deliberate backing-off
explicit and durable: the skip happens before any request, every run,
without anyone having to remember.

File format
-----------
One clip ID per line, in the archive root (next to ``.env``), named
``.blocked`` by default (``SUNO_BLOCKED_FILE`` overrides the path).
``#`` starts a comment; blank lines and surrounding whitespace are
ignored. Example::

    # Spring Has No Alibi — Suno 403s this object for anonymous fetches
    # (verified 2026-09-28); do not retry, download it via Suno's UI.
    7c68eb8d-4bad-43d5-8d37-8be1fd48fb58

A missing file is not an error — an empty blocklist is the default state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from threading import Lock

from .errors import ConfigError

__all__ = ["DEFAULT_BLOCKED_FILENAME", "load_blocked_ids"]

DEFAULT_BLOCKED_FILENAME = ".blocked"
# Suno clip IDs are lowercase UUIDs; accept any UUID spelling, compare
# case-insensitively at the lookup site.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def load_blocked_ids(path: str | Path | None = None) -> frozenset[str]:
    """Load blocked clip IDs from ``path`` (default ``<cwd>/.blocked``).

    Returns a lowercase frozenset for O(1) membership checks. Malformed
    non-empty, non-comment lines are a configuration error (fail loudly
    rather than silently not-blocking); a missing file yields an empty set.
    """
    file = Path(path) if path is not None else Path.cwd() / DEFAULT_BLOCKED_FILENAME
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        raise ConfigError(f"cannot read blocklist {file}: {exc}") from exc

    return _parse_ids(text, file)


def _parse_ids(text: str, file: Path) -> frozenset[str]:
    blocked: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not _UUID_RE.fullmatch(line):
            raise ConfigError(
                f"{file}:{lineno}: expected a clip UUID (got {raw.strip()!r}); "
                "one ID per line, '#' starts a comment"
            )
        blocked.add(line.lower())
    return frozenset(blocked)


_EDITOR_LIMIT = 32 * 1024
_EDITOR_LOCK = Lock()


def _editor_payload(text: str, path: Path) -> dict:
    ids = _parse_ids(text, path)
    notes = {}
    previous = ""
    for raw in text.splitlines():
        value, _, comment = raw.partition("#")
        clip = value.strip().lower()
        if clip in ids:
            note = comment.strip() or previous
            title, sep, reason = note.partition(" | ")
            entry = {
                "id": clip,
                "title": title if sep else clip,
                "reason": reason if sep else note or "Manually skipped",
            }
            if comment.strip().startswith("trackie: "):
                try:
                    fields = json.loads(comment.strip().removeprefix("trackie: "))
                except ValueError:
                    fields = None
                if isinstance(fields, dict) and all(
                    isinstance(fields.get(key), str) for key in ("title", "reason")
                ):
                    entry["title"] = fields["title"] or clip
                    entry["reason"] = fields["reason"] or "Manually skipped"
            notes[clip] = entry
        previous = comment.strip() if not clip and " | " in comment else ""
    return {
        "text": text,
        "revision": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "entries": list(notes.values()),
    }


def read_blocklist_editor(path: Path) -> dict:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigError("Skip list must be a regular file")
    try:
        with path.open("rb") as stream:
            data = stream.read(_EDITOR_LIMIT + 1)
    except FileNotFoundError:
        data = b""
    if len(data) > _EDITOR_LIMIT:
        raise ConfigError("Skip list exceeds the editor size limit")
    return _editor_payload(data.decode("utf-8"), path)


def save_blocklist_editor(path: Path, text: str, revision: str) -> dict | None:
    """Return None when another editor has already changed the list."""
    if len(text.encode("utf-8")) > _EDITOR_LIMIT:
        raise ConfigError("Skip list exceeds the editor size limit")
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        raise ConfigError("Skip list contains unsupported control characters")
    updated = _editor_payload(text, path)
    with _EDITOR_LOCK:
        current = read_blocklist_editor(path)
        if current["revision"] != revision:
            return None
        if current["text"] == text:
            return current
        fd, name = tempfile.mkstemp(prefix=".blocked-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(text.encode("utf-8"))
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    return updated
