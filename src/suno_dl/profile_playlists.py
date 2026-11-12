"""Anonymous public-profile playlist pagination used by Suno's View all screen.

Only the fixed read-only feed request is allowed. Membership and visibility
are independently verified by the existing per-playlist reader.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .fetcher import USER_AGENT
from .playlist_api import (
    PublicPlaylistApiError,
    _finite_json_float,
    _invalid_json_constant,
    _read_capped,
    _unique_json_object,
)
from .playlists import (
    MAX_DISCOVERY_PAGES,
    MAX_PLAYLISTS,
    PlaylistDiscovery,
    PlaylistRef,
    _playlist_id,
    _playlist_item_public,
)
from .ratelimit import HostRateLimiter
from .rsc import flight_documents

PUBLIC_PROFILE_FEED_URL = "https://suno.com/a/api/unified/feed"


def _decode(value: str | bytes) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_unique_json_object,
        parse_constant=_invalid_json_constant,
        parse_float=_finite_json_float,
    )


def _handle(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
        raise PublicPlaylistApiError("invalid public profile handle")
    return value.lower()


def profile_user_id(html: str, handle: str) -> str:
    """Read the route's profile identity, never a clip artist or viewer ID."""
    handle = _handle(handle)
    identities: set[str] = set()
    for tree in flight_documents(html):
        todo = [tree]
        while todo:
            node = todo.pop()
            if isinstance(node, dict):
                data = node.get("v2Data")
                if isinstance(data, dict):
                    meta = data.get("metadata")
                    feed = data.get("feed")
                    if isinstance(meta, dict) and isinstance(feed, dict):
                        uid = _playlist_id(data.get("user_id"))
                        if (
                            uid
                            and meta.get("user_id") == uid
                            and isinstance(meta.get("handle"), str)
                            and meta["handle"].lower() == handle
                            and feed.get("feed_id") == "artist_profile"
                        ):
                            identities.add(uid)
                todo.extend(node.values())
            elif isinstance(node, list):
                todo.extend(node)
    if len(identities) != 1:
        raise PublicPlaylistApiError("public profile identity is missing or ambiguous")
    return identities.pop()


