from __future__ import annotations

import json

import httpx
import pytest

from suno_dl.playlist_api import PublicPlaylistApiError
from suno_dl.profile_playlists import (
    PUBLIC_PROFILE_FEED_URL,
    discover_cursor_playlists,
    fetch_public_profile_playlists_page,
    profile_user_id,
)
from suno_dl.ratelimit import HostRateLimiter


def uid(n: int) -> str:
    return f"{n:08x}-1111-4111-8111-111111111111"


def profile(handle: str = "example", user_id: str = uid(1)) -> str:
    data = {
        "v2Data": {
            "user_id": user_id,
            "metadata": {"user_id": user_id, "handle": handle},
            "feed": {"feed_id": "artist_profile"},
        }
    }
    text = "bf:" + json.dumps(["$", "Component", None, data]) + "\n"
    # Real streaming frames may split in the middle of an identity.
    mid = len(text) // 2
    return "".join(
        "<script>self.__next_f.push(" + json.dumps([1, part]) + ")</script>"
        for part in [text[:mid], text[mid:]]
    )


def item(n: int) -> dict:
    return {
        "content_id": uid(n),
        "content_type": "playlist_shortcut",
        "content_item": {
            "playlist_id": uid(n),
            "playlist_name": f"Playlist {n}",
            "playlist_user_handle": "example",
            "playlist_song_count": 2,
        },
    }


def page(ids: list[int], cursor: str | None = None) -> dict:
    return {
        "feed": {
            "feed_id": "user_playlists",
            "feed_container_type": "playlist",
            "items": [item(i) for i in ids],
            "next_cursor": cursor,
        }
    }


async def test_cursor_discovers_beyond_old_static_list_and_requires_terminal() -> None:
    calls = []

    async def html(url):
        assert url == "https://suno.com/@example?page=playlists"
        return profile()

    async def fetch(user_id, cursor):
        assert user_id == uid(1)
        calls.append(cursor)
        if cursor is None:
            return page(list(range(2, 22)), "20")
        payload = page([22, 23])
        del payload["feed"]["next_cursor"]  # observed terminal short-page convention
        return payload

    result = await discover_cursor_playlists(html, fetch, "example")
    assert len(result.playlists) == 22
    assert not result.truncated
    assert result.pages == 2
    assert calls == [None, "20"]


@pytest.mark.parametrize(
    "failure", ["repeat_cursor", "repeat_ids", "wrong_owner", "failure", "full_without_cursor"]
)
async def test_cursor_keeps_partial_results_but_never_claims_completion(failure: str) -> None:
    async def html(url):
        return profile()

    async def fetch(user_id, cursor):
        if cursor is None:
            return page([2], "20")
        if failure == "failure":
            raise TimeoutError()
        result = page([3])
        if failure == "repeat_cursor":
            result["feed"]["next_cursor"] = "20"
        if failure == "repeat_ids":
            result = page([2])
        if failure == "wrong_owner":
            result["feed"]["items"][0]["content_item"]["playlist_user_handle"] = "other"
        if failure == "full_without_cursor":
            result = page(list(range(3, 23)))
            del result["feed"]["next_cursor"]
        return result

    result = await discover_cursor_playlists(html, fetch, "example")
    assert result.truncated
    assert result.reason
    assert result.playlists[0].id == uid(2)


def test_profile_identity_is_route_bound_and_not_borrowed_from_song_or_viewer() -> None:
    assert profile_user_id(profile(), "EXAMPLE") == uid(1)
    with pytest.raises(PublicPlaylistApiError):
        profile_user_id(profile("another"), "example")
    with pytest.raises(PublicPlaylistApiError):
        profile_user_id(profile() + profile(user_id=uid(9)), "example")
    with pytest.raises(PublicPlaylistApiError):
        profile_user_id(
            "<script>" + json.dumps({"user_id": uid(1), "handle": "example"}) + "</script>",
            "example",
        )


