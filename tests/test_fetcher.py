"""Tests for the HTTP fetcher: retries, backoff, and blocked handling.

Uses httpx MockTransport so no network is touched.
"""

from __future__ import annotations

import time

import httpx
import pytest

from suno_dl.errors import BlockedError, FetchError
from suno_dl.fetcher import USER_AGENT, Fetcher


def _fetcher_with(handler) -> Fetcher:
    f = Fetcher(timeout_sec=1.0, max_retries=2)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return f


async def test_fetch_html_success() -> None:
    f = _fetcher_with(lambda req: httpx.Response(200, text="<html>ok</html>"))
    assert await f.fetch_html("https://suno.com/@x") == "<html>ok</html>"
    await f.__aexit__()


async def test_blocked_401_not_retried() -> None:
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(401)

    f = _fetcher_with(handler)
    with pytest.raises(BlockedError):
        await f.fetch_html("https://suno.com/@x")
    assert calls["n"] == 1  # 401 must not be retried
    await f.__aexit__()


async def test_blocked_403_not_retried() -> None:
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(403)

    f = _fetcher_with(handler)
    with pytest.raises(BlockedError):
        await f.fetch_html("https://suno.com/@x")
    assert calls["n"] == 1
    await f.__aexit__()


async def test_404_is_terminal_and_not_retried() -> None:
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(404)

    f = _fetcher_with(handler)
    resp = await f._request("GET", "https://suno.com/@x")
    assert resp.status_code == 404
    assert calls["n"] == 1
    await f.__aexit__()


async def test_5xx_retried_then_fails() -> None:
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503)

    f = _fetcher_with(handler)
    with pytest.raises(RuntimeError):
        await f.fetch_html("https://suno.com/@x")
    assert calls["n"] == 3  # initial + 2 retries
    await f.__aexit__()


async def test_5xx_then_success() -> None:
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, text="ok")

    f = _fetcher_with(handler)
    assert await f.fetch_html("https://suno.com/@x") == "ok"
    assert calls["n"] == 2
    await f.__aexit__()


async def test_429_retry_after_is_honoured() -> None:
    """A 429's Retry-After is obeyed instead of the default 0.5 s backoff."""
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, text="ok")

    f = _fetcher_with(handler)
    start = time.monotonic()
    assert await f.fetch_html("https://suno.com/@x") == "ok"
    assert time.monotonic() - start >= 1.0
    assert calls["n"] == 2
    await f.__aexit__()


async def test_retry_after_parsing_and_clamping() -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from suno_dl.fetcher import MAX_RETRY_AFTER_SEC, _retry_after_seconds

    assert _retry_after_seconds(None) is None
    assert _retry_after_seconds({}) is None
    assert _retry_after_seconds({"Retry-After": "2"}) == 2.0
    assert _retry_after_seconds({"Retry-After": " 0 "}) == 0.0
    assert _retry_after_seconds({"Retry-After": "86400"}) == MAX_RETRY_AFTER_SEC
    assert _retry_after_seconds({"Retry-After": "-5"}) == 0.0
    # Unparseable values fall back to jittered backoff rather than raising.
    for junk in ("soon", "nan", "inf", ""):
        assert _retry_after_seconds({"Retry-After": junk}) is None
    past = format_datetime(datetime.now(UTC) - timedelta(seconds=30), usegmt=True)
    assert _retry_after_seconds({"Retry-After": past}) == 0.0
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=5), usegmt=True)
    got = _retry_after_seconds({"Retry-After": future})
    assert got is not None and 3.0 <= got <= 6.0


async def test_requires_context_manager() -> None:
    f = Fetcher(timeout_sec=1.0, max_retries=0)
    with pytest.raises(RuntimeError, match="outside async context"):
        _ = f.client


def _fetcher_no_autofollow(handler) -> Fetcher:
    """Like the real __aenter__: client does NOT auto-follow redirects."""
    f = Fetcher(timeout_sec=1.0, max_retries=1)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return f


async def test_redirect_to_internal_host_is_refused() -> None:
    """An allow-listed URL that 302s to a link-local address must not be followed."""
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        raise AssertionError("redirect target must never be requested")

    f = _fetcher_no_autofollow(handler)
    with pytest.raises(BlockedError, match="non-allow-listed host"):
        await f.fetch_html("https://cdn1.suno.ai/x.m4a?sig=SECRET")
    assert calls["n"] == 1
    await f.__aexit__()


