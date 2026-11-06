"""Tests for discovery logic, using a fake Playwright page (no browser).

`collect_tracks`, `read_target_songs`, and `scroll_until_stable` are pure with
respect to Playwright's transport, so a small duck-typed fake exercises the
scroll-termination logic that used to be untestable inline code.

`_discover_sync` itself is the Playwright wiring (launch, navigate, "View all"
click, scroll, teardown). It is tested here too by patching
`playwright.sync_api.sync_playwright` with a fake context manager — no browser
is launched, so these tests run anywhere (CI has no display and no browsers).

Feed-based discovery is tested by mocking ``httpx.AsyncClient`` responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from suno_dl import discovery
from suno_dl.discovery import (
    _clip_from_feed_item,
    _discover_sync,
    collect_tracks,
    discover_profile_tracks_feed,
    read_target_songs,
    scroll_until_stable,
)
from suno_dl.models import TrackInfo


@dataclass
class _Anchor:
    href: str | None
    text: str = "A Song"


class _AnchorList:
    def __init__(self, anchors: list[_Anchor], owner: _FakePage) -> None:
        self._anchors = anchors
        self._owner = owner

    def count(self) -> int:
        return len(self._owner.anchors)

    def nth(self, i: int):
        owner = self._owner
        idx = i

        class _Nth:
            def get_attribute(self, name: str):
                return owner.anchors[idx].href if name == "href" else None

            def inner_text(self) -> str:
                return owner.anchors[idx].text

        return _Nth()


class _Body:
    def __init__(self, owner: _FakePage) -> None:
        self._owner = owner

    def inner_text(self) -> str:
        return self._owner.body_text


class _ViewAllLocator:
    """Fake locator for the 'View all Songs' control."""

    def __init__(self, page: _FakePage) -> None:
        self._page = page

    def count(self) -> int:
        return 1 if self._page.has_view_all else 0

    @property
    def first(self) -> _ViewAllLocator:
        return self

    def click(self, timeout: int) -> None:
        self._page.clicks += 1
        if self._page.click_raises:
            raise RuntimeError("click failed")


class _FakePage:
    """Fake page whose anchor list grows on each `evaluate` (scroll)."""

    def __init__(
        self,
        pages: list[list[_Anchor]],
        body_text: str = "",
        has_view_all: bool = False,
        click_raises: bool = False,
    ) -> None:
        self._pages = pages
        self._calls = 0
        self.anchors: list[_Anchor] = pages[0] if pages else []
        self.body_text = body_text
        self.scrolls = 0
        self.has_view_all = has_view_all
        self.click_raises = click_raises
        self.clicks = 0

    def locator(self, selector: str):
        if selector == "body":
            return _Body(self)
        if "View all" in selector:
            return _ViewAllLocator(self)
        return _AnchorList(self.anchors, self)

    def evaluate(self, expression: str) -> None:
        self.scrolls += 1
        self._calls += 1
        if self._calls < len(self._pages):
            self.anchors = self._pages[self._calls]

    def wait_for_timeout(self, ms: int) -> None:
        return None

    def goto(self, url: str, **kw: object) -> None:
        return None


UUID_A = "a1b2c3d4-1111-4111-8111-111111111111"
UUID_B = "b2c3d4e5-2222-4222-8222-222222222222"
UUID_C = "c3d4e5f6-3333-4333-8333-333333333333"


def test_collect_tracks_basic() -> None:
    page = _FakePage([[_Anchor(f"/song/{UUID_A}", "First")]])
    tracks: dict[str, TrackInfo] = {}
    collect_tracks(page, "@drinko", tracks)
    assert list(tracks) == [UUID_A]
    assert tracks[UUID_A].title == "First"
    assert tracks[UUID_A].artist == "drinko"
    assert tracks[UUID_A].page_url.endswith(UUID_A)


def test_collect_tracks_skips_non_song_and_dupes() -> None:
    page = _FakePage(
        [
            [
                _Anchor("/song/" + UUID_A),
                _Anchor("/song/" + UUID_A),  # duplicate
                _Anchor("/playlist/xyz"),  # not a song
                _Anchor(None),  # missing href
                _Anchor("/song/not-a-uuid"),  # no uuid
            ]
        ]
    )
    tracks: dict[str, TrackInfo] = {}
    collect_tracks(page, "h", tracks)
    assert list(tracks) == [UUID_A]


def test_collect_tracks_empty_title_falls_back() -> None:
    page = _FakePage([[_Anchor(f"/song/{UUID_A}", "   ")]])
    tracks: dict[str, TrackInfo] = {}
    collect_tracks(page, "h", tracks)
    assert tracks[UUID_A].title == "Untitled"


def test_read_target_songs() -> None:
    assert read_target_songs(_FakePage([[]], body_text="148 songs")) == 148
    assert read_target_songs(_FakePage([[]], body_text="1 song")) == 1
    assert read_target_songs(_FakePage([[]], body_text="no count here")) is None


def test_scroll_stops_when_target_reached() -> None:
    # First page has 1 song, second adds 2 more; target=3 should stop early.
    page = _FakePage(
        [
            [_Anchor(f"/song/{UUID_A}")],
            [
                _Anchor(f"/song/{UUID_A}"),
                _Anchor(f"/song/{UUID_B}"),
                _Anchor(f"/song/{UUID_C}"),
            ],
        ]
    )
    tracks: dict[str, TrackInfo] = {}
    collect_tracks(page, "h", tracks)
    scroll_until_stable(page, tracks, "h", target_songs=3, max_idle=8, max_iters=50)
    assert len(tracks) == 3


def test_scroll_stops_after_max_idle_with_no_growth() -> None:
    page = _FakePage([[_Anchor(f"/song/{UUID_A}")]])
    tracks: dict[str, TrackInfo] = {}
    collect_tracks(page, "h", tracks)
    scroll_until_stable(page, tracks, "h", target_songs=None, max_idle=3, max_iters=100)
    # It should stop after ~max_idle idle iterations, not run all 100.
    assert page.scrolls <= 5
    assert len(tracks) == 1


def test_scroll_respects_max_iters() -> None:
    # A page that always grows would otherwise loop forever; max_iters caps it.
    pages = [[_Anchor(f"/song/{UUID_A}")]]
    for i in range(20):
        cid = f"d4e5f6a7-4444-4444-8444-{i:012d}"
        pages.append([*pages[-1], _Anchor(f"/song/{cid}")])
    page = _FakePage(pages)
    tracks: dict[str, TrackInfo] = {}
    scroll_until_stable(page, tracks, "h", target_songs=None, max_idle=99, max_iters=5)
    assert page.scrolls == 5


class _FakeBrowser:
    chromium_launch_kwargs: dict[str, object]

    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.contexts: list[_FakeContext] = []
        self.closed = False

    def new_context(self, user_agent: str) -> _FakeContext:
        ctx = _FakeContext(self._page, {"user_agent": user_agent})
        self.contexts.append(ctx)
        return ctx

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage, kwargs: dict[str, object]) -> None:
        self.page = page
        self.kwargs = kwargs

    def new_page(self) -> _FakePage:
        return self.page


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser

    def launch(self, **kwargs: object) -> _FakeBrowser:
        self._browser.chromium_launch_kwargs = kwargs
        return self._browser


class _FakePlaywrightHandle:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.chromium = _FakeChromium(browser)


class _FakeSyncPlaywright:
    """Callable context manager standing in for sync_playwright()."""

    def __init__(self, browser: _FakeBrowser) -> None:
        self.browser = browser
        self.entered = False
        self.exited = False

    def __call__(self) -> _FakeSyncPlaywright:
        return self

    def __enter__(self) -> _FakePlaywrightHandle:
        self.entered = True
        return _FakePlaywrightHandle(self.browser)

    def __exit__(self, *exc: object) -> None:
        self.exited = True


class _PlaywrightSpy:
    """Installs the fake ``sync_playwright`` and records what the wiring did."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fakes: list[_FakeSyncPlaywright] = []
        self._monkeypatch = monkeypatch

    def install(self, page: _FakePage) -> _FakeSyncPlaywright:
        fake = _FakeSyncPlaywright(_FakeBrowser(page))
        self.fakes.append(fake)
        self._monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: fake, raising=True)
        return fake


