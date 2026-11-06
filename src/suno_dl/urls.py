"""Single source of truth for Suno identifiers, hosts, and URL conventions.

Both ``parser`` and ``discovery`` must import from here so the CDN conventions
cannot drift apart again (they did once: the cdn1 ``.mp3`` convention silently
started returning HTTP 403).
"""

from __future__ import annotations

import re

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

SITE_URL = "https://suno.com"
SONG_URL_TEMPLATE = SITE_URL + "/song/{clip_id}"

# Hosts the tool is willing to fetch media from. Parsed page content is
# untrusted, so an allow-list prevents SSRF against internal addresses.
ALLOWED_MEDIA_HOSTS = (
    "suno.ai",
    "suno.com",
    "cloudfront.net",
)

SUFFIX_RE = re.compile(r"\.(mp4|m4a|mp3|wav)$", re.IGNORECASE)
GUESS_SUFFIXES = ("mp4", "m4a", "mp3")


def is_uuid(value: str) -> bool:
    return bool(UUID_RE.fullmatch(value))


def song_page_url(clip_id: str) -> str:
    return f"{SITE_URL}/song/{clip_id}"


def cdn_cover_url(clip_id: str) -> str:
    return f"https://cdn2.suno.ai/image_{clip_id}.jpeg"


def cdn_guess_urls(clip_id: str) -> list[str]:
    """Last-resort CDN guesses; usually 403 now, kept only as a fallback."""
    return [f"https://cdn1.suno.ai/{clip_id}.{ext}" for ext in GUESS_SUFFIXES]


def is_allowed_media_url(url: str) -> bool:
    """Return True only for https URLs on an allow-listed host.

    Rejects credentials, non-https schemes, and IP literals (which would allow
    SSRF against metadata services or internal hosts).
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    if parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    # Reject IP literals (v4 and bracketed v6) outright.
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host) or ":" in host:
        return False
    return any(host == h or host.endswith("." + h) for h in ALLOWED_MEDIA_HOSTS)


def redact_url(url: str) -> str:
    """Strip query/fragment (which can carry signed-URL credentials)."""
    base = url.split("?", 1)[0].split("#", 1)[0]
    had_query = "?" in url or "#" in url
    return base + ("?...redacted" if had_query else "")
