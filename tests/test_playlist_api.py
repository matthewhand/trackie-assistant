from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import suno_dl.playlists as playlists
from suno_dl.playlist_api import (
    PUBLIC_PLAYLIST_API_HOST,
    PUBLIC_PLAYLIST_API_MAX_PAGES,
    PUBLIC_PLAYLIST_API_MAX_RESPONSE_BYTES,
    PublicPlaylistApiError,
    fetch_public_playlist_api_page,
    public_playlist_api_url,
)
from suno_dl.playlists import PlaylistRef, fetch_playlist_membership
from suno_dl.ratelimit import HostRateLimiter

FIXTURES = Path(__file__).parent / "fixtures" / "public_playlist_api"
FIVE_IDS = {
    "3959b562-b709-4a75-8d23-b0780048aea4",
    "b12323c2-685e-4e30-8181-c70d876e7b16",
    "641e0f5c-0c97-449c-80b8-6baf7cf36191",
    "5e648da0-9a17-4104-bd3d-cdd75b3ebb26",
    "d3c8fff7-34aa-4561-8c3f-5712be43626a",
}


def _uuid(n: int) -> str:
    return f"{n:08x}-1111-4111-8111-111111111111"


def _entry(clip_id: str, **overrides: object) -> dict:
    clip = {
        "id": clip_id,
        "title": f"Track {clip_id[:8]}",
        "entity_type": "song_schema",
        "is_public": True,
        "is_hidden": False,
        "is_trashed": False,
    }
    clip.update(overrides)
    return {"clip": clip}


def _payload(playlist_id: str, page: int, total: int, ids: list[str], **overrides: object) -> dict:
    data = {
        "id": playlist_id,
        "name": "Fixture playlist",
        "is_public": True,
        "is_hidden": False,
        "is_trashed": False,
        "current_page": page,
        "num_total_results": total,
        "playlist_clips": [_entry(clip_id) for clip_id in ids],
    }
    data.update(overrides)
    return data


async def _membership(
    playlist: PlaylistRef, pages: dict[int, dict], html: str = "<html>Sign in</html>"
):
    calls: list[int] = []

    async def fetch_html(url: str) -> str:
        assert url == playlist.url
        return html

    async def fetch_api(ref: PlaylistRef, page: int) -> dict:
        assert ref.id == playlist.id
        calls.append(page)
        return pages[page]

    return await fetch_playlist_membership(fetch_html, playlist, fetch_api), calls


async def test_five_public_fixtures_are_complete_ordered_and_metadata_only() -> None:
    files = sorted(FIXTURES.glob("*.json"))
    assert {path.stem for path in files} == FIVE_IDS
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        playlist = PlaylistRef(payload["id"], payload["name"])
        result, calls = await _membership(playlist, {1: payload})
        expected = tuple(item["clip"]["id"] for item in payload["playlist_clips"])
        assert result.resolved, result.reason
        assert result.ids == expected
        assert len(result.ids) == payload["num_total_results"]
        assert calls == [1]
        assert all(t.is_public is True for t in result.tracks)
        assert all(
            t.audio_url is None and t.cover_url is None and not t.media_urls for t in result.tracks
        )


async def test_html_count_mismatch_and_explicit_zero_use_api_verification() -> None:
    payload = json.loads((FIXTURES / "3959b562-b709-4a75-8d23-b0780048aea4.json").read_text())
    first_id = payload["playlist_clips"][0]["clip"]["id"]
    html = f'<a href="/song/{first_id}">Track</a><p>17 songs</p>'
    playlist = PlaylistRef(payload["id"], payload["name"], track_count=17)
    result, calls = await _membership(playlist, {1: payload}, html)
    assert result.resolved and len(result.ids) == 17 and calls == [1]

    empty_id = _uuid(9)
    empty = PlaylistRef(empty_id, "Empty")
    empty_payload = _payload(empty_id, 1, 0, [])
    result, calls = await _membership(empty, {1: empty_payload}, "<p>0 songs</p>")
    assert result.resolved and result.ids == () and calls == [1]


async def test_untrusted_html_zero_does_not_override_verified_api() -> None:
    playlist_id = _uuid(1)
    playlist = PlaylistRef(playlist_id, "Conflicting zero")
    payload = _payload(playlist_id, 1, 1, [_uuid(2)])
    result, calls = await _membership(playlist, {1: payload}, "<p>0 songs</p>")
    assert result.resolved
    assert result.ids == (_uuid(2),)
    assert calls == [1]