def test_discover_sync_clicks_view_all_and_scrolls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """View-all present: it is clicked, and all tracks are collected."""
    page = _FakePage(
        [
            [_Anchor(f"/song/{UUID_A}")],
            [_Anchor(f"/song/{UUID_A}"), _Anchor(f"/song/{UUID_B}")],
        ],
        body_text="2 songs",
        has_view_all=True,
    )
    spy = _PlaywrightSpy(monkeypatch)
    fake = spy.install(page)

    tracks = _discover_sync("https://suno.com/@h", "@h", headless=True, max_idle=2, max_iters=20)

    assert [t.id for t in tracks] == [UUID_A, UUID_B]
    assert page.clicks == 1
    assert page.scrolls >= 1
    assert fake.entered and fake.exited
    browser = fake.browser
    assert browser.closed
    assert browser.contexts and browser.contexts[0].page is page
    assert browser.contexts[0].kwargs == {"user_agent": discovery._UA}
    assert browser.chromium_launch_kwargs == {"headless": True}


def test_discover_sync_without_view_all_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """View-all absent: no click happens and discovery still completes."""
    page = _FakePage(
        [
            [_Anchor(f"/song/{UUID_A}"), _Anchor(f"/song/{UUID_B}")],
            [
                _Anchor(f"/song/{UUID_A}"),
                _Anchor(f"/song/{UUID_B}"),
                _Anchor(f"/song/{UUID_C}"),
            ],
        ],
        body_text="",
        has_view_all=False,
    )
    _PlaywrightSpy(monkeypatch).install(page)

    tracks = _discover_sync("https://suno.com/@h", "h", headless=False, max_idle=2, max_iters=20)

    assert [t.id for t in tracks] == [UUID_A, UUID_B, UUID_C]
    assert page.clicks == 0
    assert page.scrolls >= 1


