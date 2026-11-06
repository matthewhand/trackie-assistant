"""HTTP fetching with retries, timeouts, and configurable headers."""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .errors import BlockedError, FetchError
from .ratelimit import HostRateLimiter
from .urls import ALLOWED_MEDIA_HOSTS, redact_url

logger = logging.getLogger(__name__)

# Honest, identifiable default: this is an archival CLI, not a browser. Sites
# overwhelmingly gate on request rate, not UA string, so advertising what we
# are is both polite and accurate. Override with SUNO_USER_AGENT if needed.
USER_AGENT = "suno-download-cli/1.0 (+https://github.com/matthewhand/suno-download-cli)"

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchBlockedError(BlockedError):
    """Raised on 401/403 — the remote is blocking the request; do not retry."""


# A server that says "come back in an hour" is telling us something we should
# obey, but a bulk archive run must not stall for that long: cap the wait and
# let the remaining attempts decide.
MAX_RETRY_AFTER_SEC = 60.0

# CDN signed URLs routinely bounce twice; five is generous without letting a
# misconfigured (or hostile) loop spin.
MAX_REDIRECTS = 5

# A page (HTML/RSC/JSON feed) is metadata, not a payload: refuse anything larger
# instead of buffering it. Real Suno pages are a few MiB at most, and the cap is
# what stops a hostile or hijacked endpoint from exhausting memory or disk (#157).
MAX_RESPONSE_BYTES = 12 * 1024 * 1024

# Header names that describe a body we deliberately did not keep.
_BODY_HEADERS = frozenset({"content-length", "content-encoding", "transfer-encoding"})


def _retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """Delay requested by a ``Retry-After`` header, clamped to [0, 60].

    Accepts both RFC 9110 forms — delay-seconds and an HTTP-date — because a
    429 without honouring either waits 0.5 s and gets rate-limited again.
    Returns None when absent or unparseable, so jittered backoff applies.
    """
    if not headers:
        return None
    raw = (headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SEC)


def _redirect_allowed(host: str) -> bool:
    """True if a redirect target host may be followed (same allow-list as media)."""
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in ALLOWED_MEDIA_HOSTS)


def _describe(resp: httpx.Response) -> str:
    """Redacted URL for a response, even one without an attached request."""
    try:
        return redact_url(str(resp.url))
    except RuntimeError:
        return "<response>"