async def fetch_public_profile_playlists_page(
    user_id: str,
    cursor: str | None,
    *,
    timeout_sec: float,
    rate_limiter: HostRateLimiter,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> dict[str, Any]:
    """POST a fixed read-only listing query; never send credentials or cookies."""
    if _playlist_id(user_id) != user_id:
        raise PublicPlaylistApiError("invalid public profile UUID")
    if cursor is not None and (
        not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9_=/+.-]{1,512}", cursor)
    ):
        raise PublicPlaylistApiError("invalid public playlist cursor")
    payload = {
        "feed_id": "user_playlists",
        "cursor": cursor,
        "page_size": 20,
        "request_metadata": {"sort_by": "created_at"},
        "target_user_id": user_id,
    }
    await rate_limiter.acquire(PUBLIC_PROFILE_FEED_URL)
    transport = transport_factory() if transport_factory else None
    async with (
        httpx.AsyncClient(
            timeout=timeout_sec,
            auth=None,
            cookies={},
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        ) as client,
        client.stream("POST", PUBLIC_PROFILE_FEED_URL, json=payload) as response,
    ):
        if response.status_code != 200:
            raise PublicPlaylistApiError(
                f"public profile playlist listing returned HTTP {response.status_code}"
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json" and not content_type.endswith("+json"):
            raise PublicPlaylistApiError("public profile playlist listing did not return JSON")
        body = await _read_capped(response)
    try:
        result = _decode(body)
    except (ValueError, RecursionError) as exc:
        raise PublicPlaylistApiError("invalid public profile playlist JSON") from exc
    if not isinstance(result, dict):
        raise PublicPlaylistApiError("invalid public profile playlist response shape")
    return result


async def discover_cursor_playlists(
    fetch_html: Callable[[str], Awaitable[str]],
    fetch_page: Callable[[str, str | None], Awaitable[dict[str, Any]]],
    handle: str,
    *,
    max_pages: int = MAX_DISCOVERY_PAGES,
    max_playlists: int = MAX_PLAYLISTS,
) -> PlaylistDiscovery:
    """Follow the public listing's cursor through its explicit terminal page."""
    handle = _handle(handle)
    uid = profile_user_id(await fetch_html(f"https://suno.com/@{handle}?page=playlists"), handle)
    seen: dict[str, PlaylistRef] = {}
    cursors: set[str] = set()
    cursor: str | None = None
    skipped = 0
    pages = 0
    reason = None
    for page in range(1, max_pages + 1):
        pages = page
        try:
            payload = await fetch_page(uid, cursor)
            feed = payload.get("feed")
            if not isinstance(feed, dict) or (
                feed.get("feed_id") != "user_playlists"
                or feed.get("feed_container_type") != "playlist"
            ):
                raise PublicPlaylistApiError("public playlist feed identity does not match")
            items = feed.get("items")
            if (
                not isinstance(items, list)
                or len(items) > 20
                or ("next_cursor" not in feed and len(items) == 20)
            ):
                raise PublicPlaylistApiError(
                    "public playlist feed omitted bounded membership or cursor"
                )
            for item in items:
                if not isinstance(item, dict) or item.get("content_type") != "playlist_shortcut":
                    raise PublicPlaylistApiError(
                        "public playlist feed contains an unsupported item"
                    )
                data = item.get("content_item")
                if not isinstance(data, dict):
                    raise PublicPlaylistApiError("public playlist feed contains a malformed item")
                pid = _playlist_id(data.get("playlist_id"))
                if pid is None or item.get("content_id") != pid:
                    raise PublicPlaylistApiError(
                        "public playlist feed contains an invalid playlist UUID"
                    )
                if not _playlist_item_public(item) or not _playlist_item_public(data):
                    skipped += 1
                    continue
                owner = data.get("playlist_user_handle")
                if not isinstance(owner, str) or owner.lower() != handle:
                    raise PublicPlaylistApiError(
                        "public playlist feed contains another profile's playlist"
                    )
                count = data.get("playlist_song_count")
                if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 5000:
                    raise PublicPlaylistApiError(
                        "public playlist feed contains an invalid song count"
                    )
                name = data.get("playlist_name")
                if not isinstance(name, str) or not name.strip():
                    raise PublicPlaylistApiError("public playlist feed contains an invalid name")
                ref = PlaylistRef(pid, name.strip(), owner, count)
                if pid in seen:
                    raise PublicPlaylistApiError(
                        "public playlist feed repeated a playlist during pagination"
                    )
                seen[pid] = ref
                if len(seen) > max_playlists:
                    seen.pop(pid)
                    raise PublicPlaylistApiError(
                        "public playlist discovery reached its playlist cap"
                    )
            next_cursor = feed.get("next_cursor")
            if next_cursor is None:
                break
            if (
                not isinstance(next_cursor, str)
                or not re.fullmatch(r"[A-Za-z0-9_=/+.-]{1,512}", next_cursor)
                or next_cursor in cursors
                or not items
            ):
                raise PublicPlaylistApiError(
                    "public playlist feed returned a repeated or invalid cursor"
                )
            cursors.add(next_cursor)
            cursor = next_cursor
        except Exception as exc:  # one failed listing must not erase verified discovery
            if not seen:
                raise
            detail = str(exc) if isinstance(exc, PublicPlaylistApiError) else type(exc).__name__
            reason = f"Public playlist discovery incomplete: {detail}"
            break
    else:
        reason = "Public playlist discovery reached its page limit"
    return PlaylistDiscovery(
        tuple(seen.values()),
        pages=pages,
        skipped_not_public=skipped,
        truncated=reason is not None,
        reason=reason,
    )