def test_discover_sync_view_all_click_failure_is_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing 'View all' click is logged and discovery carries on."""
    page = _FakePage(
        [
            [_Anchor(f"/song/{UUID_A}")],
            [_Anchor(f"/song/{UUID_A}"), _Anchor(f"/song/{UUID_B}")],
        ],
        body_text="",
        has_view_all=True,
        click_raises=True,
    )
    spy = _PlaywrightSpy(monkeypatch)
    spy.install(page)

    tracks = _discover_sync("https://suno.com/@h", "h", headless=True, max_idle=2, max_iters=20)

    assert page.clicks == 1
    assert [t.id for t in tracks] == [UUID_A, UUID_B]


def test_discover_sync_stops_when_target_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page reports a target count: scrolling stops once it is reached."""
    page = _FakePage(
        [
            [_Anchor(f"/song/{UUID_A}"), _Anchor(f"/song/{UUID_B}")],
            [
                _Anchor(f"/song/{UUID_A}"),
                _Anchor(f"/song/{UUID_B}"),
                _Anchor(f"/song/{UUID_C}"),
            ],
        ],
        body_text="3 songs",
        has_view_all=False,
    )
    _PlaywrightSpy(monkeypatch).install(page)

    tracks = _discover_sync("https://suno.com/@h", "h", headless=True, max_idle=8, max_iters=50)

    assert [t.id for t in tracks] == [UUID_A, UUID_B, UUID_C]
    # Target of 3 hit after the first scroll: no idle drain of extra iterations.
    assert page.scrolls == 1