async def test_redirect_to_same_allowed_host_is_followed() -> None:
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "https://cdn2.suno.com/moved"})
        return httpx.Response(200, text="after-redirect")

    f = _fetcher_no_autofollow(handler)
    assert await f.fetch_html("https://suno.com/@x") == "after-redirect"
    assert calls["n"] == 2
    await f.__aexit__()


async def test_multi_hop_redirect_chain_is_followed() -> None:
    """CDNs bounce twice; stopping after one hop failed a legitimate download."""
    calls: list[str] = []

    def handler(req):
        calls.append(str(req.url))
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://cdn1.suno.ai/b"})
        if len(calls) == 2:
            return httpx.Response(302, headers={"location": "https://cdn2.suno.com/c"})
        return httpx.Response(200, text="final body")

    f = _fetcher_no_autofollow(handler)
    assert await f.fetch_html("https://suno.com/@x") == "final body"
    assert calls == ["https://suno.com/@x", "https://cdn1.suno.ai/b", "https://cdn2.suno.com/c"]
    await f.__aexit__()


async def test_redirect_loop_is_bounded_and_reported() -> None:
    from suno_dl.fetcher import MAX_REDIRECTS

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(302, headers={"location": "https://cdn1.suno.ai/again"})

    f = _fetcher_no_autofollow(handler)
    with pytest.raises(BlockedError, match="too many redirects"):
        await f.fetch_html("https://suno.com/@x")
    assert calls["n"] == MAX_REDIRECTS + 1  # initial request, then every hop
    await f.__aexit__()


async def test_second_hop_to_internal_host_is_still_refused() -> None:
    """Following a chain must not weaken the SSRF check on later hops."""
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "https://cdn1.suno.ai/b"})
        if calls["n"] == 2:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        raise AssertionError("internal address must never be requested")

    f = _fetcher_no_autofollow(handler)
    with pytest.raises(BlockedError, match="non-allow-listed host"):
        await f.fetch_html("https://suno.com/@x")
    assert calls["n"] == 2
    await f.__aexit__()


async def test_redirect_to_non_https_is_refused() -> None:
    def handler(req):
        return httpx.Response(302, headers={"location": "http://suno.com/insecure"})

    f = _fetcher_no_autofollow(handler)
    with pytest.raises(BlockedError):
        await f.fetch_html("https://suno.com/@x")
    await f.__aexit__()


async def test_blocked_error_message_redacts_signed_query() -> None:
    def handler(req):
        return httpx.Response(403)

    f = _fetcher_with(handler)
    with pytest.raises(BlockedError) as excinfo:
        await f.fetch_html("https://cdn1.suno.ai/x.m4a?sig=TOPSECRET")
    assert "TOPSECRET" not in str(excinfo.value)
    assert "redacted" in str(excinfo.value)
    await f.__aexit__()


async def test_retry_exhaustion_raises_typed_redacted_error() -> None:
    def handler(req):
        return httpx.Response(503)

    f = _fetcher_with(handler)
    with pytest.raises(FetchError) as excinfo:
        await f.fetch_html("https://cdn1.suno.ai/x.m4a?sig=TOPSECRET")
    assert isinstance(excinfo.value, RuntimeError)  # back-compat for callers
    assert "TOPSECRET" not in str(excinfo.value)
    await f.__aexit__()


def test_default_user_agent_tracks_release() -> None:
    assert USER_AGENT.startswith("suno-download-cli/")
    assert "0.1 (" not in USER_AGENT


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("status", [200, 404])
async def test_buffered_compressed_response_is_decoded_once(encoding: str, status: int) -> None:
    """Buffered responses expose decoded content without stale wire headers."""
    import gzip
    import zlib

    payload = b"<html>public playlist</html>"
    compressed = gzip.compress(payload) if encoding == "gzip" else zlib.compress(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={
                "content-encoding": encoding,
                "content-length": str(len(compressed)),
                "content-type": "text/html; charset=utf-8",
            },
            stream=httpx.ByteStream(compressed),
            request=request,
        )

    fetcher = _fetcher_with(handler)
    try:
        response = await fetcher._request("GET", "https://suno.com/playlist/example")
        assert response.status_code == status
        assert response.content == payload
        assert response.text == payload.decode()
        assert "content-encoding" not in response.headers
        assert response.headers["content-length"] == str(len(payload))
        assert response.headers["content-type"] == "text/html; charset=utf-8"
    finally:
        await fetcher.__aexit__()