async def read_capped(resp: httpx.Response) -> bytes:
    """Read at most ``MAX_RESPONSE_BYTES`` of a streamed response.

    Bodies are read in chunks so an oversized response aborts the transfer
    instead of being allocated first: this is the size limit for HTML/RSC pages
    and JSON feeds (#157). The response must have been opened in streaming mode.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes(64 * 1024):
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise FetchError(f"response from {_describe(resp)} exceeds {MAX_RESPONSE_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


class Fetcher:
    """Shared async HTTP client with bounded retries and polite delays."""

    def __init__(
        self,
        timeout_sec: float,
        max_retries: int,
        rate_limit_sec: float = 0.0,
        user_agent: str | None = None,
    ) -> None:
        self.timeout = timeout_sec
        self.max_retries = max_retries
        self.rate_limiter = HostRateLimiter(rate_limit_sec)
        self.user_agent = user_agent or USER_AGENT
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Fetcher:
        headers = {**DEFAULT_HEADERS, "User-Agent": self.user_agent}
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers=headers,
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Fetcher used outside async context manager")
        return self._client

    async def _request(self, method: str, url: str) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 2):  # retries + initial try
            try:
                await self.rate_limiter.acquire(url)
                async with self.client.stream(method, url) as resp:
                    if resp.status_code in (401, 403):
                        raise FetchBlockedError(
                            f"HTTP {resp.status_code} for {redact_url(url)} (blocked)"
                        )
                    if resp.status_code == 404:
                        return await self._buffer(resp)  # caller decides; 404 is terminal
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}",
                            request=resp.request,
                            response=self._headers_only(resp),
                        )
                    if resp.is_redirect:
                        return await self._follow_redirects(resp, method, url)
                    # Plain success (or any other terminal status): return it.
                    return await self._buffer(resp)
            except FetchBlockedError:
                raise
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt > self.max_retries:
                    break
                hint = _retry_after_seconds(
                    getattr(getattr(exc, "response", None), "headers", None)
                )
                if hint is not None:
                    delay = hint
                else:
                    backoff = min(0.5 * 2 ** (attempt - 1), 8.0)
                    delay = backoff + random.random() * 0.3  # noqa: S311 — jitter, not crypto
                logger.debug(
                    "Retry %d/%d for %s in %.1fs (%s)",
                    attempt,
                    self.max_retries,
                    redact_url(url),
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
        raise FetchError(
            f"Failed after {self.max_retries + 1} attempts: {redact_url(url)}"
        ) from last_exc

    @staticmethod
    def _headers_only(resp: httpx.Response) -> httpx.Response:
        """Status + headers with no body, for retry/status error handling."""
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in _BODY_HEADERS}
        return httpx.Response(resp.status_code, headers=headers, request=resp.request)

    async def _buffer(self, resp: httpx.Response) -> httpx.Response:
        """Buffer decoded bytes without retaining their original wire encoding."""
        # aiter_bytes() already decodes Content-Encoding; retaining that
        # header makes httpx decode the buffered content a second time.
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in _BODY_HEADERS}
        return httpx.Response(
            resp.status_code,
            headers=headers,
            content=await read_capped(resp),
            request=resp.request,
        )

    async def _follow_redirects(
        self, resp: httpx.Response, method: str, url: str
    ) -> httpx.Response:
        """Follow an allow-listed https redirect chain, one vetted hop at a time.

        Only redirects that stay on allow-listed https hosts are followed: a
        signed CDN URL bouncing to an internal or non-https address would
        otherwise bypass the SSRF allow-list. Chains are followed rather than
        just the first hop — CDNs commonly bounce twice, and stopping after one
        turned a legitimate download into ``FetchError``.
        """
        for _hop in range(MAX_REDIRECTS):
            location = resp.headers.get("location", "")
            target = urlsplit(location)
            if target.scheme != "https" or not _redirect_allowed(target.hostname or ""):
                logger.warning("Refusing redirect to %s", redact_url(location))
                raise FetchBlockedError(f"redirect to non-allow-listed host for {redact_url(url)}")
            async with self.client.stream(method, location) as nxt:
                if nxt.status_code in (401, 403):
                    raise FetchBlockedError(
                        f"HTTP {nxt.status_code} for {redact_url(location)} (blocked)"
                    )
                if nxt.status_code == 404:
                    return await self._buffer(nxt)  # same terminal contract as a direct 404
                if not nxt.is_redirect:
                    nxt.raise_for_status()
                    return await self._buffer(nxt)
                resp = nxt  # headers are complete before the body is read
        raise FetchBlockedError(f"too many redirects for {redact_url(url)}")

    async def fetch_html(self, url: str) -> str:
        resp = await self._request("GET", url)
        return resp.text

    async def fetch_song_page(self, clip_id: str) -> str:
        """Fetch identity evidence from one exact HTTPS song URL only.

        Redirects and non-200 responses cannot supply producer evidence.
        Backpressure stops the caller instead of moving to another endpoint.
        """
        from .urls import UUID_RE, song_page_url

        if not isinstance(clip_id, str) or not UUID_RE.fullmatch(clip_id):
            raise FetchError("invalid canonical song UUID")
        url = song_page_url(clip_id.lower())
        await self.rate_limiter.acquire(url)
        async with self.client.stream("GET", url, follow_redirects=False) as resp:
            if resp.status_code != 200 or str(resp.url) != url or resp.history:
                raise FetchError(f"canonical song evidence requires HTTP 200 at {url}")
            chunks = []
            size = 0
            async for chunk in resp.aiter_bytes(64 * 1024):
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise FetchError("canonical song evidence exceeds the page bound")
                chunks.append(chunk)
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeError as exc:
            raise FetchError("canonical song evidence is not UTF-8") from exc
