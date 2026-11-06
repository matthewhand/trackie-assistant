"""Production tagging guard regressions. These never bypass the guard."""

import json
import shutil
import subprocess

import pytest

from suno_dl import tagging
from suno_dl.library import LibraryEntry
from suno_dl.metadata import MetadataPlan, apply_metadata
from tests.test_producer_migration import ID1, _m4a_with_opaque_locale


def test_real_mp4_locale_remains_unchanged_when_public_tagging_is_refused(tmp_path):
    pytest.importorskip("mutagen")
    path = _m4a_with_opaque_locale(tmp_path, ".m4a", "Old")
    before = path.read_bytes()
    atom = before.index(b"data", before.index(b"zzzz"))
    assert before[atom + 8 : atom + 12] == (42).to_bytes(4, "big")
    result = tagging.tag_file(path, artist="New", title="New")
    assert not result.tagged
    assert "Embedded tag updates are unavailable" in result.reason
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".suno-tag-*"))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_real_mp3_status_flag_remains_unchanged_when_public_tagging_is_refused(tmp_path):
    from mutagen.id3 import ID3, TPE1, TXXX

    path = tmp_path / "Artist_Song.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "libmp3lame",
            str(path),
        ],
        check=True,
    )
    tags = ID3(path)
    tags.add(TXXX(encoding=3, desc="Sentinel", text=["hello"]))
    tags.add(TPE1(encoding=3, text=["Old"]))
    tags.save(path)
    content = bytearray(path.read_bytes())
    location = content.index(b"TXXX")
    content[location + 8] = 0x40
    path.write_bytes(content)
    result = tagging.tag_file(path, artist="New", title="New")
    assert not result.tagged
    assert "Embedded tag updates are unavailable" in result.reason
    assert path.read_bytes() == bytes(content)


def test_ffmpeg_replacement_boundary_is_blocked_before_any_stage(tmp_path, monkeypatch):
    path = _m4a_with_opaque_locale(tmp_path, ".m4a", "Old")
    before = path.read_bytes()

    def unexpected():
        pytest.fail("FFmpeg lookup should not run while writes are blocked")

    monkeypatch.setattr(tagging, "find_ffmpeg", unexpected)
    ok, reason = tagging._write_with_ffmpeg(path, "New", "New")
    assert not ok
    assert "Embedded tag updates are unavailable" in reason
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".suno-tag-*"))


def test_explicit_retag_dry_run_and_metadata_failure_preserve_audio_and_sidecar(tmp_path):
    path = _m4a_with_opaque_locale(tmp_path, ".m4a", "Old")
    sidecar = tmp_path / "drinko_A song.json"
    before = (path.read_bytes(), sidecar.read_bytes())
    dry = tagging.retag_tree(tmp_path, artist="Producer", dry_run=True)
    assert dry["scanned"] == 1
    assert (path.read_bytes(), sidecar.read_bytes()) == before
    live = tagging.retag_tree(tmp_path, artist="Producer")
    assert live["failed"] == 1 and live["tagged"] == 0
    assert (path.read_bytes(), sidecar.read_bytes()) == before

    entry = LibraryEntry(ID1, sidecar, (path,))
    plan = MetadataPlan(
        ID1,
        entry,
        {"artist": ("Old", "Producer")},
        {"id": ID1, "artist": "Producer", "title": "A song", "saved_files": [path.name]},
        (path,),
    )
    stats = {}
    result = apply_metadata(
        plan, dry_run=False, sidecar=True, lyrics_sidecar=None, stats=stats, echo=lambda _: None
    )
    assert result == "failed" and stats["metadata_failed"] == 1
    assert (path.read_bytes(), sidecar.read_bytes()) == before
    assert json.loads(sidecar.read_text())["artist"] == "Old"