async def test_api_paginates_and_requires_exact_total() -> None:
    playlist_id = _uuid(1)
    playlist = PlaylistRef(playlist_id, "Paged", track_count=3)
    pages = {
        1: _payload(playlist_id, 1, 3, [_uuid(2), _uuid(3)]),
        2: _payload(playlist_id, 2, 3, [_uuid(4)]),
    }
    result, calls = await _membership(playlist, pages)
    assert result.resolved and result.ids == (_uuid(2), _uuid(3), _uuid(4))
    assert calls == [1, 2]
    pages[2] = _payload(playlist_id, 2, 3, [])
    result, calls = await _membership(playlist, pages)
    assert not result.resolved and calls == [1, 2]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("is_public", False),
        ("is_public", None),
        ("is_hidden", True),
        ("is_hidden", None),
        ("is_trashed", True),
        ("is_trashed", None),
    ],
)
async def test_api_rejects_nonpublic_playlist(field: str, value: object) -> None:
    playlist_id = _uuid(1)
    playlist = PlaylistRef(playlist_id, "Private", track_count=1)
    payload = _payload(playlist_id, 1, 1, [_uuid(2)], **{field: value})
    result, _ = await _membership(playlist, {1: payload})
    assert not result.resolved


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("is_public", False),
        ("is_public", None),
        ("is_hidden", True),
        ("is_hidden", None),
        ("is_trashed", True),
        ("is_trashed", None),
        ("entity_type", "other"),
    ],
)
async def test_api_rejects_nonpublic_or_unsupported_clip(field: str, value: object) -> None:
    playlist_id = _uuid(1)
    playlist = PlaylistRef(playlist_id, "Hidden clip", track_count=1)
    payload = _payload(playlist_id, 1, 1, [])
    payload["playlist_clips"] = [_entry(_uuid(2), **{field: value})]
    result, _ = await _membership(playlist, {1: payload})
    assert not result.resolved


async def test_api_rejects_identity_count_page_duplicate_and_invalid_id() -> None:
    playlist_id = _uuid(1)
    playlist = PlaylistRef(playlist_id, "Invalid", track_count=2)
    cases = [
        _payload(_uuid(9), 1, 2, [_uuid(2), _uuid(3)]),
        _payload(playlist_id, 1, 1, [_uuid(2)]),
        _payload(playlist_id, 2, 2, [_uuid(2), _uuid(3)]),
        _payload(playlist_id, 1, 2, [_uuid(2), _uuid(2)]),
        _payload(playlist_id, 1, 2, ["not-a-uuid", _uuid(3)]),
    ]
    for payload in cases:
        result, _ = await _membership(playlist, {1: payload})
        assert not result.resolved


async def test_api_rejects_total_changes_and_exhausted_page_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playlist_id = _uuid(1)
    playlist = PlaylistRef(playlist_id, "Paged", track_count=3)
    changed = {
        1: _payload(playlist_id, 1, 3, [_uuid(2), _uuid(3)]),
        2: _payload(playlist_id, 2, 4, [_uuid(4)]),
    }
    result, _ = await _membership(playlist, changed)
    assert not result.resolved

    monkeypatch.setattr(playlists, "PUBLIC_PLAYLIST_API_MAX_PAGES", 2)
    limited = {
        1: _payload(playlist_id, 1, 3, [_uuid(2)]),
        2: _payload(playlist_id, 2, 3, [_uuid(3)]),
    }
    result, calls = await _membership(playlist, limited)
    assert not result.resolved and calls == [1, 2]


def test_api_url_rejects_untrusted_ids_and_unbounded_pages() -> None:
    assert public_playlist_api_url(_uuid(1), 2) == (
        f"https://{PUBLIC_PLAYLIST_API_HOST}/api/playlist/{_uuid(1)}/?page=2"
    )
    for bad_id in ("not-a-uuid", ""):
        with pytest.raises(PublicPlaylistApiError):
            public_playlist_api_url(bad_id, 1)
    for bad_page in (0, -1, True, 1.5, PUBLIC_PLAYLIST_API_MAX_PAGES + 1):
        with pytest.raises(PublicPlaylistApiError):
            public_playlist_api_url(_uuid(1), bad_page)  # type: ignore[arg-type]


