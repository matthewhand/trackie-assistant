"""Filesystem-safe filename construction.

All untrusted strings (track titles, handles) and user-supplied patterns pass
through here.  The guarantees we test for:

* the result is always a *single path component* (no separators, no ``..``)
* Windows reserved device names (``CON``, ``NUL``, ``COM1`` ...) are neutralised
* leading dots are stripped so we cannot create dotfiles
* the result is non-empty and bounded in length
* media basenames carry no period at all, because tag-reading players
  (Music Assistant above all) treat the first ``.`` in a basename as the
  extension boundary and would truncate ``Song (feat. Alice)`` to
  ``Song (feat`` — see :func:`build_filename`
"""

from __future__ import annotations

import re
from string import Formatter
from urllib.parse import urlparse

_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")
_SEPARATOR_RUN_RE = re.compile(r"_{2,}")

# Windows reserved device names, case-insensitive, with or without extension.
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_LEN = 180

# A mid-name period ends a filename for every tag reader that guesses metadata
# from the name: Music Assistant splits on the first ``.`` and treats the rest
# as the extension, so ``drinko_Song (feat. Alice).m4a`` is read as title
# ``drinko_Song (feat``. The full title always survives in the
# embedded tag; the filename only has to stop tripping the parser.


def ma_safe_basename(name: str) -> str:
    """Encode every period in a component as an underscore (``feat.`` -> ``feat_``).

    Leading/trailing periods cannot survive :func:`sanitize_component` (a
    dotfile / Windows-stripped name), so whatever is left here is mid-name and
    is exactly what a tag-guessing player would mistake for an extension.
    """
    return name.replace(".", "_")


def sanitize_component(name: str, max_len: int = MAX_LEN) -> str:
    """Return a single, safe path component (no separators)."""
    name = _UNSAFE_RE.sub("_", name)
    name = _WHITESPACE_RE.sub(" ", name)
    name = _SEPARATOR_RUN_RE.sub("_", name)
    name = re.sub(r"(_ )|( _)", "_", name)
    # Strip leading dots/spaces (no dotfiles, no empty-name tricks) and any
    # trailing space/dot/underscore (Windows strips these, causing mismatches).
    name = name.strip(" ._")
    name = name.rstrip(" ._")
    if not name:
        return "untitled"
    stem = name.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        name = "_" + name
    return name[:max_len].rstrip(" ._") or "untitled"


def pattern_uses_artist(pattern: str) -> bool:
    """Whether a format pattern references the producer, including format specs."""
    try:
        for _, field, spec, _ in Formatter().parse(pattern):
            if field and field.split(".", 1)[0].split("[", 1)[0] == "artist":
                return True
            if spec and pattern_uses_artist(spec):
                return True
    except (ValueError, RecursionError):
        return False
    return False


def build_filename(
    pattern: str,
    *,
    title: str,
    handle: str,
    clip_id: str,
    artist: str | None = None,
    ma_safe: bool = True,
) -> str:
    """Format ``pattern`` with sanitised placeholders and re-sanitise the result.

    Unknown placeholders fall back to a safe default rather than raising.

    ``ma_safe`` (default) also encodes mid-name periods as underscores, because
    every tag reader that infers metadata from a filename splits on the first
    ``.`` and would truncate ``feat.`` titles. Pass
    ``ma_safe=False`` to reproduce the pre-1.6 name — the downloader uses that
    only to recognise already-downloaded tracks and never to write one.
    """
    safe_title = sanitize_component(title)
    safe_handle = sanitize_component(handle)
    safe_id = sanitize_component(clip_id)
    safe_artist = sanitize_component(artist or "Unknown artist")
    try:
        name = pattern.format(handle=safe_handle, title=safe_title, id=safe_id, artist=safe_artist)
    except (KeyError, IndexError, ValueError):
        name = f"{safe_handle}_{safe_title}_{safe_id}"
    name = sanitize_component(name)
    return ma_safe_basename(name) if ma_safe else name


def source_slug(url: str) -> str:
    """Derive a safe subfolder name from a Suno source URL.

    Profiles map to their handle (without ``@``), playlists and songs to
    their ids. The result is always a single sanitized path component, so
    nesting it under the output directory cannot escape containment.
    """
    path = urlparse(url).path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "downloads"
    last = parts[-1]
    if last.startswith("@"):
        last = last[1:]
    if not last:
        return "downloads"
    return sanitize_component(last, max_len=64) or "downloads"


def normalize_watch_entry(entry: str) -> tuple[str, str]:
    """Resolve a watch-list entry to ``(source_url, handle)``.

    Accepts a bare handle (``@drinko`` or ``drinko``) or a full public Suno
    URL. Bare handles become profile URLs; URL entries pass through with the
    handle extracted when the entry is a profile URL. Raises ``ValueError``
    for anything that is neither.
    """
    text = entry.strip()
    if text.startswith("http://") or text.startswith("https://"):
        path = urlparse(text).path.rstrip("/")
        parts = [p for p in path.split("/") if p]
        if not text.lower().startswith("https://suno.com/") or not parts:
            raise ValueError(
                f"Watch-list entry {entry!r} must be a public suno.com URL "
                "(https://suno.com/@handle, /playlist/<id>, or /song/<id>)."
            )
        handle = ""
        if path.startswith("/@"):
            handle = parts[-1].lstrip("@")
        return text, handle
    handle = text.lstrip("@").strip()
    if not handle or any(ch in handle for ch in "/?#@"):
        raise ValueError(f"Watch-list entry {entry!r} is not a handle or a https://suno.com/ URL.")
    return f"https://suno.com/@{handle}", handle
