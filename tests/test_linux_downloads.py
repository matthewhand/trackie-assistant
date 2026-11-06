"""Public Linux downloader acceptance: real audio, mocked provider, no bypasses."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from mutagen.mp4 import MP4

from suno_dl import downloader, linux_downloads, publication
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo
from suno_dl.sidecar import build_sidecar
from suno_dl.tagging import TaggingUnavailable
from tests.test_producer_lookup import ID, OTHER, page

PAYLOAD = Path(__file__).parent / "fixtures/lossless-native/sample.m4a"


def incoming(clip_id=ID):
    return TrackInfo(
        id=clip_id,
        title="Caller title",
        artist="drinko",
        creator_handle="wrong",
        page_url="https://suno.com/song/" + OTHER,
        audio_url="https://cdn1.suno.ai/" + OTHER + ".mp3",
    )


async def download(root, tracks, mock_http, **options):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.host == "suno.com":
            return httpx.Response(200, text=page())
        return httpx.Response(200, content=PAYLOAD.read_bytes())

    mock_http(handler)
    async with Fetcher(10, 0) as fetcher:
        result = await downloader.download_tracks(
            tracks,
            fetcher,
            1,
            root,
            "{artist}_{title}",
            "owner",
            min_free_mb=0,
            **({"covers": False} | options),
        )
    return result, requests


@pytest.mark.parametrize("provided_artist", ["drinko", "Actual Producer", None])
async def test_public_download_prepares_verified_tags_and_sidecar_before_publication(
    tmp_path, mock_http, monkeypatch, provided_artist
):
    root = tmp_path / "library"
    original = linux_downloads.publish_prepared
    observed = []

    def before_publication(root, marker):
        state = json.loads(marker.read_text())
        assert state["schema"] == 2
        assert not list(root.glob("*.m4a"))
        audio = next(
            root / m["stage"] for m in state["members"] if m["destination"].endswith(".m4a")
        )
        assert audio.suffix == ".part"
        tags = MP4(audio)
        assert tags["\xa9ART"] == ["Actual Producer"] and tags["\xa9nam"] == ["A song"]
        observed.append(state)
        return original(root, marker)

    monkeypatch.setattr(linux_downloads, "publish_prepared", before_publication)
    track = incoming()
    track.artist = provided_artist
    track.from_playlist = True
    result, requests = await download(root, [track], mock_http)
    assert result["downloaded"] == result["tagged"] == result["sidecars"] == 1
    assert observed and all(OTHER not in u for u in requests)
    audio = root / "Actual Producer_A song.m4a"
    assert audio.is_file() and audio.stat().st_nlink == 1
    side = json.loads(audio.with_suffix(".json").read_text())
    assert side["artist"] == "Actual Producer" and side["creator_handle"] == "actual"
    assert side["handle"] == "owner"
    assert track.artist == provided_artist and track.title == "Caller title"
    assert not list((root / ".suno-staging").rglob("*.m4a"))
    assert not list((root / ".library/verified-sync-intents").iterdir())


async def test_indexed_recordings_are_unchanged_and_skipped_before_producer_lookup(
    tmp_path, mock_http
):
    audio = tmp_path / "drinko_Older.m4a"
    audio.write_bytes(PAYLOAD.read_bytes())
    track = incoming(OTHER)
    side = audio.with_suffix(".json")
    side.write_text(
        json.dumps(
            build_sidecar(
                track,
                handle="owner",
                saved_files=[audio.name],
            )
        )
    )
    before = (audio.read_bytes(), side.read_bytes(), audio.stat().st_mtime_ns)
    result, requests = await download(tmp_path, [track, incoming()], mock_http)
    assert result["skipped_exists"] == 1 and result["downloaded"] == 1
    assert not any(u == "https://suno.com/song/" + OTHER for u in requests)
    assert (audio.read_bytes(), side.read_bytes(), audio.stat().st_mtime_ns) == before
    assert len(list(tmp_path.glob("*.m4a"))) == 2


async def test_preview_verifies_producer_but_never_creates_archive(tmp_path, mock_http, capsys):
    root = tmp_path / "not-created"
    result, requests = await download(root, [incoming()], mock_http, dry_run=True)
    assert result["dry_run"] == 1 and result["failed"] == 0
    assert requests == ["https://suno.com/song/" + ID]
    assert not root.exists()
    assert "Actual Producer_A song" in capsys.readouterr().out


async def test_interrupted_group_recovers_before_rescheduling_without_second_transfer(
    tmp_path, mock_http, monkeypatch
):
    real_link = publication.os.link
    count = 0

    def stop_between_members(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected crash between audio and sidecar")
        return real_link(*args, **kwargs)

    monkeypatch.setattr(publication.os, "link", stop_between_members)
    with pytest.raises(OSError):
        await download(tmp_path, [incoming()], mock_http)
    marker = tmp_path / ".library/verified-sync-intents" / (ID + ".json")
    assert marker.exists()
    monkeypatch.setattr(publication.os, "link", real_link)
    result, requests = await download(tmp_path, [incoming()], mock_http)
    assert requests == [] and result["downloaded"] == 0 and result["skipped_exists"] == 1
    assert len(list(tmp_path.glob("*.m4a"))) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert next(tmp_path.glob("*.m4a")).stat().st_nlink == 1
    assert not marker.exists()


@pytest.mark.parametrize(
    "mode", ["metadata_only", "update_existing", "force_redownload", "rotate", "keep_video"]
)
async def test_indexed_mutation_options_remain_blocked_before_any_request_or_write(
    tmp_path, mock_http, mode
):
    root = tmp_path / "not-created"
    with pytest.raises(TaggingUnavailable, match="separately reviewed"):
        await download(root, [incoming()], mock_http, **{mode: True})
    assert not root.exists()


async def test_blocklist_and_repeated_ids_do_not_create_redundant_requests(tmp_path, mock_http):
    result, requests = await download(
        tmp_path,
        [incoming(), incoming(), incoming(OTHER)],
        mock_http,
        blocked_ids=frozenset({OTHER}),
    )
    assert result["skipped_blocked"] == result["skipped_duplicate"] == result["downloaded"] == 1
    assert requests.count("https://suno.com/song/" + ID) == 1
    assert all(OTHER not in u for u in requests)


async def test_preparation_failure_never_publishes_audio_or_sidecar(
    tmp_path, mock_http, monkeypatch
):
    def rejected(*args, **kwargs):
        raise ValueError("unsupported or damaged media")

    monkeypatch.setattr(linux_downloads, "prepare_audio", rejected)
    result, _ = await download(tmp_path, [incoming()], mock_http)
    assert result["failed"] == 1 and result["downloaded"] == 0
    assert not list(tmp_path.glob("*.m4a")) and not list(tmp_path.glob("*.json"))
    assert not list((tmp_path / ".library/verified-sync-intents").iterdir())


async def test_four_member_group_with_real_cover_and_lrc_recovers_without_duplicates(
    tmp_path, mock_http, monkeypatch
):
    import base64

    import suno_dl.sidecar as sidecars

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
    )

    async def staged_cover(track, base, output, fetcher, budget=None):
        cover = Path(str(base) + ".cover.png")
        cover.write_bytes(png)
        return cover

    monkeypatch.setattr(linux_downloads, "_stage_cover", staged_cover)
    track = incoming()
    track.lyrics = "[00:00.00]A synthetic test lyric"
    real = publication.os.link
    count = 0

    def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("stop after publishing audio and cover")
        return real(*args, **kwargs)

    monkeypatch.setattr(publication.os, "link", interrupted)
    with pytest.raises(OSError):
        await download(tmp_path, [track], mock_http, covers=True, lyrics_sidecar="lrc")
    marker = tmp_path / ".library/verified-sync-intents" / (ID + ".json")
    assert marker.exists()
    monkeypatch.setattr(publication.os, "link", real)
    result, requests = await download(
        tmp_path, [track], mock_http, covers=True, lyrics_sidecar="lrc"
    )
    assert requests == [] and result["skipped_exists"] == 1
    assert (tmp_path / "Actual Producer_A song.cover.png").read_bytes() == png
    assert bytes(MP4(tmp_path / "Actual Producer_A song.m4a")["covr"][0]) == png
    assert (tmp_path / "Actual Producer_A song.lrc").is_file()
    side = sidecars.read_sidecar(tmp_path / "Actual Producer_A song.json", strict_identity=True)
    assert side["saved_files"] == sorted(
        [
            "Actual Producer_A song.m4a",
            "Actual Producer_A song.cover.png",
            "Actual Producer_A song.lrc",
        ]
    )
    assert all(path.stat().st_nlink == 1 for path in tmp_path.glob("Actual Producer*"))
    assert not marker.exists()


async def test_oversized_sidecar_cannot_publish_unindexable_audio(tmp_path, mock_http):
    track = incoming()
    track.model_name = "x" * (300 * 1024)
    with pytest.raises(ValueError, match="indexed"):
        await download(tmp_path, [track], mock_http)
    assert not list(tmp_path.glob("*.m4a")) and not list(tmp_path.glob("*.json"))
    assert not list((tmp_path / ".library/verified-sync-intents").iterdir())


async def test_legacy_pruned_uuid_is_not_fetched_and_state_is_unchanged(tmp_path, mock_http):
    path = tmp_path / ".suno-rotation.json"
    path.write_text(json.dumps({"version": 1, "tracks": {}, "pruned": [ID]}))
    before = path.read_bytes()
    result, requests = await download(tmp_path, [incoming()], mock_http, max_tracks=10)
    assert requests == [] and result["skipped_rotated"] == 1
    assert path.read_bytes() == before


async def test_rotation_journal_blocks_before_any_network_or_media_write(tmp_path, mock_http):
    path = tmp_path / ".suno-rotation.journal"
    path.write_bytes(b"unreconciled")
    with pytest.raises(ValueError, match="rotation journal"):
        await download(tmp_path, [incoming()], mock_http)
    assert path.read_bytes() == b"unreconciled"
    assert not list(tmp_path.glob("*.m4a"))


async def test_cover_backpressure_stops_without_publishing_prepared_audio(tmp_path, mock_http):
    import suno_dl.linux_downloads as core

    cover_url = "https://cdn2.suno.ai/" + ID + ".jpeg"
    track = incoming()
    track.cover_url = cover_url
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "60"})

    mock_http(handler)
    async with Fetcher(10, 0) as fetcher:
        with pytest.raises(RuntimeError, match="backpressure"):
            await core._stage_cover(track, tmp_path / "private", tmp_path, fetcher)
    assert requests == [cover_url] and not list(tmp_path.iterdir())


async def test_unreadable_staging_subtree_is_not_treated_as_empty(tmp_path, mock_http, monkeypatch):
    import os

    hidden = tmp_path / ".suno-staging/blocked"
    hidden.mkdir(parents=True)
    original = os.scandir

    def denied(path):
        if Path(path) == hidden:
            raise PermissionError("injected unreadable staging")
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(ValueError, match="Cannot inspect private staging"):
        await download(tmp_path, [incoming()], mock_http)
    assert not list(tmp_path.glob("*.m4a"))


async def test_special_staging_artifact_fails_without_opening_it(tmp_path, mock_http):
    import os

    stage = tmp_path / ".suno-staging/unknown"
    stage.mkdir(parents=True)
    os.mkfifo(stage / "unrecognized")
    with pytest.raises(ValueError, match="Unrecorded private staging"):
        await download(tmp_path, [incoming()], mock_http)
    assert not list(tmp_path.glob("*.m4a"))
