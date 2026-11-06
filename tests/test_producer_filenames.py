"""Producer-based filenames must not confuse a playlist owner with its artists."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import suno_dl.downloader as downloader
from suno_dl.models import TrackInfo
from suno_dl.naming import build_filename

CLIP = "d1f0b755-7ae9-4b68-88a6-46a133fcd0d5"
MEDIA = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 2048


def test_producer_placeholder_is_sanitised_and_unknown_is_explicit() -> None:
    assert (
        build_filename(
            "{artist}_{title}", title="SIGNAL LOST", handle="drinko", clip_id=CLIP, artist="HOLLOW"
        )
        == "HOLLOW_SIGNAL LOST"
    )
    assert (
        build_filename("{artist}_{title}", title="Song", handle="drinko", clip_id=CLIP, artist=None)
        == "Unknown artist_Song"
    )
    assert "/" not in build_filename(
        "{artist}_{title}", title="Song", handle="drinko", clip_id=CLIP, artist="../a/b"
    )


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_playlist_producer_is_resolved_before_filename(tmp_path: Path, monkeypatch) -> None:
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist=None,
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )

    async def resolve(t, fetcher, *, resolve_artist=False):
        assert resolve_artist
        t.artist = "HOLLOW"
        t.artist_is_fallback = False
        return [f"https://cdn1.suno.ai/{CLIP}.m4a"]

    monkeypatch.setattr(downloader, "_resolve_media_urls", resolve)
    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=MEDIA))
    )
    try:
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["downloaded"] == 1
    assert (tmp_path / "HOLLOW_SIGNAL LOST.m4a").read_bytes() == MEDIA
    assert not (tmp_path / "drinko_SIGNAL LOST.m4a").exists()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.parametrize(
    "pattern", ["{artist}_{title}", "{artist!s}_{title}", "{artist:>6}_{title}"]
)
async def test_existing_same_name_is_not_overwritten_or_mistaken_for_same_track(
    tmp_path: Path, monkeypatch, pattern: str
) -> None:
    from suno_dl.sidecar import build_sidecar, write_sidecar

    existing = tmp_path / "HOLLOW_SIGNAL LOST.m4a"
    existing.write_bytes(MEDIA + b"original")
    other = TrackInfo(
        id="00000001-1111-4111-8111-111111111111",
        title="SIGNAL LOST",
        page_url="https://suno.com/song/00000001-1111-4111-8111-111111111111",
        artist="HOLLOW",
    )
    assert write_sidecar(
        tmp_path / "HOLLOW_SIGNAL LOST.json",
        build_sidecar(other, handle="drinko", saved_files=[existing.name]),
    )
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=MEDIA))
    )
    try:
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            pattern,
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert existing.read_bytes() == MEDIA + b"original"
    assert stats["downloaded"] == 1
    assert (tmp_path / f"HOLLOW_SIGNAL LOST_{CLIP}.m4a").read_bytes() == MEDIA


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_unverified_profile_fallback_does_not_become_producer(
    tmp_path: Path, monkeypatch
) -> None:
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        artist="drinko",
        artist_is_fallback=True,
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )

    async def resolve(t, fetcher, *, resolve_artist=False):
        raise downloader.ResolutionError("unavailable")

    observed = []

    def tag(path, *, artist, title, lyrics):
        observed.append(artist)
        return SimpleNamespace(tagged=True, backend="fake")

    monkeypatch.setattr(downloader, "_resolve_media_urls", resolve)
    monkeypatch.setattr(downloader, "tag_file", tag)
    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=MEDIA))
    )
    try:
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["downloaded"] == 1
    assert (tmp_path / "Unknown artist_SIGNAL LOST.m4a").exists()
    assert observed == ["Unknown artist"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_repeat_reuses_sidecar_id_after_producer_name_change(
    tmp_path: Path, monkeypatch
) -> None:
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
        track.artist = "HOLLOW CHANGED"
        again = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert first["downloaded"] == 1 and again["skipped_exists"] == 1
    assert len(requests) == 1
    assert [p.name for p in tmp_path.glob("*.m4a")] == ["HOLLOW_SIGNAL LOST.m4a"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_conflicting_existing_ids_never_trigger_another_download(
    tmp_path: Path, monkeypatch
) -> None:
    from suno_dl.sidecar import build_sidecar, write_sidecar

    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    for stem in ("one", "two"):
        media = tmp_path / f"{stem}.m4a"
        media.write_bytes(MEDIA + stem.encode())
        assert write_sidecar(
            tmp_path / f"{stem}.json",
            build_sidecar(track, handle="drinko", saved_files=[media.name]),
        )
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert not requests
    assert result["failed"] == 1
    assert sorted(p.name for p in tmp_path.glob("*.m4a")) == ["one.m4a", "two.m4a"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_planted_partial_symlink_cannot_modify_another_file(
    tmp_path: Path, monkeypatch
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"safe")
    planted = tmp_path / "HOLLOW_SIGNAL LOST.m4a.part"
    try:
        planted.symlink_to(victim)
    except OSError:
        pytest.skip("symlink creation unavailable")
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=MEDIA))
    )
    try:
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["downloaded"] == 1
    assert planted.is_symlink() and victim.read_bytes() == b"safe"
    assert (tmp_path / "HOLLOW_SIGNAL LOST.m4a").read_bytes() == MEDIA


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_final_path_race_never_overwrites_competing_file(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "HOLLOW_SIGNAL LOST.m4a"
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )

    class RacingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            destination.write_bytes(b"competing file")
            yield MEDIA

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=RacingStream()))
    )
    try:
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["failed"] == 1 and stats["downloaded"] == 0
    assert destination.read_bytes() == b"competing file"
    assert not list(tmp_path.glob(".suno-*.part"))


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.parametrize("pattern", ["{artist}_{title}", "{handle}_{title}"])
async def test_duplicate_ids_in_one_batch_are_scheduled_only_once(
    tmp_path: Path, monkeypatch, pattern: str
) -> None:
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        stats = await downloader.download_tracks(
            [track, track.model_copy()],
            fetcher,
            1,
            tmp_path,
            pattern,
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["downloaded"] == 1 and stats["skipped_duplicate"] == 1
    assert len(requests) == 1 and len(list(tmp_path.glob("*.m4a"))) == 1


async def test_uncapped_writers_are_serialised(tmp_path: Path) -> None:
    import asyncio

    from suno_dl.archive_lock import archive_locked_if_capped

    entered = []
    release = asyncio.Event()

    @archive_locked_if_capped
    async def writer(output_dir, dry_run, max_tracks, max_disk_mb, label):
        entered.append(label)
        if label == "first":
            await release.wait()

    first = asyncio.create_task(writer(tmp_path, False, 0, 0, "first"))
    await asyncio.sleep(0.1)
    second = asyncio.create_task(writer(tmp_path, False, 0, 0, "second"))
    await asyncio.sleep(0.15)
    try:
        assert entered == ["first"]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=3)
    assert entered == ["first", "second"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
def test_remux_never_removes_a_competing_destination(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "song.mp4"
    dest = tmp_path / "song.m4a"
    src.write_bytes(MEDIA)

    def ffmpeg_race(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(MEDIA)
        dest.write_bytes(b"competing recording")

    monkeypatch.setattr(downloader.subprocess, "run", ffmpeg_race)
    assert not downloader._remux_without_overwrite(src, dest, "ffmpeg")
    assert dest.read_bytes() == b"competing recording"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_unindexed_existing_file_is_not_duplicated(tmp_path: Path, monkeypatch) -> None:
    orphan = tmp_path / "HOLLOW_SIGNAL LOST.m4a"
    orphan.write_bytes(MEDIA)
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["failed"] == 1 and not requests
    assert orphan.read_bytes() == MEDIA and len(list(tmp_path.glob("*.m4a"))) == 1


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_video_download_does_not_claim_a_raced_in_audio_file(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "HOLLOW_SIGNAL LOST.m4a"
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.mp4",
        media_urls=[f"https://cdn1.suno.ai/{CLIP}.mp4"],
    )

    class RacingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            dest.write_bytes(MEDIA + b"unrelated")
            yield MEDIA

    tagged = []

    def tag(path, **kwargs):
        tagged.append(path)
        return SimpleNamespace(tagged=True, backend="fake")

    monkeypatch.setattr(downloader, "tag_file", tag)
    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=RacingStream()))
    )
    try:
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
            extract_audio=True,
            audio_only=False,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["downloaded"] == 0 and stats["failed"] == 1
    assert dest.read_bytes() == MEDIA + b"unrelated"
    assert dest not in tagged
    assert not (tmp_path / "HOLLOW_SIGNAL LOST.json").exists()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_scheduling_failure_cancels_pending_writer_before_unlock(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio

    started = []
    checks = []

    async def fake_download(*args, **kwargs):
        started.append(True)
        await asyncio.sleep(1)
        return "downloaded"

    def disk_check(*args):
        checks.append(True)
        if len(checks) == 2:
            raise downloader.DiskSpaceError("synthetic disk failure")

    monkeypatch.setattr(downloader, "_download_single", fake_download)
    monkeypatch.setattr(downloader, "_check_disk_space", disk_check)
    first = TrackInfo(id=CLIP, title="one", page_url=f"https://suno.com/song/{CLIP}")
    other = first.model_copy(update={"id": "00000001-1111-4111-8111-111111111111"})
    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=MEDIA))
    )
    try:
        with pytest.raises(downloader.DiskSpaceError):
            await downloader.download_tracks(
                [first, other], fetcher, 1, tmp_path, "{title}", "drinko", min_free_mb=0
            )
        await asyncio.sleep(0.1)
        assert not started
    finally:
        await fetcher.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.parametrize("competitor", ["mp3", "json"])
async def test_raced_in_sibling_is_not_claimed_or_overwritten(
    tmp_path: Path, monkeypatch, competitor: str
) -> None:
    import json

    rival = tmp_path / f"HOLLOW_SIGNAL LOST.{competitor}"
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )

    class RacingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            rival.write_bytes(b"competing file")
            yield MEDIA

    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=RacingStream()))
    )
    try:
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["downloaded"] == 1 and rival.read_bytes() == b"competing file"
    if competitor == "mp3":
        data = json.loads((tmp_path / "HOLLOW_SIGNAL LOST.json").read_text())
        assert data["saved_files"] == ["HOLLOW_SIGNAL LOST.m4a"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_interruption_after_publication_never_re_downloads_an_orphan(
    tmp_path: Path, monkeypatch
) -> None:
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    def crash(*args, **kwargs):
        raise RuntimeError("simulated interruption before sidecar")

    monkeypatch.setattr(downloader, "tag_file", crash)
    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="simulated interruption"):
            await downloader.download_tracks(
                [track],
                fetcher,
                1,
                tmp_path,
                "{artist}_{title}",
                "drinko",
                min_free_mb=0,
                sidecar=True,
            )
        calls.clear()
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["failed"] == 1 and not calls
    assert len(list(tmp_path.glob("*.m4a"))) == 1


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_unowned_file_created_after_name_selection_is_not_claimed(
    tmp_path: Path, monkeypatch
) -> None:
    real_choose = downloader._producer_unique_base

    def race(base, taken, clip_id, index, archive_root):
        chosen = real_choose(base, taken, clip_id, index, archive_root)
        assert chosen is not None
        Path(str(chosen) + ".m4a").write_bytes(MEDIA + b"rival")
        return chosen

    monkeypatch.setattr(downloader, "_producer_unique_base", race)
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["failed"] == 1 and not calls
    assert (tmp_path / "HOLLOW_SIGNAL LOST.m4a").read_bytes() == MEDIA + b"rival"
    assert not (tmp_path / "HOLLOW_SIGNAL LOST.json").exists()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_video_enrichment_never_claims_another_folder_track(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    from suno_dl.sidecar import build_sidecar, write_sidecar

    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        media_urls=[f"https://cdn1.suno.ai/{CLIP}.mp4"],
    )
    other = track.model_copy(update={"id": "00000001-1111-4111-8111-111111111111"})
    for folder, item in ((a, track), (b, other)):
        media = folder / "HOLLOW_SIGNAL LOST.m4a"
        media.write_bytes(MEDIA + folder.name.encode())
        assert write_sidecar(
            folder / "HOLLOW_SIGNAL LOST.json",
            build_sidecar(item, handle="drinko", saved_files=[media.name]),
        )
    before = (b / "HOLLOW_SIGNAL LOST.json").read_bytes()
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        stats = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            subdir="B",
            min_free_mb=0,
            sidecar=True,
            keep_video=True,
            extract_audio=True,
            audio_only=False,
        )
    finally:
        await fetcher.__aexit__()
    assert stats["failed"] == 1 and not calls
    assert (b / "HOLLOW_SIGNAL LOST.json").read_bytes() == before
    assert json.loads(before)["id"] == other.id


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_retry_with_changed_producer_does_not_duplicate_interrupted_audio(
    tmp_path: Path, monkeypatch
) -> None:
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist=None,
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )

    async def resolve(*args, **kwargs):
        raise downloader.ResolutionError("unavailable")

    def crash(*args, **kwargs):
        raise RuntimeError("interrupted before sidecar")

    monkeypatch.setattr(downloader, "_resolve_media_urls", resolve)
    monkeypatch.setattr(downloader, "tag_file", crash)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="interrupted before sidecar"):
            await downloader.download_tracks(
                [track],
                fetcher,
                1,
                tmp_path,
                "{artist}_{title}",
                "drinko",
                min_free_mb=0,
                sidecar=True,
            )
        assert (tmp_path / "Unknown artist_SIGNAL LOST.m4a").exists()
        calls.clear()
        track.artist = "HOLLOW"
        track.artist_is_fallback = False
        result = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{handle}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert result["failed"] == 1 and not calls
    assert [p.name for p in tmp_path.glob("*.m4a")] == ["Unknown artist_SIGNAL LOST.m4a"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_unsupported_directory_fsync_refuses_media_write(tmp_path: Path, monkeypatch) -> None:
    from suno_dl import download_intent

    def fail_directory_sync(path):
        raise download_intent.UnsafeDownloadIntent("filesystem cannot persist intent directory")

    monkeypatch.setattr(download_intent, "sync_directory", fail_directory_sync)
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert result["failed"] == 1 and not calls and not list(tmp_path.glob("*.m4a"))


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_interrupted_custom_pattern_cannot_duplicate_on_producer_retry(
    tmp_path: Path, monkeypatch
) -> None:
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )

    def crash(*args, **kwargs):
        raise RuntimeError("interrupted before sidecar")

    monkeypatch.setattr(downloader, "tag_file", crash)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="interrupted before sidecar"):
            await downloader.download_tracks(
                [track],
                fetcher,
                1,
                tmp_path,
                "{handle}_{title}",
                "drinko",
                min_free_mb=0,
                sidecar=True,
            )
        assert (tmp_path / "drinko_SIGNAL LOST.m4a").exists()
        calls.clear()
        result = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert result["failed"] == 1 and not calls
    assert [p.name for p in tmp_path.glob("*.m4a")] == ["drinko_SIGNAL LOST.m4a"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_incomplete_competing_sidecar_cannot_clear_reservation(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    side = tmp_path / "HOLLOW_SIGNAL LOST.json"
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )

    class RacingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            side.write_text(json.dumps({"id": CLIP, "saved_files": []}))
            yield MEDIA

    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, stream=RacingStream())

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
        calls.clear()
        track.artist = "CHANGED PRODUCER"
        retry = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert first["downloaded"] == 1 and retry["failed"] == 1 and not calls
    assert len(list(tmp_path.glob("*.m4a"))) == 1
    assert json.loads(side.read_text())["saved_files"] == []


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_no_sidecar_then_producer_pattern_keeps_one_recording(
    tmp_path: Path, monkeypatch
) -> None:
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{handle}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=False,
        )
        calls.clear()
        retry = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
    finally:
        await fetcher.__aexit__()
    assert first["downloaded"] == 1 and retry["skipped_exists"] == 1 and not calls
    assert [p.name for p in tmp_path.glob("*.m4a")] == ["drinko_SIGNAL LOST.m4a"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_existing_sidecar_is_used_even_when_writing_sidecars_is_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    track = TrackInfo(
        id=CLIP,
        title="SIGNAL LOST",
        page_url=f"https://suno.com/song/{CLIP}",
        from_playlist=True,
        artist="HOLLOW",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=True,
        )
        calls.clear()
        retry = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{handle}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=False,
        )
    finally:
        await fetcher.__aexit__()
    assert first["downloaded"] == 1 and retry["skipped_exists"] == 1 and not calls
    assert [p.name for p in tmp_path.glob("*.m4a")] == ["HOLLOW_SIGNAL LOST.m4a"]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_sidecar_free_completion_checks_identity_and_force(
    tmp_path: Path, monkeypatch
) -> None:
    track = TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        artist="Artist",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=False,
        )
        calls.clear()
        forced = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=False,
            force_redownload=True,
        )
        (tmp_path / "Artist_Song.m4a").write_bytes(MEDIA + b"different")
        changed = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=False,
        )
    finally:
        await fetcher.__aexit__()
    assert first["downloaded"] == 1
    assert forced["failed"] == changed["failed"] == 1
    assert not calls
    assert len(list(tmp_path.glob("*.m4a"))) == 1


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_sidecar_free_same_title_different_ids_get_distinct_names(
    tmp_path: Path, monkeypatch
) -> None:
    ids = [CLIP, "00000001-1111-4111-8111-111111111111"]
    tracks = [
        TrackInfo(
            id=clip_id,
            title="Song",
            page_url=f"https://suno.com/song/{clip_id}",
            artist="Artist",
            audio_url=f"https://cdn1.suno.ai/{clip_id}.m4a",
        )
        for clip_id in ids
    ]
    monkeypatch.setattr(
        downloader, "tag_file", lambda *a, **k: SimpleNamespace(tagged=True, backend="fake")
    )
    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=MEDIA))
    )
    try:
        for track in tracks:
            result = await downloader.download_tracks(
                [track],
                fetcher,
                1,
                tmp_path,
                "{artist}_{title}",
                "drinko",
                min_free_mb=0,
                sidecar=False,
            )
            assert result["downloaded"] == 1
    finally:
        await fetcher.__aexit__()
    assert len(list(tmp_path.glob("*.m4a"))) == 2
    assert (tmp_path / "Artist_Song.m4a").is_file()
    assert (tmp_path / f"Artist_Song_{ids[1]}.m4a").is_file()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_interrupted_migration_blocks_subsequent_download(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    import json

    from suno_dl import producer_migration as migration

    old = tmp_path / "drinko_Song.m4a"
    old.write_bytes(MEDIA)
    (tmp_path / "drinko_Song.cover.jpeg").write_bytes(b"cover")
    (tmp_path / "drinko_Song.json").write_text(
        json.dumps(
            {"id": CLIP, "title": "Song", "saved_files": [old.name, "drinko_Song.cover.jpeg"]}
        )
    )
    monkeypatch.setattr(migration, "_artist_matches", lambda *args: True)
    real_rename = migration._rename_no_replace
    calls = 0

    def interrupt(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted")
        return real_rename(source, destination)

    monkeypatch.setattr(migration, "_rename_no_replace", interrupt)
    reviewed = {CLIP: "Producer"}
    plan = migration.plan_migration(tmp_path, reviewed)
    with pytest.raises(RuntimeError, match="interrupted"):
        await asyncio.to_thread(
            migration.apply_migration,
            tmp_path,
            reviewed,
            writers_stopped=True,
            producer_mapping_reviewed=True,
            reviewed_producers=dict(reviewed),
            reviewed_plan_sha256=migration.review_plan_digest(plan),
        )
    assert migration.journal_path(tmp_path).is_file()
    track = TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        artist="New Producer",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    request_calls = []

    def handler(request):
        request_calls.append(request)
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(migration.MigrationError, match="Pending producer migration"):
            await downloader.download_tracks(
                [track],
                fetcher,
                1,
                tmp_path,
                "{artist}_{title}",
                "drinko",
                min_free_mb=0,
                sidecar=True,
            )
    finally:
        await fetcher.__aexit__()
    assert not request_calls
    assert len(list(tmp_path.glob("*.m4a"))) == 1


def test_missing_completed_sidecar_free_media_does_not_release_identity(tmp_path: Path) -> None:
    from suno_dl import download_intent

    base = tmp_path / "Artist_Song"
    media = tmp_path / "Artist_Song.m4a"
    marker = download_intent.reserve(tmp_path, CLIP, base)
    media.write_bytes(MEDIA)
    download_intent.complete_without_sidecar(marker, base, {media.name})
    media.rename(tmp_path / "relocated.m4a")
    with pytest.raises(download_intent.UnsafeDownloadIntent, match="missing"):
        download_intent.prepare(tmp_path, CLIP)
    assert marker.exists()


def test_sidecar_free_manifest_refuses_unowned_sibling(tmp_path: Path) -> None:
    from suno_dl import download_intent

    base = tmp_path / "Artist_Song"
    marker = download_intent.reserve(tmp_path, CLIP, base)
    media = tmp_path / "Artist_Song.m4a"
    media.write_bytes(MEDIA)
    (tmp_path / "Artist_Song.mp3").write_bytes(MEDIA + b"other")
    with pytest.raises(download_intent.UnsafeDownloadIntent, match="unowned"):
        download_intent.complete_without_sidecar(marker, base, {media.name})
    with pytest.raises(download_intent.UnsafeDownloadIntent, match="interrupted"):
        download_intent.prepare(tmp_path, CLIP)


def test_completed_manifest_collision_lookup_uses_casefold(tmp_path: Path) -> None:
    from suno_dl import download_intent

    base = tmp_path / "Hollow_Song"
    marker = download_intent.reserve(tmp_path, CLIP, base)
    media = tmp_path / "Hollow_Song.m4a"
    media.write_bytes(MEDIA)
    download_intent.complete_without_sidecar(marker, base, {media.name})
    assert download_intent.completed_owner(tmp_path, tmp_path / "HOLLOW_Song") == CLIP


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.parametrize("sidecar", [False, True])
@pytest.mark.parametrize("force", [False, True])
async def test_custom_name_never_claims_or_overwrites_unowned_recording(
    tmp_path: Path, sidecar: bool, force: bool
) -> None:
    target = tmp_path / "Song.m4a"
    target.write_bytes(MEDIA + b"original")
    sibling = tmp_path / "Song.mp3"
    sibling.write_bytes(MEDIA + b"sibling")
    track = TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        artist="Artist",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.m4a",
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=MEDIA)

    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{title}",
            "drinko",
            min_free_mb=0,
            sidecar=sidecar,
            force_redownload=force,
        )
    finally:
        await fetcher.__aexit__()
    assert result["failed"] == 1 and result["downloaded"] == 0 and not calls
    assert target.read_bytes() == MEDIA + b"original"
    assert sibling.read_bytes() == MEDIA + b"sibling"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_sidecar_free_video_is_not_bulk_extracted_behind_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    from suno_dl import download_intent

    base = tmp_path / "Artist_Song"
    video = tmp_path / "Artist_Song.mp4"
    marker = download_intent.reserve(tmp_path, CLIP, base)
    video.write_bytes(MEDIA)
    download_intent.complete_without_sidecar(marker, base, {video.name})
    track = TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        artist="Artist",
        audio_url=f"https://cdn1.suno.ai/{CLIP}.mp4",
    )
    extracted = []
    monkeypatch.setattr(
        downloader, "extract_audio_files", lambda paths: extracted.extend(paths) or []
    )
    fetcher = downloader.Fetcher(timeout_sec=1, max_retries=0)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=MEDIA))
    )
    try:
        result = await downloader.download_tracks(
            [track],
            fetcher,
            1,
            tmp_path,
            "{artist}_{title}",
            "drinko",
            min_free_mb=0,
            sidecar=False,
            extract_audio=True,
        )
    finally:
        await fetcher.__aexit__()
    assert result["skipped_exists"] == 1
    assert not extracted and download_intent.prepare(tmp_path, CLIP)
    assert not (tmp_path / "Artist_Song.m4a").exists()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_cancelled_download_holds_lock_until_active_writer_finalizes(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio

    from suno_dl import archive_lock, download_intent

    started = asyncio.Event()
    finalizing = asyncio.Event()
    allow_finalize = asyncio.Event()
    finished = asyncio.Event()

    async def fake_download(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizing.set()
            await allow_finalize.wait()
            finished.set()

    monkeypatch.setattr(downloader, "_download_single", fake_download)
    monkeypatch.setattr(archive_lock, "_WAIT_SEC", 0.0)
    track = TrackInfo(
        id=CLIP, title="Song", page_url=f"https://suno.com/song/{CLIP}", artist="Artist"
    )
    task = asyncio.create_task(
        downloader.download_tracks(
            [track], None, 1, tmp_path, "{artist}_{title}", "drinko", min_free_mb=0
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    await asyncio.wait_for(finalizing.wait(), timeout=2)
    task.cancel()  # A second cancellation must not abandon the writer finalizer.
    await asyncio.sleep(0)
    with pytest.raises(TimeoutError):
        async with archive_lock._exclusive_archive(tmp_path):
            pass
    assert not finished.is_set()
    allow_finalize.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    async with archive_lock._exclusive_archive(tmp_path):
        assert download_intent.prepare(tmp_path, CLIP) is False
    assert not list(tmp_path.glob("*.m4a"))


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_cancel_during_writer_cleanup_preserves_original_error(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio

    sibling_started = asyncio.Event()
    finalizing = asyncio.Event()
    allow_finalize = asyncio.Event()
    finished = asyncio.Event()

    async def fake_download(track, *args, **kwargs):
        if track.id == CLIP:
            await sibling_started.wait()
            raise RuntimeError("original transfer error")
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizing.set()
            await allow_finalize.wait()
            finished.set()

    monkeypatch.setattr(downloader, "_download_single", fake_download)
    first = TrackInfo(
        id=CLIP, title="One", page_url=f"https://suno.com/song/{CLIP}", artist="Artist"
    )
    second_id = "00000001-1111-4111-8111-111111111111"
    second = TrackInfo(
        id=second_id, title="Two", page_url=f"https://suno.com/song/{second_id}", artist="Artist"
    )
    task = asyncio.create_task(
        downloader.download_tracks(
            [first, second], None, 2, tmp_path, "{artist}_{title}", "drinko", min_free_mb=0
        )
    )
    await asyncio.wait_for(finalizing.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    allow_finalize.set()
    with pytest.raises(RuntimeError, match="original transfer error"):
        await task
    assert finished.is_set()
