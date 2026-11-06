"""Playback-duration pacing for bulk sync (#161)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from suno_dl.downloader import download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo
from suno_dl.pacing import (
    FALLBACK_DURATION_SEC,
    MAX_DURATION_SEC,
    PlaybackPacer,
    effective_duration_sec,
)

MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048


class FakeClock:
    """Monotonic stand-in: sleeping advances time instead of blocking."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _pacer(clock: FakeClock) -> PlaybackPacer:
    return PlaybackPacer(clock=clock, sleep=clock.sleep)


def _track(clip: str, title: str, duration: int | None) -> TrackInfo:
    return TrackInfo(
        id=clip,
        title=title,
        page_url=f"https://suno.com/song/{clip}",
        audio_url=f"https://cdn1.suno.ai/{clip}.m4a",
        media_urls=[f"https://cdn1.suno.ai/{clip}.m4a"],
        duration_sec=duration,
    )


def _fetcher(calls: list[str] | None = None) -> Fetcher:
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = Fetcher(timeout_sec=1.0, max_retries=1)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return fetcher


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, FALLBACK_DURATION_SEC),
        (0, FALLBACK_DURATION_SEC),
        (-3, FALLBACK_DURATION_SEC),
        (float("nan"), FALLBACK_DURATION_SEC),
        (float("inf"), FALLBACK_DURATION_SEC),
        ("not-a-number", FALLBACK_DURATION_SEC),
        (90, 90.0),
        (999_999, MAX_DURATION_SEC),
    ],
)
def test_effective_duration_is_bounded_and_finite(value: object, expected: float) -> None:
    assert effective_duration_sec(value) == expected  # type: ignore[arg-type]


async def test_first_transfer_waits_and_next_waits_previous_duration() -> None:
    clock = FakeClock()
    pacer = _pacer(clock)

    assert await pacer.wait(120) == 0.0
    assert clock.sleeps == []
    assert await pacer.wait(120) == 120.0
    assert clock.sleeps == [120.0]
    assert pacer.total_wait == 120.0


async def test_unknown_duration_uses_the_conservative_fallback() -> None:
    clock = FakeClock()
    pacer = _pacer(clock)

    await pacer.wait(None)
    await pacer.wait(None)

    assert clock.sleeps == [FALLBACK_DURATION_SEC]
    assert pacer.last_duration == FALLBACK_DURATION_SEC


