"""Tests for CLI internals: URL kinds and the _run dispatch path."""

from __future__ import annotations

from pathlib import Path

import click
import httpx
import pytest

import suno_dl.cli as cli_mod
from suno_dl.cli import _run, _run_sync, _url_kind
from suno_dl.config import Config

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://suno.com/@drinko", "profile"),
        ("https://suno.com/@drinko/", "profile"),
        ("https://suno.com/playlist/abc", "playlist"),
        (f"https://suno.com/song/{CLIP}", "song"),
    ],
)
def test_url_kind(url: str, kind: str) -> None:
    assert _url_kind(url) == kind


def test_url_kind_rejects_unknown() -> None:
    with pytest.raises(click.ClickException, match="profile, playlist, or song"):
        _url_kind("https://suno.com/whatever")


def _cfg(tmp_path: Path) -> Config:
    return Config(
        default_handle="drinko",
        default_url_type="profile",
        default_url="",
        concurrency=2,
        timeout_sec=1.0,
        max_retries=0,
        output_dir=tmp_path,
        pattern="{title}",
    )


@pytest.fixture
def mock_fetcher(monkeypatch: pytest.MonkeyPatch):
    """Patch cli.Fetcher to serve canned HTML via httpx MockTransport."""

    def _install(html: str) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        class _F(cli_mod.Fetcher):
            async def __aenter__(self):
                self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                return self

        monkeypatch.setattr(cli_mod, "Fetcher", _F)

    return _install