def test_discover_sync_idle_timeout_closes_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No growth and no target: loop exits on max_idle, browser still closed."""
    page = _FakePage(
        [[_Anchor(f"/song/{UUID_A}")]],
        body_text="",
        has_view_all=False,
    )
    spy = _PlaywrightSpy(monkeypatch)
    fake = spy.install(page)

    tracks = _discover_sync("https://suno.com/@h", "h", headless=True, max_idle=2, max_iters=50)

    assert [t.id for t in tracks] == [UUID_A]
    # Exactly max_idle idle scroll iterations, well before max_iters.
    assert page.scrolls == 2
    assert fake.exited
    assert fake.browser.closed


def test_clip_from_feed_item_valid() -> None:
    item = {
        "id": UUID_A,
        "title": "Test Song",
    }
    track = _clip_from_feed_item(item, "testhandle")
    assert track is not None
    assert track.id == UUID_A.lower()
    assert track.title == "Test Song"
    assert track.artist == "testhandle"
    assert track.page_url.endswith(UUID_A)


def test_clip_from_feed_item_invalid_id() -> None:
    item = {
        "id": "not-a-uuid",
        "title": "Test Song",
    }
    track = _clip_from_feed_item(item, "testhandle")
    assert track is None


def test_clip_from_feed_item_missing_title() -> None:
    item = {
        "id": UUID_A,
    }
    track = _clip_from_feed_item(item, "testhandle")
    assert track is not None
    assert track.title == "Untitled"


@pytest.mark.asyncio
async def test_discover_profile_tracks_feed_single_page() -> None:
    """Feed API returns all clips in a single page."""
    page1 = {
        "clips": [
            {"id": UUID_A, "title": "Song A"},
            {"id": UUID_B, "title": "Song B"},
        ],
        "has_more": False,
    }

    with patch("suno_dl.discovery._fetch_feed_page", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = [page1]
        with patch("suno_dl.ratelimit.HostRateLimiter.acquire", new_callable=AsyncMock):
            tracks = await discover_profile_tracks_feed(
                handle="testhandle",
                timeout=10.0,
                max_pages=10,
                rate_limit_sec=0.0,
            )

    assert len(tracks) == 2
    assert tracks[0].id == UUID_A.lower()
    assert tracks[0].title == "Song A"
    assert tracks[1].id == UUID_B.lower()
    assert tracks[1].title == "Song B"
    assert mock_fetch.call_count == 1


@pytest.mark.asyncio
async def test_discover_profile_tracks_feed_pagination() -> None:
    """Feed API returns clips across multiple pages."""
    page1 = {
        "clips": [
            {"id": UUID_A, "title": "Song A"},
            {"id": UUID_B, "title": "Song B"},
        ],
        "has_more": True,
    }
    page2 = {
        "clips": [
            {"id": UUID_C, "title": "Song C"},
        ],
        "has_more": False,
    }

    with patch("suno_dl.discovery._fetch_feed_page", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = [page1, page2]
        with patch("suno_dl.ratelimit.HostRateLimiter.acquire", new_callable=AsyncMock):
            tracks = await discover_profile_tracks_feed(
                handle="testhandle",
                timeout=10.0,
                max_pages=10,
                rate_limit_sec=0.0,
            )

    assert len(tracks) == 3
    assert [t.id for t in tracks] == [UUID_A.lower(), UUID_B.lower(), UUID_C.lower()]
    assert mock_fetch.call_count == 2


@pytest.mark.asyncio
async def test_discover_profile_tracks_feed_stops_on_empty_page() -> None:
    """Feed API returns empty clips list; discovery stops."""
    page1 = {
        "clips": [
            {"id": UUID_A, "title": "Song A"},
        ],
        "has_more": True,
    }
    page2 = {
        "clips": [],
        "has_more": False,
    }

    with patch("suno_dl.discovery._fetch_feed_page", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = [page1, page2]
        with patch("suno_dl.ratelimit.HostRateLimiter.acquire", new_callable=AsyncMock):
            tracks = await discover_profile_tracks_feed(
                handle="testhandle",
                timeout=10.0,
                max_pages=10,
                rate_limit_sec=0.0,
            )

    assert len(tracks) == 1
    assert mock_fetch.call_count == 2


@pytest.mark.asyncio
async def test_discover_profile_tracks_feed_404_stops() -> None:
    """Feed API returns 404; discovery stops gracefully."""
    import httpx

    with patch("suno_dl.discovery._fetch_feed_page", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = httpx.HTTPStatusError(
            "Not Found", request=MagicMock(), response=MagicMock(status_code=404)
        )
        with patch("suno_dl.ratelimit.HostRateLimiter.acquire", new_callable=AsyncMock):
            tracks = await discover_profile_tracks_feed(
                handle="testhandle",
                timeout=10.0,
                max_pages=10,
                rate_limit_sec=0.0,
            )

    assert tracks == []
    assert mock_fetch.call_count == 1


@pytest.mark.asyncio
async def test_discover_profile_tracks_feed_respects_max_pages() -> None:
    """Discovery stops after max_pages even if has_more is True."""
    page1 = {
        "clips": [{"id": UUID_A, "title": "Song A"}],
        "has_more": True,
    }
    page2 = {
        "clips": [{"id": UUID_B, "title": "Song B"}],
        "has_more": True,
    }
    page3 = {
        "clips": [{"id": UUID_C, "title": "Song C"}],
        "has_more": True,
    }

    with patch("suno_dl.discovery._fetch_feed_page", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = [page1, page2, page3]
        with patch("suno_dl.ratelimit.HostRateLimiter.acquire", new_callable=AsyncMock):
            tracks = await discover_profile_tracks_feed(
                handle="testhandle",
                timeout=10.0,
                max_pages=3,
                rate_limit_sec=0.0,
            )

    assert len(tracks) == 3
    assert mock_fetch.call_count == 3


# --- #113: the real URL / parameters, exercised through a mock HTTP transport ---


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _install_profile_api(monkeypatch, pages: list[int], total: int | None, seen: list):
    """Serve ``/api/profiles/synthetic`` like the real API (422 without sort params)."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path != "/api/profiles/synthetic":
            return httpx.Response(404, text="Not Found")
        q = request.url.params
        if "clips_sort_by" not in q or "playlists_sort_by" not in q:
            return httpx.Response(422, json={"detail": "missing sort parameters"})
        page = int(q.get("page", "1"))
        size = pages[page - 1] if page <= len(pages) else 0
        base = sum(pages[: page - 1])
        body: dict = {
            "clips": [
                {"id": _uuid(base + i), "title": f"Synthetic {base + i}"} for i in range(size)
            ],
            "current_page": page,
        }
        if total is not None:
            body["num_total_clips"] = total
        return httpx.Response(200, json=body)

    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("suno_dl.discovery.httpx.AsyncClient", _Client)