async def test_huge_and_negative_durations_cannot_park_or_rush_a_sync() -> None:
    clock = FakeClock()
    pacer = _pacer(clock)

    await pacer.wait(10**9)
    await pacer.wait(-1)

    # The huge duration is clamped, the negative one falls back.
    assert clock.sleeps == [MAX_DURATION_SEC]
    assert pacer.last_duration == FALLBACK_DURATION_SEC


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_downloads_are_spaced_in_one_bulk_run(tmp_path: Path) -> None:
    clock = FakeClock()
    calls: list[str] = []
    fetcher = _fetcher(calls)
    tracks = [
        _track("aaaaaaaa-1111-4111-8111-111111111111", "One", 30),
        _track("bbbbbbbb-2222-4222-8222-222222222222", "Two", 60),
    ]

    stats = await download_tracks(
        tracks,
        fetcher,
        1,
        tmp_path,
        "{title}",
        "drinko",
        sidecar=False,
        pacer=_pacer(clock),
    )

    assert stats["downloaded"] == 2
    assert len(calls) == 2
    # One transfer necessarily follows the first; it waits that track's 30 s.
    assert clock.sleeps == [30.0]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_pacing_spans_sources_that_share_one_pacer(tmp_path: Path) -> None:
    clock = FakeClock()
    pacer = _pacer(clock)

    first = await download_tracks(
        [_track("aaaaaaaa-1111-4111-8111-111111111111", "One", 45)],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        sidecar=False,
        pacer=pacer,
    )
    second = await download_tracks(
        [_track("bbbbbbbb-2222-4222-8222-222222222222", "Two", 45)],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        sidecar=False,
        pacer=pacer,
    )

    assert first["downloaded"] == second["downloaded"] == 1
    # The second profile waits for the first profile's track instead of
    # starting immediately after the source switch.
    assert clock.sleeps == [45.0]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_already_archived_tracks_never_wait_or_fetch(tmp_path: Path) -> None:
    clock = FakeClock()
    track = _track("aaaaaaaa-1111-4111-8111-111111111111", "One", 30)
    await download_tracks(
        [track], _fetcher(), 1, tmp_path, "{title}", "drinko", sidecar=True, pacer=_pacer(clock)
    )
    clock.sleeps.clear()

    def refuse(request: httpx.Request) -> httpx.Response:  # pragma: no cover - asserted
        raise AssertionError(f"skip-existing made a media request: {request.url}")

    fetcher = Fetcher(timeout_sec=1.0, max_retries=1)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    stats = await download_tracks(
        [track], fetcher, 1, tmp_path, "{title}", "drinko", sidecar=True, pacer=_pacer(clock)
    )

    assert stats["skipped_exists"] == 1
    assert clock.sleeps == []


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_dry_run_and_video_only_skips_never_wait(tmp_path: Path) -> None:
    clock = FakeClock()
    pacer = _pacer(clock)

    stats = await download_tracks(
        [_track("aaaaaaaa-1111-4111-8111-111111111111", "One", 30)],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        dry_run=True,
        pacer=pacer,
    )
    assert stats["dry_run"] == 1

    video_only = TrackInfo(
        id="cccccccc-3333-4333-8333-333333333333",
        title="Video",
        page_url="https://suno.com/song/cccccccc-3333-4333-8333-333333333333",
        audio_url="https://cdn1.suno.ai/cccccccc-3333-4333-8333-333333333333.mp4",
        media_urls=["https://cdn1.suno.ai/cccccccc-3333-4333-8333-333333333333.mp4"],
        duration_sec=30,
    )
    waited = await download_tracks(
        [video_only],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        audio_only=True,
        pacer=pacer,
    )
    assert waited["skipped_video"] == 1
    assert clock.sleeps == []


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_concurrent_tasks_cannot_share_one_free_slot(tmp_path: Path) -> None:
    clock = FakeClock()
    tracks = [
        _track("aaaaaaaa-1111-4111-8111-111111111111", "One", 30),
        _track("bbbbbbbb-2222-4222-8222-222222222222", "Two", 30),
    ]

    stats = await download_tracks(
        tracks,
        _fetcher(),
        2,
        tmp_path,
        "{title}",
        "drinko",
        sidecar=False,
        pacer=_pacer(clock),
    )

    assert stats["downloaded"] == 2
    # Exactly one of the two starts was gated; both never start together.
    assert clock.sleeps == [30.0]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_without_a_pacer_behaviour_is_unchanged(tmp_path: Path) -> None:
    clock = FakeClock()
    tracks = [
        _track("aaaaaaaa-1111-4111-8111-111111111111", "One", 30),
        _track("bbbbbbbb-2222-4222-8222-222222222222", "Two", 30),
    ]

    stats = await download_tracks(tracks, _fetcher(), 1, tmp_path, "{title}", "drinko")

    assert stats["downloaded"] == 2
    assert clock.sleeps == []


async def test_pacing_checkpoint_survives_new_process_and_corruption_is_rejected(tmp_path):
    clock = FakeClock()
    checkpoint = tmp_path / "pacing.json"
    first = _pacer(clock)
    first.bind_checkpoint(checkpoint)
    await first.wait(300)
    clock.now += 50
    second = _pacer(clock)
    second.bind_checkpoint(checkpoint)
    await second.wait(90)
    assert clock.sleeps == [250]
    checkpoint.write_text("{}")
    with pytest.raises(ValueError):
        _pacer(clock).bind_checkpoint(checkpoint)


async def test_pacing_checkpoint_waits_conservatively_after_reboot(tmp_path):
    import json

    clock = FakeClock()
    checkpoint = tmp_path / "pacing.json"
    checkpoint.write_text(
        json.dumps({"schema": 1, "deadline": 1, "duration": 240, "boot": "different-boot"})
    )
    pacer = _pacer(clock)
    pacer.bind_checkpoint(checkpoint)
    await pacer.wait(10)
    assert clock.sleeps == [240]
