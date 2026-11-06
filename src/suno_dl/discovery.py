"""Discovery of public Suno profile song links.

This module provides two discovery methods:
1. Feed-based discovery (primary): uses the anonymous studio feed API to page
   through all public clips for a profile. No browser required.
2. Playwright-based discovery (fallback): drives a headless Chromium to scroll
   the profile's songs view and collect `/song/<uuid>` anchors.

The feed API returns all public clips (hundreds per profile, ~20 per page)
while the Playwright method only finds the featured subset unless the user
clicks "View all" and scrolls extensively.

The result is a bare list of clip IDs and titles. Media URLs are intentionally
NOT resolved here: that belongs to the downloader, which resolves each track
under its concurrency bound so that discovery stays bounded and per-track
pages are fetched exactly once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from typing import Any, Protocol

import httpx

from .fetcher import read_capped
from .models import TrackInfo
from .urls import UUID_RE, cdn_cover_url, cdn_guess_urls, redact_url, song_page_url

logger = logging.getLogger(__name__)


class BrowserDependencyMissing(ImportError):
    """The optional Playwright Python package is unavailable."""


class ChromiumExecutableMissing(RuntimeError):
    """The Playwright Chromium executable is unavailable."""


def _log_source(value: str) -> str:
    """Redact URL extras and escape characters that can forge log output."""
    safe = redact_url(value)
    return "".join(
        (f"\\u{ord(char):04x}" if ord(char) <= 0xFFFF else f"\\U{ord(char):08x}")
        if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
        else char
        for char in safe
    )


_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Studio feed API base URL (anonymous, no auth required)
_FEED_API_BASE = "https://studio-api.prod.suno.com"


async def _fetch_feed_page(
    client: httpx.AsyncClient,
    handle: str,
    page: int,
    timeout: float = 30.0,
) -> dict:
    """Fetch a single page of the profile feed.

    The anonymous studio profile endpoint returns paginated public clips.
    ``clips_sort_by`` and ``playlists_sort_by`` are required by the server
    (HTTP 422 without them). Observed response shape (tolerated defensively):
    {
        "clips": [{"id": "...", "title": "...", ...}, ...],
        "num_total_clips": int,
        "current_page": int,
        ...
    }
    The older ``/api/profiles/<handle>/songs`` URL answers 404 (#113).
    """
    url = f"{_FEED_API_BASE}/api/profiles/{handle.lstrip('@')}"
    params: dict[str, str | int] = {
        "page": page,
        "clips_sort_by": "created_at",
        "playlists_sort_by": "created_at",
    }
    async with client.stream("GET", url, params=params, timeout=timeout) as resp:
        resp.raise_for_status()
        payload = await read_capped(resp)  # bounded read of untrusted JSON (#157)
    data = json.loads(payload)
    return data if isinstance(data, dict) else {}


async def fetch_profile_feed_page(
    client: httpx.AsyncClient,
    handle: str,
    page: int,
    timeout: float = 30.0,
    rate_limiter: object | None = None,
) -> dict:
    """One paced, size-capped profile feed page for callers outside this module.

    Playlist discovery (#160) pages the same endpoint, so it needs the same
    bound (``read_capped``) and the same politeness without duplicating the
    request. ``rate_limiter`` is any object with ``await acquire(url)``
    (``HostRateLimiter``); ``None`` skips pacing for an injected test client.
    """
    acquire = getattr(rate_limiter, "acquire", None)
    if callable(acquire):
        await acquire(_FEED_API_BASE)
    return await _fetch_feed_page(client, handle, page, timeout)


def _creator_from_feed_item(item: dict, source_handle: str) -> str | None:
    """Use clip-local identity, not the source profile, when it is provided.

    Display names remain verbatim UTF-8 (no Unicode normalization); canonical
    handles are accepted separately when no display name is available. A
    malformed explicit identity is unresolved, never replaced by the owner.
    """
    fields = ("display_name", "handle")
    # A clip in a playlist row often carries its producer as a nested object
    # (``user``/``author``/``creator``) rather than flattened fields. Each song
    # must be credited to that row's producer, never to the playlist owner.
    sources = _identity_sources(item)
    if _identity_is_ambiguous(item):
        logger.warning("Unresolved producer: conflicting clip-local identities")
        return None
    for source in sources:
        creator = _validated_identity(source, fields)
        if creator is not None:
            return creator
    if any(field in source for source in sources for field in fields):
        logger.warning("Unresolved producer: empty or invalid clip-local identity")
        return None
    # Legacy profile-feed shape does not provide creator fields; retain the
    # existing source-handle fallback for backwards compatibility.
    return source_handle.lstrip("@") or None


def _canonical_handle(item: dict) -> str | None:
    """The clip's canonical handle, flattened or nested, validated the same way."""
    for source in _identity_sources(item):
        if "handle" in source:
            return _validated_identity({"handle": source["handle"]}, ("handle",))
    return None


def _identity_sources(item: dict) -> list[dict]:
    """The row itself plus any nested producer objects, nearest first."""
    sources: list[dict] = [item]
    for key in ("user", "author", "creator", "profile"):
        nested = item.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    return sources


def _identity_is_ambiguous(item: dict) -> bool:
    """Compare like fields; a display name may legitimately differ from a handle."""
    sources = _identity_sources(item)
    for field in ("display_name", "handle"):
        values = {
            value
            for source in sources
            if (value := _validated_identity(source, (field,))) is not None
        }
        if len(values) > 1:
            return True
    return False


def _validated_identity(source: dict, fields: tuple[str, ...]) -> str | None:
    """First valid display name/handle in ``source``; None when unusable."""
    for field in fields:
        if field not in source:
            continue
        value = source[field]
        if not isinstance(value, str) or not value.strip():
            continue
        # Inspect before encoding to avoid allocating an unbounded UTF-8 buffer.
        if len(value) > 256 or any(
            ord(char) < 32
            or 127 <= ord(char) <= 159
            or 0xD800 <= ord(char) <= 0xDFFF
            or 0x202A <= ord(char) <= 0x202E
            or 0x2066 <= ord(char) <= 0x2069
            or ord(char) in {0x061C, 0x200E, 0x200F, 0x2028, 0x2029}
            or char in "<>"
            or (field == "handle" and char in "/\\?#%")
            for char in value
        ):
            logger.warning("Unresolved producer: invalid clip-local %s", field)
            return None
        if len(value.encode("utf-8")) > 512:
            logger.warning("Unresolved producer: oversized clip-local %s", field)
            return None
        creator = value.strip().lstrip("@") if field == "handle" else value.strip()
        if not creator:
            logger.warning("Unresolved producer: empty canonical handle")
            return None
        return creator
    return None


def _clip_from_feed_item(item: dict, handle: str) -> TrackInfo | None:
    """Convert a feed API clip item to TrackInfo."""
    clip_id = str(item.get("id") or "").lower()
    if not UUID_RE.fullmatch(clip_id):
        return None
    title = str(item.get("title") or "Untitled")
    media_urls = _media_candidates(clip_id)
    creator_handle = _canonical_handle(item)
    artist = _creator_from_feed_item(item, handle)
    has_local_identity = any(
        field in source
        for source in _identity_sources(item)
        for field in ("display_name", "handle")
    )
    return TrackInfo(
        id=clip_id,
        title=title,
        artist=artist,
        artist_is_fallback=bool(artist and not has_local_identity),
        creator_handle=creator_handle,
        page_url=song_page_url(clip_id),
        audio_url=media_urls[0],
        cover_url=cdn_cover_url(clip_id),
        media_urls=media_urls,
    )


async def discover_profile_tracks_feed(
    handle: str,
    timeout: float = 30.0,
    max_pages: int = 50,
    rate_limit_sec: float = 1.5,
) -> list[TrackInfo]:
    """Discover all public tracks for a profile via the anonymous studio feed API.

    Pages through the feed endpoint until no more clips are returned or
    ``max_pages`` is reached. Applies a polite per-host rate limit between
    requests.

    Args:
        handle: Profile handle (with or without leading '@').
        timeout: Request timeout in seconds.
        max_pages: Maximum number of pages to fetch (safety bound).
        rate_limit_sec: Minimum seconds between requests to the feed host.

    Returns:
        List of TrackInfo for all discovered public clips.
    """
    from .ratelimit import HostRateLimiter

    handle_clean = handle.lstrip("@")
    handle_label = _log_source(handle_clean)
    tracks: dict[str, TrackInfo] = {}
    limiter = HostRateLimiter(rate_limit_sec)

    async with httpx.AsyncClient(
        timeout=timeout,
        headers={
            "User-Agent": _UA,
            "Accept": "application/json",
        },
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for page in range(1, max_pages + 1):
            await limiter.acquire(_FEED_API_BASE)
            try:
                data = await _fetch_feed_page(client, handle_clean, page, timeout)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.warning(
                        "Feed endpoint returned 404 for @%s (page %d): unknown profile or the "
                        "API changed",
                        handle_label,
                        page,
                    )
                    break
                logger.warning(
                    "Feed page %d failed for @%s (HTTP %d)",
                    page,
                    handle_label,
                    exc.response.status_code,
                )
                break
            except httpx.RequestError as exc:
                logger.warning(
                    "Feed page %d request error for @%s (%s)",
                    page,
                    handle_label,
                    type(exc).__name__,
                )
                break

            clips = data.get("clips") or data.get("items") or data.get("data") or []
            if not clips:
                logger.debug("Feed page %d empty for @%s", page, handle_label)
                break

            new_count = 0
            for item in clips:
                track = _clip_from_feed_item(item, handle_clean)
                if track and track.id not in tracks:
                    tracks[track.id] = track
                    new_count += 1

            logger.debug("Feed page %d: %d new tracks (total %d)", page, new_count, len(tracks))

            # Stop if no new clips or API signals end of feed
            if new_count == 0:
                break
            total = data.get("num_total_clips")
            if isinstance(total, int) and total > 0 and len(tracks) >= total:
                break
            has_more = data.get("has_more")
            if has_more is None:
                has_more = data.get("hasMore")
            if has_more is None:
                has_more = data.get("next_page")
            if has_more is False:
                break

    logger.info("Feed discovery complete for @%s: %d tracks", handle_label, len(tracks))
    return list(tracks.values())


# JS that scrolls the largest scrollable container (the songs list is an inner
# `overflow-y-auto` element, so window scrolling alone does nothing).
_SCROLL_JS = (
    "() => {"
    "  let best = null;"
    "  for (const el of document.querySelectorAll('div, main, section')) {"
    "    const over = el.scrollHeight - el.clientHeight;"
    "    if (over > 50 && el.clientHeight > 200) {"
    "      if (!best || over > best.scrollHeight - best.clientHeight) best = el;"
    "    }"
    "  }"
    "  if (best) best.scrollTop = best.scrollHeight;"
    "  window.scrollTo(0, document.body.scrollHeight);"
    "  const se = document.scrollingElement;"
    "  if (se) se.scrollTop = se.scrollHeight;"
    "}"
)


class _Page(Protocol):
    """The subset of the Playwright page API the pure helpers need.

    Typed loosely on purpose: this documents the duck-type the tests fake, not
    every Playwright overload. ``goto`` is intentionally excluded — it is used
    only by ``_discover_sync`` directly.
    """

    def locator(self, selector: str) -> Any: ...
    def evaluate(self, expression: str) -> Any: ...
    def wait_for_timeout(self, ms: int) -> None: ...


def _media_candidates(clip_id: str) -> list[str]:
    """CDN guesses, used only until the downloader resolves the real URL."""
    return cdn_guess_urls(clip_id)


def collect_tracks(page: _Page, handle: str, tracks: dict[str, TrackInfo]) -> None:
    """Merge every ``/song/<uuid>`` anchor on ``page`` into ``tracks``.

    Pure with respect to Playwright's transport: any object exposing a
    ``locator(selector)`` returning something with ``count()`` / ``nth(i)`` /
    ``get_attribute`` / ``inner_text`` works. This is what the tests fake.
    """
    anchors = page.locator('a[href*="/song/"]')
    for i in range(anchors.count()):
        href = anchors.nth(i).get_attribute("href")
        if not href:
            continue
        m = UUID_RE.search(href)
        if not m:
            continue
        clip_id = m.group(0).lower()
        if clip_id in tracks:
            continue
        raw = anchors.nth(i).inner_text() or ""
        title = " ".join(raw.split()) or "Untitled"
        # Media URLs are resolved later by the downloader, under its
        # concurrency bound, so we only record CDN guesses here.
        media_urls = _media_candidates(clip_id)
        tracks[clip_id] = TrackInfo(
            id=clip_id,
            title=title,
            artist=handle.lstrip("@") or None,
            artist_is_fallback=True,
            page_url=song_page_url(clip_id),
            audio_url=media_urls[0],
            cover_url=cdn_cover_url(clip_id),
            media_urls=media_urls,
            model_version=None,
        )


def read_target_songs(page: _Page) -> int | None:
    """Read the profile's advertised song count from the page body, if present."""
    body = page.locator("body").inner_text()
    m = re.search(r"(\d+)\s+songs?\b", body, re.IGNORECASE)
    return int(m.group(1)) if m else None


def scroll_until_stable(
    page: _Page,
    tracks: dict[str, TrackInfo],
    handle: str,
    target_songs: int | None,
    max_idle: int,
    max_iters: int,
) -> None:
    """Scroll the songs container until no new tracks appear ``max_idle`` times.

    Stops early when ``target_songs`` is known and reached. This is the loop
    that used to be untestable inline code.
    """
    idle = 0
    prev_count = len(tracks)
    for i in range(max_iters):
        before = len(tracks)
        page.evaluate(_SCROLL_JS)
        page.wait_for_timeout(1_500)
        collect_tracks(page, handle, tracks)
        count = len(tracks)
        if count == before and count == prev_count:
            idle += 1
            if idle >= max_idle:
                break
        else:
            idle = 0
        prev_count = count
        if target_songs is not None and count >= target_songs:
            break
        if i % 10 == 0:
            logger.debug("Scroll iteration %d: %d/%s tracks", i, count, target_songs)


def _discover_sync(
    profile_url: str,
    handle: str,
    headless: bool,
    max_idle: int,
    max_iters: int,
) -> list[TrackInfo]:
    from playwright.sync_api import sync_playwright

    tracks: dict[str, TrackInfo] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            context = browser.new_context(user_agent=_UA)
            page = context.new_page()

            logger.info("Opening %s in browser ...", _log_source(profile_url))
            page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(8_000)
            collect_tracks(page, handle, tracks)
            target_songs = read_target_songs(page)
            if target_songs:
                logger.info("Public profile reports %d songs", target_songs)

            # The profile only renders a featured subset; click "View all".
            view_all = page.locator('button[aria-label*="View all" i], a:has-text("View all")')
            if view_all.count() > 0:
                logger.debug("Clicking 'View all' to open the songs view")
                try:
                    view_all.first.click(timeout=15_000)
                    page.wait_for_timeout(6_000)
                except Exception as exc:  # pragma: no cover - UI variance
                    logger.debug("View all click failed (%s)", type(exc).__name__)
                collect_tracks(page, handle, tracks)
                logger.debug("After 'View all': %d tracks", len(tracks))

            scroll_until_stable(page, tracks, handle, target_songs, max_idle, max_iters)

            collect_tracks(page, handle, tracks)
            count_now = len(tracks)
            if target_songs is not None and count_now < target_songs:
                logger.warning(
                    "Discovered %d of %d publicly reported songs; Suno may limit "
                    "anonymous views of long profiles.",
                    count_now,
                    target_songs,
                )
            logger.info(
                "Browser discovery complete: %d tracks%s",
                count_now,
                f" (profile claims {target_songs})" if target_songs else "",
            )
        finally:
            browser.close()

    return list(tracks.values())


async def discover_profile_tracks_playwright(
    profile_url: str,
    handle: str,
    headless: bool = False,
    max_idle: int = 8,
    max_iters: int = 400,
) -> list[TrackInfo]:
    """Open a browser and collect all public song links from a profile.

    Runs the blocking Playwright loop in a worker thread so callers stay async.
    """

    try:
        import playwright  # noqa: F401
        from playwright.sync_api import Error as PlaywrightError
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise BrowserDependencyMissing(
            "Browser fallback requires Playwright. Install the optional browser extra:"
            " pip install 'trackie-assistant[browser]' && python -m playwright install chromium"
            " (or pass --no-browser for HTTP-only discovery)."
        ) from exc

    try:
        return await asyncio.to_thread(
            _discover_sync, profile_url, handle, headless, max_idle, max_iters
        )
    except PlaywrightError as exc:
        # A Python-only [browser] install does not download the Chromium binary.
        # Translate only this known launch error; preserve unrelated browser
        # failures for debugging rather than misdiagnosing them as missing files.
        detail = str(exc)
        if "BrowserType.launch:" not in detail or "Executable doesn't exist at" not in detail:
            raise
        raise ChromiumExecutableMissing(
            "Playwright Chromium is not installed. Run:"
            " python -m playwright install chromium"
            " (or pass --no-browser for HTTP-only profile discovery)."
        ) from None


async def discover_profile_tracks(
    profile_url: str,
    handle: str,
    use_browser_fallback: bool = True,
    headless: bool = True,
    max_idle: int = 8,
    max_iters: int = 400,
    feed_timeout: float = 30.0,
    feed_max_pages: int = 50,
    feed_rate_limit_sec: float = 1.5,
) -> list[TrackInfo]:
    """Discover all public tracks for a profile.

    Tries the anonymous studio feed API first (fast, no browser needed).
    Falls back to Playwright-based discovery if the feed API fails or returns
    no tracks and ``use_browser_fallback`` is True.

    Args:
        profile_url: Full profile URL (e.g., "https://suno.com/@handle").
        handle: Profile handle (with or without leading '@').
        use_browser_fallback: Whether to fall back to Playwright on feed failure.
        headless: Run Playwright in headless mode (fallback only).
        max_idle: Max idle scroll iterations before stopping (fallback only).
        max_iters: Max scroll iterations (fallback only).
        feed_timeout: Feed API request timeout in seconds.
        feed_max_pages: Maximum feed pages to fetch.
        feed_rate_limit_sec: Minimum seconds between feed API requests.

    Returns:
        List of TrackInfo for all discovered public clips.
    """
    # Log a safe label; callers may provide a handle with query-like text.
    handle_label = _log_source(handle.lstrip("@"))
    # Try feed API first
    try:
        tracks = await discover_profile_tracks_feed(
            handle=handle,
            timeout=feed_timeout,
            max_pages=feed_max_pages,
            rate_limit_sec=feed_rate_limit_sec,
        )
        if tracks:
            logger.info("Feed discovery succeeded for @%s: %d tracks", handle_label, len(tracks))
            return tracks
        logger.info("Feed discovery returned no tracks for @%s", handle_label)
    except Exception as exc:  # pragma: no cover - network variance
        logger.warning("Feed discovery failed for @%s (%s)", handle_label, type(exc).__name__)

    # Fall back to Playwright
    if use_browser_fallback:
        logger.info("Falling back to Playwright discovery for @%s", handle_label)
        return await discover_profile_tracks_playwright(
            profile_url=profile_url,
            handle=handle,
            headless=headless,
            max_idle=max_idle,
            max_iters=max_iters,
        )

    logger.warning("Feed discovery failed and browser fallback disabled for @%s", handle_label)
    return []