@pytest.mark.asyncio
async def test_feed_uses_profile_endpoint_with_sort_params_and_pages_to_the_end(
    monkeypatch,
) -> None:
    seen: list = []
    _install_profile_api(monkeypatch, pages=[3, 2, 4], total=None, seen=seen)
    tracks = await discover_profile_tracks_feed("@synthetic", rate_limit_sec=0.0, max_pages=10)
    assert len(tracks) == 9
    assert {t.id for t in tracks} == {_uuid(i) for i in range(9)}
    # pages 1-3 have clips, page 4 is empty and ends the loop
    assert [r.url.params["page"] for r in seen] == ["1", "2", "3", "4"]
    assert all(r.url.params["clips_sort_by"] == "created_at" for r in seen)
    assert all(r.url.params["playlists_sort_by"] == "created_at" for r in seen)
    assert all(r.url.path == "/api/profiles/synthetic" for r in seen)  # not ".../songs"


@pytest.mark.asyncio
async def test_feed_stops_at_num_total_clips_without_an_extra_request(monkeypatch) -> None:
    seen: list = []
    _install_profile_api(monkeypatch, pages=[3, 2], total=5, seen=seen)
    tracks = await discover_profile_tracks_feed("synthetic", rate_limit_sec=0.0, max_pages=10)
    assert len(tracks) == 5
    assert len(seen) == 2  # no probing page 3


