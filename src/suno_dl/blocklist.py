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

import re
from pathlib import Path

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