async def test_http_is_exact_get_without_cookies_or_authorization() -> None:
    seen: list[httpx.Request] = []

    def transport_factory() -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "set-cookie": "session=not-reused"},
                json={"ok": True},
            )

        return httpx.MockTransport(handler)

    for page in (1, 2):
        await fetch_public_playlist_api_page(
            _uuid(1),
            page,
            timeout_sec=1,
            rate_limiter=HostRateLimiter(0),
            transport_factory=transport_factory,
        )
    assert len(seen) == 2
    for page, request in enumerate(seen, 1):
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.host == PUBLIC_PLAYLIST_API_HOST
        assert request.url.path == f"/api/playlist/{_uuid(1)}/"
        assert dict(request.url.params) == {"page": str(page)}
        assert "cookie" not in request.headers and "authorization" not in request.headers


async def test_http_does_not_follow_redirects() -> None:
    seen: list[httpx.Request] = []

    def factory() -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(302, headers={"location": "https://untrusted.example/next"})

        return httpx.MockTransport(handler)

    with pytest.raises(PublicPlaylistApiError, match="redirects"):
        await fetch_public_playlist_api_page(
            _uuid(1), 1, timeout_sec=1, rate_limiter=HostRateLimiter(0), transport_factory=factory
        )
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("status", "content_type", "body", "reason"),
    [
        (403, "application/json", b"{}", "HTTP 403"),
        (200, "text/html", b"{}", "did not return JSON"),
        (200, "application/json", b"bad-json", "invalid JSON"),
    ],
)
async def test_http_rejects_bad_status_content_type_and_json(
    status: int, content_type: str, body: bytes, reason: str
) -> None:
    def factory() -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda request: httpx.Response(
                status, headers={"content-type": content_type}, content=body
            )
        )

    with pytest.raises(PublicPlaylistApiError, match=reason):
        await fetch_public_playlist_api_page(
            _uuid(1), 1, timeout_sec=1, rate_limiter=HostRateLimiter(0), transport_factory=factory
        )


async def test_http_rejects_oversized_response() -> None:
    body = b"x" * (PUBLIC_PLAYLIST_API_MAX_RESPONSE_BYTES + 1)

    def factory() -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, content=body
            )
        )

    with pytest.raises(PublicPlaylistApiError, match="size limit"):
        await fetch_public_playlist_api_page(
            _uuid(1), 1, timeout_sec=1, rate_limiter=HostRateLimiter(0), transport_factory=factory
        )


@pytest.mark.parametrize(
    "html_count_suffix",
    ["", "<p>1 songs</p>"],
)
async def test_profile_count_rejects_partial_html_even_if_html_self_consistent(
    html_count_suffix: str,
) -> None:
    """A known feed count must not be overridden by partial, convincing HTML."""
    playlist_id = _uuid(1)
    profile = PlaylistRef(playlist_id, "Partial HTML", track_count=3)
    html = f'<a href="/song/{_uuid(2)}">one</a>{html_count_suffix}'
    verified = _payload(playlist_id, 1, 3, [_uuid(2), _uuid(3), _uuid(4)])

    result, calls = await _membership(profile, {1: verified}, html)
    assert calls == [1], "partial HTML bypassed the verified API fallback"
    assert result.resolved
    assert result.ids == (_uuid(2), _uuid(3), _uuid(4))


async def test_partial_html_without_api_cannot_complete_known_profile_count() -> None:
    """Without an API, partial HTML must not create a complete checkpoint."""
    profile = PlaylistRef(_uuid(1), "Partial", track_count=3)

    async def fetch_html(url: str) -> str:
        return f'<a href="/song/{_uuid(2)}">one</a>'

    result = await fetch_playlist_membership(fetch_html, profile)
    assert not result.resolved
    assert result.ids == ()


async def test_html_without_verifiable_count_remains_unresolved_without_api() -> None:
    """A DOM anchor alone proves existence, not complete membership."""
    profile = PlaylistRef(_uuid(1), "Unknown size")

    async def fetch_html(url: str) -> str:
        return f'<a href="/song/{_uuid(2)}">one</a>'

    result = await fetch_playlist_membership(fetch_html, profile)
    assert not result.resolved
    assert result.ids == ()