@pytest.mark.asyncio
async def test_feed_404_is_logged_as_a_warning(monkeypatch, caplog) -> None:
    seen: list = []
    _install_profile_api(monkeypatch, pages=[3], total=None, seen=seen)
    with caplog.at_level("WARNING", logger="suno_dl.discovery"):
        tracks = await discover_profile_tracks_feed("other-handle", rate_limit_sec=0.0)
    assert tracks == []
    assert "404" in caplog.text


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"display_name": "SmileDJ", "handle": "smiledj"}, "SmileDJ"),
        ({"display_name": "Æsthaeth 🎵 e\u0301", "handle": "aesthaeth"}, "Æsthaeth 🎵 e\u0301"),
        ({"display_name": "Ricktron 5000"}, "Ricktron 5000"),
        ({"handle": "canonical_creator"}, "canonical_creator"),
        ({"handle": "@canonical_creator"}, "canonical_creator"),
    ],
)
def test_feed_creator_explicit_clip_fields_override_source_handle(
    fields: dict[str, str], expected: str
) -> None:
    """#157: the source profile is not always a clip's original creator."""
    item = {"id": UUID_A, "title": "Mixed profile song", **fields}
    track = _clip_from_feed_item(item, "drinko")
    assert track is not None
    assert track.artist == expected
    expected_handle = fields.get("handle")
    assert track.creator_handle == (expected_handle.lstrip("@") if expected_handle else None)
    assert track.id == UUID_A
    assert track.page_url.endswith(UUID_A)


@pytest.mark.parametrize(
    "malformed",
    [
        "Spoof\u202eexe.m4a",  # bidi override
        "Spoof\u2066Alice",  # bidi isolate
        "Control\u0000Name",
        "Bad\ud800Surrogate",
        "A" * 700,  # unbounded producer-controlled string
        "🎵" * 150,  # bounded characters but too many UTF-8 bytes
        "<script>alert(1)</script>",
    ],
)
def test_feed_creator_invalid_explicit_identity_is_unresolved(malformed: str) -> None:
    """Invalid explicit creator metadata must not silently become the profile owner."""
    track = _clip_from_feed_item(
        {"id": UUID_A, "title": "Track", "display_name": malformed},
        "drinko",
    )
    assert track is not None
    assert track.artist is None


def test_feed_creator_sidecar_keeps_canonical_identity_and_source_separate() -> None:
    """Producer and source handle must never be collapsed in persisted metadata."""
    import json

    from suno_dl.sidecar import build_sidecar

    track = _clip_from_feed_item(
        {
            "id": UUID_A,
            "title": "Song",
            "display_name": "Æsthaeth 🎵 e\u0301",
            "handle": "aesthaeth",
        },
        "drinko",
    )
    assert track is not None
    data = build_sidecar(
        track,
        handle="drinko",
        saved_files=["song.m4a"],
        downloaded_at="2026-01-01T00:00:00Z",
    )
    assert data["handle"] == "drinko"  # source provenance, unchanged
    assert data["creator_handle"] == "aesthaeth"  # canonical clip creator
    assert "Æsthaeth 🎵 e\u0301" in json.dumps(track.model_dump(), ensure_ascii=False)


@pytest.mark.parametrize("bad_handle", ["@@@", "evil/../", "bad%2fhandle"])
def test_feed_canonical_handles_cannot_be_unsafe_or_empty(bad_handle: str) -> None:
    track = _clip_from_feed_item(
        {"id": UUID_A, "title": "Song", "handle": bad_handle},
        "drinko",
    )
    assert track is not None
    assert track.artist is None
    assert track.creator_handle is None


@pytest.mark.asyncio
async def test_anonymous_feed_client_refuses_redirects_and_environment_credentials() -> None:
    with (
        patch("suno_dl.discovery.httpx.AsyncClient") as client_factory,
        patch("suno_dl.discovery._fetch_feed_page", new_callable=AsyncMock) as read,
        patch("suno_dl.ratelimit.HostRateLimiter.acquire", new_callable=AsyncMock),
    ):
        read.return_value = {"clips": []}
        await discover_profile_tracks_feed("synthetic", rate_limit_sec=0)
    kwargs = client_factory.call_args.kwargs
    assert kwargs["follow_redirects"] is False
    assert kwargs["trust_env"] is False