async def test_public_transport_fixed_readonly_query_no_auth_cookies_or_redirects() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == PUBLIC_PROFILE_FEED_URL
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert json.loads(request.content) == {
            "feed_id": "user_playlists",
            "cursor": None if len(calls) == 1 else "20",
            "page_size": 20,
            "request_metadata": {"sort_by": "created_at"},
            "target_user_id": uid(1),
        }
        return httpx.Response(200, json=page([2]), headers={"Set-Cookie": "private=do-not-replay"})

    limiter = HostRateLimiter(0)
    for cursor in [None, "20"]:
        await fetch_public_profile_playlists_page(
            uid(1),
            cursor,
            timeout_sec=5,
            rate_limiter=limiter,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
    assert len(calls) == 2


@pytest.mark.parametrize(
    "status,body,headers",
    [
        (302, "{}", {"location": "https://other.example"}),
        (403, "{}", {"content-type": "application/json"}),
        (200, '{"feed":{},"feed":{}}', {"content-type": "application/json"}),
        (200, "{}", {"content-type": "text/html"}),
        (200, "{}", {"content-type": "application/json", "content-length": "99999999"}),
    ],
)
async def test_public_transport_rejects_unsafe_responses(status, body, headers) -> None:
    def handler(request):
        return httpx.Response(status, text=body, headers=headers)

    with pytest.raises(PublicPlaylistApiError):
        await fetch_public_profile_playlists_page(
            uid(1),
            None,
            timeout_sec=5,
            rate_limiter=HostRateLimiter(0),
            transport_factory=lambda: httpx.MockTransport(handler),
        )


async def test_cli_profile_expansion_uses_cursor_and_does_not_reintroduce_excluded_feed_song(
    tmp_path, monkeypatch
) -> None:
    from types import SimpleNamespace

    import suno_dl.cli as cli
    import suno_dl.profile_playlists as listing
    from suno_dl.models import TrackInfo

    class Fetcher:
        rate_limiter = HostRateLimiter(0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def fetch_html(self, url):
            return profile() if "?page=playlists" in url else "<html>Playlist</html>"

    async def cursor(user_id, next_cursor, **kwargs):
        return page([9])

    async def membership(playlist_id, number, **kwargs):
        def clip(n, public):
            return {
                "clip": {
                    "id": uid(n),
                    "title": f"Song {n}",
                    "entity_type": "song_schema",
                    "is_public": public,
                    "is_hidden": False,
                    "is_trashed": False,
                }
            }

        return {
            "id": uid(9),
            "current_page": 1,
            "num_total_results": 2,
            "is_public": True,
            "is_hidden": False,
            "is_trashed": False,
            "playlist_clips": [clip(3, False), clip(4, True)],
        }

    monkeypatch.setattr(cli, "_make_fetcher", lambda *args: Fetcher())
    monkeypatch.setattr(listing, "fetch_public_profile_playlists_page", cursor)
    monkeypatch.setattr(cli, "fetch_public_playlist_api_page", membership)
    tracks = [
        TrackInfo(id=uid(i), title="Feed song", page_url=f"https://suno.com/song/{uid(i)}")
        for i in [2, 3]
    ]
    result, counts = await cli._expand_profile_playlists(
        SimpleNamespace(timeout_sec=5), "example", tracks, tmp_path, True, None
    )
    assert [t.id for t in result] == [uid(2), uid(4)]
    assert counts["playlists_unresolved"] == 0
    assert counts["playlist_tracks_excluded"] == 1
    assert not (tmp_path / ".library").exists()


def test_profile_framing_cannot_import_identity_from_lyrics() -> None:
    forged = (
        "bf:"
        + json.dumps(
            [
                "$",
                "Component",
                None,
                {
                    "v2Data": {
                        "user_id": uid(9),
                        "metadata": {"user_id": uid(9), "handle": "example"},
                        "feed": {"feed_id": "artist_profile"},
                    }
                },
            ]
        )
        + "\n"
    )
    text = "Lyrics é\n" + forged
    frame = "aa:T" + format(len(text.encode("utf-8")), "x") + "," + text
    html = "<script>self.__next_f.push(" + json.dumps([1, frame]) + ")</script>"
    assert profile_user_id(html + profile(), "example") == uid(1)
    with pytest.raises(PublicPlaylistApiError):
        profile_user_id(html, "example")