async def test_html_without_verifiable_count_uses_api_as_authority() -> None:
    """A complete explicitly public API count can resolve count-less HTML."""
    playlist_id = _uuid(1)
    profile = PlaylistRef(playlist_id, "Unknown size")
    payload = _payload(playlist_id, 1, 2, [_uuid(2), _uuid(3)])
    html = f'<a href="/song/{_uuid(2)}">one</a>'

    result, calls = await _membership(profile, {1: payload}, html)
    assert calls == [1]
    assert result.resolved
    assert result.ids == (_uuid(2), _uuid(3))


async def test_profile_count_verifies_html_without_page_count() -> None:
    """A trusted feed count is sufficient; avoid unnecessary API requests."""
    playlist_id = _uuid(1)
    profile = PlaylistRef(playlist_id, "Known size", track_count=2)
    html = f'<a href="/song/{_uuid(2)}">two</a><a href="/song/{_uuid(3)}">three</a>'

    async def fetch_html(url: str) -> str:
        return html

    async def should_not_call_api(playlist: PlaylistRef, page: int) -> dict:
        raise AssertionError("complete, feed-verified HTML must not fetch API")

    result = await fetch_playlist_membership(fetch_html, profile, should_not_call_api)
    assert result.resolved
    assert result.ids == (_uuid(2), _uuid(3))


async def test_profile_count_disagrees_with_explicit_empty_html_without_api() -> None:
    profile = PlaylistRef(_uuid(1), "Not empty", track_count=2)

    async def fetch_html(url: str) -> str:
        return "<p>0 songs</p>"

    result = await fetch_playlist_membership(fetch_html, profile)
    assert not result.resolved
    assert result.ids == ()


async def test_over_cap_html_membership_never_resolves_or_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete HTML listing above the cap must not be silently truncated."""
    monkeypatch.setattr("suno_dl.playlists.MAX_PLAYLIST_TRACKS", 3)
    playlist = PlaylistRef(_uuid(1), "Over cap", track_count=4)
    html = "".join(f'<a href="/song/{_uuid(i)}">Track</a>' for i in range(2, 6))
    html += "<p>4 songs</p>"

    async def fetch_html(url: str) -> str:
        return html

    result = await fetch_playlist_membership(fetch_html, playlist)
    assert not result.resolved
    assert result.ids == ()
    assert "cap" in (result.reason or "").lower()


async def test_unrelated_html_count_cannot_complete_membership_without_feed_count() -> None:
    """An unrelated '1 songs' text is not evidence of complete membership."""
    playlist = PlaylistRef(_uuid(1), "Unknown size")
    html = f'<div class="unrelated">1 songs</div><a href="/song/{_uuid(2)}">one</a>'

    async def fetch_html(url: str) -> str:
        return html

    result = await fetch_playlist_membership(fetch_html, playlist)
    assert not result.resolved
    assert result.ids == ()


async def test_untrusted_html_count_requires_verified_api_fallback() -> None:
    playlist = PlaylistRef(_uuid(1), "Unknown size")
    html = f'<div class="unrelated">1 songs</div><a href="/song/{_uuid(2)}">one</a>'
    payload = _payload(playlist.id, 1, 2, [_uuid(2), _uuid(3)])
    result, calls = await _membership(playlist, {1: payload}, html)
    assert calls == [1]
    assert (
        result.resolved
    )  # The exact validated public API, not unrelated page text, is authoritative.
    assert result.ids == (_uuid(2), _uuid(3))


async def test_unrelated_page_count_cannot_veto_complete_public_api_membership() -> None:
    """The verified API total, not unrelated page text, determines membership."""
    playlist_id = _uuid(1)
    playlist = PlaylistRef(playlist_id, "Verified API", track_count=2)
    html = f'<div class="unrelated">1 songs</div><a href="/song/{_uuid(2)}">one</a>'
    payload = _payload(playlist_id, 1, 2, [_uuid(2), _uuid(3)])
    result, calls = await _membership(playlist, {1: payload}, html)
    assert calls == [1]
    assert result.resolved
    assert result.ids == (_uuid(2), _uuid(3))


async def test_public_api_rechecks_built_request_url_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport-layer URL mutation cannot bypass the exact host rule."""
    original = httpx.AsyncClient.build_request
    sent = []

    def altered(self, method, url, **kwargs):
        request = original(self, method, url, **kwargs)
        request.url = httpx.URL("https://untrusted.example/other")
        return request

    def transport_factory() -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json={})

        return httpx.MockTransport(handler)

    monkeypatch.setattr(httpx.AsyncClient, "build_request", altered)
    with pytest.raises(PublicPlaylistApiError, match="exact-host"):
        await fetch_public_playlist_api_page(
            _uuid(1),
            1,
            timeout_sec=1,
            rate_limiter=HostRateLimiter(0),
            transport_factory=transport_factory,
        )
    assert not sent