def test_browser_discovery_redacts_profile_and_click_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    page = _FakePage([[]], has_view_all=True)
    _PlaywrightSpy(monkeypatch).install(page)
    seen = []
    monkeypatch.setattr(page, "goto", lambda url, **kwargs: seen.append(url))

    def failing_click(*args, **kwargs):
        raise RuntimeError("failed https://cdn.example/path?token=CLICK_SECRET")

    monkeypatch.setattr(_ViewAllLocator, "click", failing_click)
    url = "https://suno.com/@artist?token=PROFILE_SECRET#FRAGMENT_SECRET"
    with caplog.at_level(logging.DEBUG, logger="suno_dl.discovery"):
        _discover_sync(url, "artist", headless=True, max_idle=1, max_iters=1)
    assert seen == [url]
    assert "Opening https://suno.com/@artist?...redacted" in caplog.text
    assert "View all click failed (RuntimeError)" in caplog.text
    for secret in ("PROFILE_SECRET", "FRAGMENT_SECRET", "CLICK_SECRET"):
        assert secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["request", "http"])
async def test_feed_discovery_does_not_log_exception_urls(
    caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    import logging

    import httpx

    request = httpx.Request("GET", "https://example.test/path?token=FEED_SECRET")
    if kind == "request":
        error = httpx.ConnectError("error " + str(request.url), request=request)
    else:
        response = httpx.Response(403, request=request)
        error = httpx.HTTPStatusError(
            "error " + str(request.url), request=request, response=response
        )
    with patch("suno_dl.discovery._fetch_feed_page", new_callable=AsyncMock) as fetch:
        fetch.side_effect = error
        with (
            patch("suno_dl.ratelimit.HostRateLimiter.acquire", new_callable=AsyncMock),
            caplog.at_level(logging.WARNING, logger="suno_dl.discovery"),
        ):
            tracks = await discover_profile_tracks_feed("artist", rate_limit_sec=0)
    assert tracks == []
    assert "FEED_SECRET" not in caplog.text
    assert "HTTP 403" in caplog.text if kind == "http" else "ConnectError" in caplog.text


@pytest.mark.asyncio
async def test_profile_fallback_does_not_log_untrusted_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    async def failing_feed(**kwargs):
        raise RuntimeError("failed https://suno.com/@x?token=FALLBACK_SECRET")

    monkeypatch.setattr(discovery, "discover_profile_tracks_feed", failing_feed)
    with caplog.at_level(logging.WARNING, logger="suno_dl.discovery"):
        tracks = await discovery.discover_profile_tracks(
            "https://suno.com/@x?token=PROFILE_SECRET",
            "x",
            use_browser_fallback=False,
        )
    assert tracks == []
    assert "RuntimeError" in caplog.text
    assert "FALLBACK_SECRET" not in caplog.text
    assert "PROFILE_SECRET" not in caplog.text


def test_discovery_browser_log_escapes_controls_and_bidi_for_direct_callers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    page = _FakePage([[]])
    _PlaywrightSpy(monkeypatch).install(page)
    seen = []
    monkeypatch.setattr(page, "goto", lambda url, **kwargs: seen.append(url))
    url = "https://suno.com/@artist\nFORGED\u202eBIDI?token=QUERY_SECRET"
    with caplog.at_level(logging.INFO, logger="suno_dl.discovery"):
        _discover_sync(url, "artist", headless=True, max_idle=1, max_iters=1)
    assert seen == [url]
    assert "QUERY_SECRET" not in caplog.text
    assert "\nFORGED" not in caplog.text
    assert "\u202e" not in caplog.text
    assert r"\u000aFORGED\u202eBIDI" in caplog.text


@pytest.mark.asyncio
async def test_discovery_feed_log_escapes_direct_handle_controls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    handle = "artist\nFORGED\u202eBIDI?token=QUERY_SECRET"
    with patch("suno_dl.discovery._fetch_feed_page", new_callable=AsyncMock) as fetch:
        fetch.return_value = {"clips": []}
        with (
            patch("suno_dl.ratelimit.HostRateLimiter.acquire", new_callable=AsyncMock),
            caplog.at_level(logging.DEBUG, logger="suno_dl.discovery"),
        ):
            await discover_profile_tracks_feed(handle, rate_limit_sec=0)
    assert fetch.call_args.args[1] == handle
    assert "QUERY_SECRET" not in caplog.text
    assert "\nFORGED" not in caplog.text
    assert "\u202e" not in caplog.text
    assert r"\u000aFORGED\u202eBIDI" in caplog.text
