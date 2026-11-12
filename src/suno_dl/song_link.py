"""Resolve Suno song links for the skip list editor using page metadata."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx

from .errors import FetchError
from .fetcher import DEFAULT_HEADERS, read_capped
from .producer_lookup import ProducerLookupError, producer_identity_from_page
from .urls import UUID_RE, song_page_url

_SHARE_CODE = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _target(url: str) -> tuple[str, str | None]:
    if len(url) > 2048 or any(ord(char) <= 32 for char in url):
        raise ValueError("Paste a Suno song or Share link.")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.lower() != "suno.com":
        raise ValueError("Paste a Suno song or Share link.")
    path = parts.path.removesuffix("/")
    if path.startswith("/s/") and _SHARE_CODE.fullmatch(path[3:]):
        return "https://suno.com" + path, None
    if path.startswith("/song/") and UUID_RE.fullmatch(path[6:]):
        clip_id = path[6:].lower()
        query = parse_qs(parts.query, max_num_fields=32)
        share = query.get("sh", [])
        canonical = song_page_url(clip_id)
        if len(share) == 1 and _SHARE_CODE.fullmatch(share[0]):
            canonical += "?" + urlencode({"sh": share[0]})
        return canonical, clip_id
    raise ValueError("Paste a Suno song or Share link.")


async def resolve_song_link(link: str) -> dict[str, object]:
    url, clip_id = _target(link.strip())
    try:
        async with (
            asyncio.timeout(12),
            httpx.AsyncClient(
                headers=DEFAULT_HEADERS, trust_env=False, follow_redirects=False, timeout=6
            ) as client,
        ):
            for _ in range(3):
                async with client.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            break
                        next_url, next_id = _target(urljoin(url, location))
                        if clip_id is not None and next_id != clip_id:
                            break
                        url, clip_id = next_url, next_id
                        continue
                    if (
                        response.status_code != 200
                        or not clip_id
                        or response.headers.get("content-type", "").split(";", 1)[0] != "text/html"
                    ):
                        break
                    try:
                        title = producer_identity_from_page(
                            clip_id, (await read_capped(response)).decode("utf-8")
                        ).title
                    except ProducerLookupError:
                        title = None
                    return {"id": clip_id, "url": song_page_url(clip_id), "title": title}
    except (FetchError, httpx.HTTPError, TimeoutError, UnicodeError):
        raise ValueError("Suno could not be reached for a title lookup.") from None
    raise ValueError("Suno did not return a song page for this link.")