@pytest.mark.parametrize("target", ["playlist", "clip"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("is_private", True),
        ("deleted", True),
        ("visibility", "private"),
        ("status", {"is_hidden": True}),
        ("hidden", "true"),
    ],
)
async def test_api_rejects_contradictory_visibility_markers(
    target: str, field: str, value: object
) -> None:
    playlist_id = _uuid(1)
    playlist = PlaylistRef(playlist_id, "Contradictory", track_count=1)
    payload = _payload(playlist_id, 1, 1, [_uuid(2)])
    obj = payload if target == "playlist" else payload["playlist_clips"][0]["clip"]
    obj[field] = value
    result, _ = await _membership(playlist, {1: payload})
    assert not result.resolved


async def test_api_membership_preserves_clip_producer_identity() -> None:
    playlist_id = _uuid(1)
    playlist = PlaylistRef(playlist_id, "Curated", track_count=1)
    payload = _payload(playlist_id, 1, 1, [])
    payload["playlist_clips"] = [
        _entry(_uuid(2), user={"display_name": "Real Producer", "handle": "realproducer"})
    ]
    result, _ = await _membership(playlist, {1: payload})
    assert result.resolved
    assert result.tracks[0].artist == "Real Producer"
    assert result.tracks[0].creator_handle == "realproducer"


@pytest.mark.parametrize("identity", [{"display_name": "Bad\u202eName"}, {}])
async def test_api_does_not_invent_missing_or_unsafe_producer(identity: dict) -> None:
    playlist_id = _uuid(1)
    payload = _payload(playlist_id, 1, 1, [])
    payload["playlist_clips"] = [_entry(_uuid(2), user=identity)]
    result, _ = await _membership(PlaylistRef(playlist_id, "Owner", track_count=1), {1: payload})
    assert result.resolved
    assert result.tracks[0].artist is None
    assert result.tracks[0].creator_handle is None


@pytest.mark.parametrize(
    "body",
    [
        b'{"id":"first","id":"second"}',
        b'{"playlist_clips":[{"clip":{"id":"first","id":"second"}}]}',
        b'{"playlist_clips":[{"clip":{"user":{"display_name":"Alice","display_name":"Bob"}}}]}',
        b'{"playlist_clips":[{"clip":{"user":{"handle":"same","handle":"same"}}}]}',
    ],
)
async def test_http_rejects_duplicate_fields_at_every_nesting_level(body: bytes) -> None:
    def factory() -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, content=body
            )
        )

    with pytest.raises(PublicPlaylistApiError, match="duplicate JSON fields"):
        await fetch_public_playlist_api_page(
            _uuid(1), 1, timeout_sec=1, rate_limiter=HostRateLimiter(0), transport_factory=factory
        )


@pytest.mark.parametrize("body", [b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}'])
async def test_http_rejects_non_json_numeric_constants(body: bytes) -> None:
    def factory() -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, content=body
            )
        )

    with pytest.raises(PublicPlaylistApiError, match="invalid JSON"):
        await fetch_public_playlist_api_page(
            _uuid(1), 1, timeout_sec=1, rate_limiter=HostRateLimiter(0), transport_factory=factory
        )


@pytest.mark.parametrize("number", ["1e9999", "-1e9999"])
async def test_http_rejects_nested_finite_syntax_overflow(number: str) -> None:
    body = ('{"outer":[{"number":' + number + "}]}").encode()

    def factory() -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, content=body
            )
        )

    with pytest.raises(PublicPlaylistApiError, match="invalid JSON"):
        await fetch_public_playlist_api_page(
            _uuid(1), 1, timeout_sec=1, rate_limiter=HostRateLimiter(0), transport_factory=factory
        )


async def test_http_accepts_valid_finite_float() -> None:
    def factory() -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, content=b'{"value":1.25e2}'
            )
        )

    payload = await fetch_public_playlist_api_page(
        _uuid(1), 1, timeout_sec=1, rate_limiter=HostRateLimiter(0), transport_factory=factory
    )
    assert payload == {"value": 125.0}