@pytest.mark.asyncio
async def test_download_refuses_before_transfer_or_intent_if_tag_writes_unavailable(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from suno_dl import downloader
    from suno_dl.models import TrackInfo
    from suno_dl.tagging import TaggingUnavailable

    monkeypatch.setattr(downloader, "_require_trusted_framing", lambda: None)
    track = TrackInfo(id=ID1, title="Song", artist="Producer", page_url="")
    archive = tmp_path / "archive"
    with pytest.raises(TaggingUnavailable, match="separately reviewed"):
        await downloader.download_tracks(
            [track],
            SimpleNamespace(),
            1,
            archive,
            "{artist}_{title}",
            "owner",
            min_free_mb=0,
            force_redownload=True,
        )
    assert not archive.exists() or {p.name for p in archive.iterdir()} <= {".suno-sync.lock"}


@pytest.mark.asyncio
async def test_download_preview_reports_tag_block_without_writes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from suno_dl import downloader
    from suno_dl.models import TrackInfo

    monkeypatch.setattr(downloader, "_require_trusted_framing", lambda: None)
    track = TrackInfo(id=ID1, title="Song", artist="Producer", page_url="")
    from tests.test_producer_lookup import page

    async def fetch(clip_id):
        return page(clip_id)

    result = await downloader.download_tracks(
        [track],
        SimpleNamespace(fetch_song_page=fetch),
        1,
        tmp_path,
        "{artist}_{title}",
        "owner",
        dry_run=True,
        min_free_mb=0,
    )
    assert result["dry_run"] == 1 and result["failed"] == 0
    assert not any(p.name != ".suno-sync.lock" for p in tmp_path.iterdir())


def test_retag_dry_run_cli_reports_block_and_empty_tree_succeeds(tmp_path):
    from click.testing import CliRunner

    from suno_dl.cli import main

    path = _m4a_with_opaque_locale(tmp_path, ".m4a", "Old")
    before = path.read_bytes()
    runner = CliRunner()
    blocked = runner.invoke(
        main,
        [
            "--retag",
            str(tmp_path),
            "--retag-archive-root",
            str(tmp_path),
            "--artist",
            "Producer",
            "--dry-run",
        ],
    )
    assert blocked.exit_code != 0
    assert "blocked" in blocked.output.lower()
    assert "failed=1" in blocked.output
    assert path.read_bytes() == before

    empty = tmp_path / "empty"
    empty.mkdir()
    noop = runner.invoke(
        main,
        [
            "--retag",
            str(empty),
            "--retag-archive-root",
            str(empty),
            "--artist",
            "Producer",
            "--dry-run",
        ],
    )
    assert noop.exit_code == 0, noop.output
    assert "failed=0" in noop.output


def test_metadata_dry_run_reports_block_without_changing_sidecar(tmp_path):
    path = _m4a_with_opaque_locale(tmp_path, ".m4a", "Old")
    sidecar = tmp_path / "drinko_A song.json"
    before = (path.read_bytes(), sidecar.read_bytes())
    entry = LibraryEntry(ID1, sidecar, (path,))
    plan = MetadataPlan(
        ID1,
        entry,
        {"artist": ("Old", "Producer")},
        {"id": ID1, "artist": "Producer", "title": "A song", "saved_files": [path.name]},
        (path,),
    )
    messages = []
    stats = {}
    result = apply_metadata(
        plan, dry_run=True, sidecar=True, lyrics_sidecar=None, stats=stats, echo=messages.append
    )
    assert result == "failed" and stats["metadata_failed"] == 1
    assert "unavailable" in messages[0]
    assert (path.read_bytes(), sidecar.read_bytes()) == before


@pytest.mark.asyncio
async def test_cli_download_refusal_happens_before_discovery_and_playlist_checkpoint(
    tmp_path, monkeypatch
):
    from suno_dl import cli
    from suno_dl.config import Config
    from suno_dl.tagging import TaggingUnavailable

    monkeypatch.setattr(cli, "_require_trusted_framing", lambda: None)

    def unexpected(*_args, **_kwargs):
        pytest.fail("A blocked operation must not expand playlists")

    monkeypatch.setattr(cli, "_expand_profile_playlists", unexpected)
    archive = tmp_path / "archive"
    with pytest.raises(TaggingUnavailable, match="separately reviewed"):
        await cli._run(
            "https://suno.com/@owner",
            Config(default_handle="owner"),
            None,
            "{artist}_{title}",
            1,
            archive,
            False,
            False,
            playlists=True,
            metadata_only=True,
        )
    assert not archive.exists()


@pytest.mark.asyncio
async def test_existing_intent_is_not_read_or_cleared_when_tagging_is_blocked(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from suno_dl import downloader
    from suno_dl.models import TrackInfo

    monkeypatch.setattr(downloader, "_require_trusted_framing", lambda: None)
    intent_dir = tmp_path / ".library" / "download-intents"
    intent_dir.mkdir(parents=True)
    marker = intent_dir / (ID1 + ".json")
    marker.write_bytes(b"reserved ownership sentinel")
    before = marker.read_bytes()
    track = TrackInfo(id=ID1, title="Song", artist="Producer", page_url="")
    with pytest.raises(ValueError, match="Pending legacy publication"):
        await downloader.download_tracks(
            [track], SimpleNamespace(), 1, tmp_path, "{artist}_{title}", "owner", min_free_mb=0
        )
    assert marker.read_bytes() == before
    assert sorted(p.name for p in intent_dir.iterdir()) == [marker.name]


@pytest.mark.asyncio
async def test_empty_list_never_bulk_extracts_existing_indexed_video(tmp_path):
    from types import SimpleNamespace

    from suno_dl import downloader

    video = _m4a_with_opaque_locale(tmp_path, ".mp4", "Old")
    video.with_suffix(".json").write_text(json.dumps({"id": ID1, "saved_files": [video.name]}))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = await downloader.download_tracks(
        [],
        SimpleNamespace(),
        1,
        tmp_path,
        "{title}",
        "owner",
        extract_audio=True,
        min_free_mb=0,
    )
    assert result["downloaded"] == 0
    assert {
        p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name != ".suno-sync.lock"
    } == before
    assert not video.with_suffix(".m4a").exists()


@pytest.mark.asyncio
async def test_empty_list_without_extraction_remains_noop(tmp_path):
    from types import SimpleNamespace

    from suno_dl import downloader

    result = await downloader.download_tracks(
        [], SimpleNamespace(), 1, tmp_path, "{title}", "owner", min_free_mb=0
    )
    assert result["total"] == result["downloaded"] == result["failed"] == 0


@pytest.mark.asyncio
async def test_provenance_preflight_does_not_create_missing_archive(tmp_path):
    from types import SimpleNamespace

    from suno_dl import downloader
    from suno_dl.models import TrackInfo
    from suno_dl.producer_lookup import ProducerLookupError

    target = tmp_path / "not-created"
    track = TrackInfo(id="invalid", title="Song", artist="Producer", page_url="")
    with pytest.raises(ProducerLookupError):
        await downloader.download_tracks(
            [track], SimpleNamespace(), 1, target, "{title}", "owner", min_free_mb=0
        )
    assert not target.exists()


@pytest.mark.asyncio
async def test_empty_download_does_not_create_archive_or_lock(tmp_path):
    from types import SimpleNamespace

    from suno_dl import downloader

    target = tmp_path / "missing-archive"
    result = await downloader.download_tracks(
        [], SimpleNamespace(), 1, target, "{title}", "owner", min_free_mb=0
    )
    assert result["total"] == result["failed"] == 0
    assert not target.exists()


@pytest.mark.asyncio
async def test_empty_download_still_refuses_existing_migration_journal(tmp_path):
    from types import SimpleNamespace

    from suno_dl import downloader
    from suno_dl.producer_migration import MigrationError, journal_path

    journal = journal_path(tmp_path)
    journal.parent.mkdir()
    journal.write_text("{}")
    before = journal.read_bytes()
    with pytest.raises(MigrationError, match="Pending producer migration"):
        await downloader.download_tracks(
            [], SimpleNamespace(), 1, tmp_path, "{title}", "owner", min_free_mb=0
        )
    assert journal.read_bytes() == before
    assert not (tmp_path / ".suno-sync.lock").exists()


def test_direct_public_extraction_is_guarded_and_nonapplicable_inputs_are_noops(tmp_path):
    from suno_dl.downloader import extract_audio_files
    from suno_dl.tagging import TaggingUnavailable

    video = _m4a_with_opaque_locale(tmp_path, ".mp4", "Old")
    before = video.read_bytes()
    with pytest.raises(TaggingUnavailable):
        extract_audio_files([video])
    assert video.read_bytes() == before
    assert not video.with_suffix(".m4a").exists()
    assert extract_audio_files([]) == []
    assert extract_audio_files([tmp_path / "missing.mp4"]) == []
    existing = video.with_suffix(".m4a")
    existing.write_bytes(b"already owned")
    assert extract_audio_files([video]) == []
    assert existing.read_bytes() == b"already owned"


def test_explicit_artist_does_not_override_a_file_with_existing_title(tmp_path):
    import wave

    path = tmp_path / "Original_Song.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(bytes(16000))
    # Construct the old tagged fixture directly; no public tagging guard bypass.
    tagging._tag_wav(path, "Original", "Song")
    before = path.read_bytes()
    for dry in (True, False):
        stats = tagging.retag_tree(tmp_path, artist="Different", dry_run=dry)
        assert stats["skipped"] == 1 and stats["failed"] == stats["tagged"] == 0
        assert path.read_bytes() == before


def test_windows_refusal_has_safe_specific_cli_message():
    from suno_dl.cli import _safe_fatal_source_error
    from suno_dl.download_intent import UnsafeDownloadIntent, WindowsArchiveWriteUnavailable

    assert "Windows archive writes are unavailable" in _safe_fatal_source_error(
        WindowsArchiveWriteUnavailable("not echoed")
    )
    generic = _safe_fatal_source_error(UnsafeDownloadIntent("private path"))
    assert "private path" not in generic
    assert "archive write safety" in generic