@pytest.fixture
def mock_feed(monkeypatch: pytest.MonkeyPatch):
    """Patch discovery.discover_profile_tracks_feed to return a canned track."""

    def _install(clips: list[dict] | None = None) -> None:
        if clips is None:
            clips = [{"id": CLIP, "title": "Test Song"}]

        async def fake_discover(handle, **kwargs):
            from suno_dl.models import TrackInfo
            from suno_dl.urls import cdn_cover_url, cdn_guess_urls, song_page_url

            tracks = []
            for c in clips:
                clip_id = c["id"].lower()
                media_urls = cdn_guess_urls(clip_id)
                tracks.append(
                    TrackInfo(
                        id=clip_id,
                        title=c.get("title", "Test Song"),
                        artist=handle.lstrip("@") or None,
                        page_url=song_page_url(clip_id),
                        audio_url=media_urls[0],
                        cover_url=cdn_cover_url(clip_id),
                        media_urls=media_urls,
                    )
                )
            return tracks

        monkeypatch.setattr("suno_dl.discovery.discover_profile_tracks_feed", fake_discover)

    return _install


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_run_dry_run_song(tmp_path: Path, mock_fetcher) -> None:
    mock_fetcher(f'<html><body><a href="/song/{CLIP}">A Song</a></body></html>')
    stats = await _run(
        f"https://suno.com/song/{CLIP}",
        _cfg(tmp_path),
        limit=None,
        pattern="{title}",
        concurrency=2,
        output_dir=tmp_path,
        dry_run=True,
        browser=False,
    )
    assert stats["dry_run"] == 1
    assert stats["total"] == 1


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_run_limit_applied(tmp_path: Path, mock_fetcher) -> None:
    html = "".join(f'<a href="/song/a1b2c3d4-1111-4111-8111-{i:012d}">S{i}</a>' for i in range(5))
    mock_fetcher(html)
    stats = await _run(
        "https://suno.com/playlist/x",
        _cfg(tmp_path),
        limit=2,
        pattern="{title}",
        concurrency=2,
        output_dir=tmp_path,
        dry_run=True,
        browser=False,
    )
    assert stats["dry_run"] == 2


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_run_no_tracks_returns_zero(tmp_path: Path, mock_fetcher) -> None:
    mock_fetcher("<html><body>nothing here</body></html>")
    stats = await _run(
        "https://suno.com/playlist/empty",
        _cfg(tmp_path),
        limit=None,
        pattern="{title}",
        concurrency=2,
        output_dir=tmp_path,
        dry_run=True,
        browser=False,
    )
    assert stats["total"] == 0


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_run_profile_no_browser_uses_parser(tmp_path: Path, mock_feed) -> None:
    mock_feed([{"id": CLIP, "title": "Test Song"}])
    stats = await _run(
        "https://suno.com/@drinko",
        _cfg(tmp_path),
        limit=None,
        pattern="{title}",
        concurrency=2,
        output_dir=tmp_path,
        dry_run=True,
        browser=False,
    )
    assert stats["dry_run"] == 1


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_run_forwards_audio_only_and_covers_to_downloader(
    tmp_path: Path, mock_fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new opt-in flags must reach download_tracks, defaults must not."""
    mock_fetcher(f'<html><body><a href="/song/{CLIP}">A Song</a></body></html>')
    seen: dict[str, object] = {}

    async def fake_download(
        tracks,
        fetcher,
        concurrency,
        output_dir,
        pattern,
        handle,
        dry_run=False,
        preferred_format=None,
        audio_only=False,
        covers=False,
        subdir=None,
        extract_audio=False,
        lyrics_sidecar=None,
        blocked_ids=frozenset(),
        max_tracks=0,
        max_disk_mb=0,
        rotate=False,
        min_free_mb=2048,
        sidecar=True,
        playback_paced=True,
        pacer=None,
        metadata_only=False,
        update_existing=False,
        force_redownload=False,
        library=None,
        source=None,
    ):
        seen.update(
            n=len(tracks),
            audio_only=audio_only,
            covers=covers,
            max_tracks=max_tracks,
            max_disk_mb=max_disk_mb,
            rotate=rotate,
        )
        return {
            "total": len(tracks),
            "dry_run": 0,
            "downloaded": 0,
            "skipped_exists": 0,
            "failed": 0,
            "skipped_video": 0,
        }

    monkeypatch.setattr(cli_mod, "download_tracks", fake_download)

    stats = await _run(
        f"https://suno.com/song/{CLIP}",
        _cfg(tmp_path),
        limit=None,
        pattern="{title}",
        concurrency=2,
        output_dir=tmp_path,
        dry_run=False,
        browser=False,
        audio_only=True,
        covers=True,
        max_tracks=7,
        max_disk_mb=42,
        rotate=True,
    )
    assert seen == {
        "n": 1,
        "audio_only": True,
        "covers": True,
        "max_tracks": 7,
        "max_disk_mb": 42,
        "rotate": True,
    }
    assert stats["total"] == 1


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_run_defaults_leave_flags_off(
    tmp_path: Path, mock_fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the flags, downloader receives audio_only=False / covers=False."""
    mock_fetcher(f'<html><body><a href="/song/{CLIP}">A Song</a></body></html>')
    seen: dict[str, object] = {}

    async def fake_download(
        tracks,
        fetcher,
        concurrency,
        output_dir,
        pattern,
        handle,
        dry_run=False,
        preferred_format=None,
        audio_only=False,
        covers=False,
        subdir=None,
        extract_audio=False,
        lyrics_sidecar=None,
        blocked_ids=frozenset(),
        max_tracks=0,
        max_disk_mb=0,
        rotate=False,
        min_free_mb=2048,
        sidecar=True,
        playback_paced=True,
        pacer=None,
        metadata_only=False,
        update_existing=False,
        force_redownload=False,
        library=None,
        source=None,
    ):
        seen.update(
            audio_only=audio_only,
            covers=covers,
            max_tracks=max_tracks,
            max_disk_mb=max_disk_mb,
            rotate=rotate,
        )
        return {
            "total": len(tracks),
            "dry_run": 0,
            "downloaded": 0,
            "skipped_exists": 0,
            "failed": 0,
            "skipped_video": 0,
        }

    monkeypatch.setattr(cli_mod, "download_tracks", fake_download)
    await _run(
        f"https://suno.com/song/{CLIP}",
        _cfg(tmp_path),
        limit=None,
        pattern="{title}",
        concurrency=2,
        output_dir=tmp_path,
        dry_run=False,
        browser=False,
    )
    assert seen == {
        "audio_only": False,
        "covers": False,
        "max_tracks": 0,
        "max_disk_mb": 0,
        "rotate": False,
    }


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_run_sync_dry_run_two_sources(tmp_path: Path, mock_feed) -> None:
    mock_feed([{"id": CLIP, "title": "Test Song"}])
    results = await _run_sync(
        _cfg(tmp_path),
        ["@alpha", "https://suno.com/@beta"],
        limit=None,
        pattern="{title}",
        concurrency=2,
        dry_run=True,
        browser=False,
        fmt=None,
        audio_only=False,
        covers=False,
        rate_limit=None,
        flat=False,
        extract_audio=False,
        lyrics_sidecar=None,
    )
    assert [r["dry_run"] for _h, r in results] == [1, 1]
    assert [h for h, _r in results] == ["alpha", "beta"]


async def test_run_sync_isolates_source_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One dead source must not abort the rest of the watch list."""
    from suno_dl.errors import DownloadError

    seen_urls: list[str] = []

    async def fake_run(url, cfg, *args, **kwargs):
        seen_urls.append(url)
        if "beta" in url:
            raise DownloadError("boom")
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0, "skipped_video": 0}

    monkeypatch.setattr(cli_mod, "_run", fake_run)
    results = await _run_sync(
        _cfg(tmp_path),
        ["@alpha", "@beta", "@gamma"],
        limit=None,
        pattern="{title}",
        concurrency=1,
        dry_run=False,
        browser=False,
        fmt=None,
        audio_only=False,
        covers=False,
        rate_limit=None,
        flat=False,
        extract_audio=False,
        lyrics_sidecar=None,
    )
    assert [h for h, _r in results] == ["alpha", "beta", "gamma"]
    assert results[1][1]["source_failed"] == 1
    assert results[1][1]["total"] == 0
    assert results[2][1]["downloaded"] == 1
    assert len(seen_urls) == 3


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_sync_refuses_pending_migration_before_discovery_or_membership(tmp_path, monkeypatch):
    from suno_dl.producer_migration import MigrationError

    metadata = tmp_path / ".library"
    metadata.mkdir()
    (metadata / "producer-rename-journal.json").write_text("incomplete")
    calls = []

    async def discover(*args, **kwargs):
        calls.append("network")
        return []

    monkeypatch.setattr(cli_mod, "discover_profile_tracks", discover)
    with pytest.raises(MigrationError, match="Pending producer migration"):
        await _run(
            "https://suno.com/@drinko",
            _cfg(tmp_path),
            None,
            "{title}",
            1,
            tmp_path,
            False,
            False,
            playlists=True,
        )
    assert calls == []
    assert not (metadata / "membership.json").exists()


async def test_windows_sync_refuses_before_discovery_or_state_creation(tmp_path, monkeypatch):
    from suno_dl import archive_lock
    from suno_dl.download_intent import UnsafeDownloadIntent

    missing = tmp_path / "archive"
    calls = []

    async def discover(*args, **kwargs):
        calls.append("network")
        return []

    monkeypatch.setattr(cli_mod, "discover_profile_tracks", discover)
    monkeypatch.setattr(archive_lock.sys, "platform", "win32")
    with pytest.raises(UnsafeDownloadIntent, match="Windows Trackie archive writes"):
        await _run(
            "https://suno.com/@drinko",
            _cfg(missing),
            None,
            "{title}",
            1,
            missing,
            False,
            False,
            playlists=True,
        )
    assert calls == [] and not missing.exists()


async def test_retag_refuses_pending_migration_before_audio_change(tmp_path, monkeypatch):
    from suno_dl.producer_migration import MigrationError

    metadata = tmp_path / ".library"
    metadata.mkdir()
    (metadata / "producer-rename-journal.json").write_text("incomplete")
    calls = []
    monkeypatch.setattr(cli_mod, "retag_tree", lambda *args, **kwargs: calls.append("retag"))
    with pytest.raises(MigrationError, match="Pending producer migration"):
        await cli_mod._retag_locked(tmp_path, False, "artist")
    assert calls == []


async def test_windows_retag_refuses_before_audio_change(tmp_path, monkeypatch):
    from suno_dl import archive_lock
    from suno_dl.download_intent import UnsafeDownloadIntent

    calls = []
    monkeypatch.setattr(cli_mod, "retag_tree", lambda *args, **kwargs: calls.append("retag"))
    monkeypatch.setattr(archive_lock.sys, "platform", "win32")
    with pytest.raises(UnsafeDownloadIntent, match="Windows Trackie archive writes"):
        await cli_mod._retag_locked(tmp_path, False, "artist")
    assert calls == [] and list(tmp_path.iterdir()) == []


async def test_nested_retag_checks_declared_ancestor_migration_journal(tmp_path, monkeypatch):
    from suno_dl.producer_migration import MigrationError

    target = tmp_path / "drinko"
    target.mkdir()
    metadata = tmp_path / ".library"
    metadata.mkdir()
    (metadata / "producer-rename-journal.json").write_text("incomplete")
    calls = []
    monkeypatch.setattr(cli_mod, "retag_tree", lambda *args, **kwargs: calls.append("retag"))
    with pytest.raises(MigrationError, match="Pending producer migration"):
        await cli_mod._retag_locked(tmp_path, False, "artist", target_dir=target)
    assert calls == []


async def test_nested_retag_waits_on_declared_archive_lock(tmp_path, monkeypatch):
    from suno_dl import archive_lock

    target = tmp_path / "drinko"
    target.mkdir()
    monkeypatch.setattr(archive_lock, "_WAIT_SEC", 0.0)
    async with archive_lock._exclusive_archive(tmp_path):
        with pytest.raises(TimeoutError):
            await cli_mod._retag_locked(tmp_path, False, "artist", target_dir=target)


async def test_run_sync_redacts_source_and_exception_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from suno_dl.errors import DownloadError

    urls = [
        "https://suno.com/playlist/abc?auth=SOURCE_SECRET#fragment",
        "https://suno.com/song/def?auth=SECOND_SECRET",
    ]
    seen = []

    async def fake_run(url, cfg, *args, **kwargs):
        seen.append(url)
        if "playlist" in url:
            raise DownloadError(
                f"request for {url} failed and https://cdn.example/audio?sig=CDN_SECRET"
            )
        return {"total": 1, "downloaded": 1}

    monkeypatch.setattr(cli_mod, "_run", fake_run)
    results = await _run_sync(
        _cfg(tmp_path),
        urls,
        limit=None,
        pattern="{title}",
        concurrency=1,
        dry_run=False,
        browser=False,
        fmt=None,
        audio_only=True,
        covers=False,
        rate_limit=None,
        flat=False,
        extract_audio=False,
        lyrics_sidecar=None,
    )
    output = capsys.readouterr()
    text = output.out + output.err + repr(results)
    assert seen == urls
    assert results[0][1]["source_failed"] == 1
    assert results[1][1]["downloaded"] == 1
    assert "download failed" in text
    assert "?...redacted" in text
    for secret in ("SOURCE_SECRET", "SECOND_SECRET", "CDN_SECRET", "fragment"):
        assert secret not in text


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
async def test_run_sync_does_not_echo_secrets_from_fatal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    error_type: type[Exception],
) -> None:
    url = "https://suno.com/song/abc?access=SOURCE_SECRET"

    async def fake_run(*args, **kwargs):
        raise error_type(f"failed {url} and credential=OTHER_SECRET")

    monkeypatch.setattr(cli_mod, "_run", fake_run)
    with pytest.raises(RuntimeError) as error:
        await _run_sync(
            _cfg(tmp_path),
            [url],
            limit=None,
            pattern="{title}",
            concurrency=1,
            dry_run=False,
            browser=False,
            fmt=None,
            audio_only=True,
            covers=False,
            rate_limit=None,
            flat=False,
            extract_audio=False,
            lyrics_sidecar=None,
        )
    captured = capsys.readouterr()
    output = captured.out + captured.err + str(error.value)
    assert "SOURCE_SECRET" not in output
    assert "OTHER_SECRET" not in output
    assert "?...redacted" in output


async def test_song_queue_checks_current_skip_list(tmp_path, monkeypatch, capsys):
    from suno_dl.errors import DownloadError

    first, skipped, failed, last = (
        f"00000000-0000-4000-8000-{number:012}" for number in range(1, 5)
    )
    blocked_path = tmp_path / ".blocked"
    seen = []

    async def fake_run(url, *args, **kwargs):
        clip = url.rsplit("/", 1)[-1]
        seen.append((clip, kwargs["pacer"]))
        if clip == first:
            blocked_path.write_text(skipped + "\n")
        if clip == failed:
            raise DownloadError("unavailable")
        return {"total": 1, "downloaded": 1, "failed": 0}

    monkeypatch.setattr(cli_mod, "_run", fake_run)
    await _run_sync(
        _cfg(tmp_path),
        [f"https://suno.com/song/{clip}" for clip in (first, skipped, failed, last)],
        limit=None,
        pattern="{title}",
        concurrency=1,
        dry_run=False,
        browser=False,
        fmt=None,
        audio_only=True,
        covers=False,
        rate_limit=None,
        flat=False,
        extract_audio=False,
        lyrics_sidecar=None,
        blocked_path=blocked_path,
    )
    assert [clip for clip, _ in seen] == [first, failed, last]
    assert seen[0][1] is not None and all(pacer is seen[0][1] for _, pacer in seen)
    events = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("TRACKIE_RETRY ")
    ]
    assert events == [
        f"TRACKIE_RETRY {first} working",
        f"TRACKIE_RETRY {skipped} skipped",
        f"TRACKIE_RETRY {failed} working",
        f"TRACKIE_RETRY {failed} failed",
        f"TRACKIE_RETRY {last} working",
    ]
