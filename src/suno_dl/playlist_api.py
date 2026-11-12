"""Narrow anonymous read-only transport for public playlist membership.

This transport permits one exact HTTPS GET endpoint on Suno's public playlist
metadata service. Public profile enumeration has its own narrowly scoped
read-only transport in profile_playlists.py. It never
forwards cookies, credentials, redirects, or requests to another API path.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from .fetcher import USER_AGENT
from .ratelimit import HostRateLimiter
from .urls import UUID_RE

PUBLIC_PLAYLIST_API_HOST = "studio-api.prod.suno.com"
PUBLIC_PLAYLIST_API_ORIGIN = f"https://{PUBLIC_PLAYLIST_API_HOST}"
PUBLIC_PLAYLIST_API_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PUBLIC_PLAYLIST_API_MAX_PAGES = 100


class PublicPlaylistApiError(RuntimeError):
    """A public playlist API response could not be safely or fully read."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON objects, including nested clip/producer fields."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublicPlaylistApiError("public playlist API returned duplicate JSON fields")
        result[key] = value
    return result


def _invalid_json_constant(_value: str) -> None:
    raise ValueError("non-JSON numeric constant")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def public_playlist_api_url(playlist_id: str, page: int) -> str:
    """Build and revalidate the one permitted anonymous playlist GET URL."""
    if not isinstance(playlist_id, str) or not UUID_RE.fullmatch(playlist_id):
        raise PublicPlaylistApiError("invalid public playlist UUID")
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or not 1 <= page <= PUBLIC_PLAYLIST_API_MAX_PAGES
    ):
        raise PublicPlaylistApiError("public playlist page is outside the request budget")

    canonical_id = playlist_id.lower()
    url = f"{PUBLIC_PLAYLIST_API_ORIGIN}/api/playlist/{canonical_id}/?{urlencode({'page': page})}"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise PublicPlaylistApiError("invalid public playlist API URL") from exc

    if (
        parts.scheme != "https"
        or parts.hostname != PUBLIC_PLAYLIST_API_HOST
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.path != f"/api/playlist/{canonical_id}/"
        or parts.fragment
    ):
        raise PublicPlaylistApiError("public playlist API URL failed its exact-host check")
    return url


async def _read_capped(response: httpx.Response) -> bytes:
    """Stream a decoded response while enforcing a strict byte ceiling."""
    raw_length = response.headers.get("content-length")
    if raw_length is not None:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise PublicPlaylistApiError(
                "public playlist API returned an invalid content length"
            ) from exc
        if declared_length < 0 or declared_length > PUBLIC_PLAYLIST_API_MAX_RESPONSE_BYTES:
            raise PublicPlaylistApiError("public playlist API response exceeds the size limit")

    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes(64 * 1024):
        size += len(chunk)
        if size > PUBLIC_PLAYLIST_API_MAX_RESPONSE_BYTES:
            raise PublicPlaylistApiError("public playlist API response exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def fetch_public_playlist_api_page(
    playlist_id: str,
    page: int,
    *,
    timeout_sec: float,
    rate_limiter: HostRateLimiter,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> dict[str, Any]:
    """Fetch one page from the exact anonymous endpoint, with no reusable jar.

    A fresh client is created for each page so a Set-Cookie response can never
    be replayed on the next page. Environment proxies and netrc are disabled;
    there is no auth provider, cookies are empty, redirects are disabled, and
    only a GET to the exact allow-listed host/path is constructed.
    """
    url = public_playlist_api_url(playlist_id, page)
    await rate_limiter.acquire(url)

    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    transport = transport_factory() if transport_factory is not None else None
    async with httpx.AsyncClient(
        timeout=timeout_sec,
        headers=headers,
        auth=None,
        cookies={},
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        request = client.build_request("GET", url)
        if str(request.url) != url:
            raise PublicPlaylistApiError(
                "public playlist API request URL failed its exact-host check"
            )
        if request.method != "GET":
            raise PublicPlaylistApiError("public playlist API method is not GET")
        if "cookie" in request.headers or "authorization" in request.headers:
            raise PublicPlaylistApiError(
                "public playlist API request unexpectedly contains credentials"
            )

        response = await client.send(request, stream=True)
        try:
            if response.is_redirect:
                raise PublicPlaylistApiError("public playlist API redirects are not permitted")
            if response.status_code != 200:
                raise PublicPlaylistApiError(
                    f"public playlist API returned HTTP {response.status_code}"
                )
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise PublicPlaylistApiError("public playlist API did not return JSON")
            body = await _read_capped(response)
        finally:
            await response.aclose()

    try:
        payload = json.loads(
            body,
            object_pairs_hook=_unique_json_object,
            parse_constant=_invalid_json_constant,
            parse_float=_finite_json_float,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PublicPlaylistApiError("public playlist API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PublicPlaylistApiError("public playlist API returned an unexpected JSON shape")
    return payload
