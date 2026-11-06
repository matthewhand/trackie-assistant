"""Independent regressions for draft PR #131: clip-ID resume and paired-media bounds.

All requests use MockTransport; valid AAC input is created locally with ffmpeg.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from suno_dl.downloader import download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo
from suno_dl.sidecar import build_sidecar, write_sidecar

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
M4A = f"https://cdn1.suno.ai/{CLIP}.m4a"
MP4 = f"https://cdn1.suno.ai/{CLIP}.mp4"
MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048


def _track(title: str = "Updated Title") -> TrackInfo:
    return TrackInfo(
        id=CLIP,
        title=title,
        handle=None,
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=M4A,
        media_urls=[M4A, MP4],
    )


def _fake_fetcher(handler) -> Fetcher:
    fetcher = Fetcher(timeout_sec=1.0, max_retries=1)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return fetcher


def _seed_sidecar(folder: Path, title: str, extensions: tuple[str, ...]) -> None:
    paths = []
    for ext in extensions:
        path = folder / f"{title}{ext}"
        path.write_bytes(MEDIA)
        paths.append(path.name)
    assert write_sidecar(
        folder / f"{title}.json",
        build_sidecar(_track(title), handle="h", saved_files=paths),
    )


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_renamed_title_existing_pair_skips_without_any_request(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, "Original Title", (".m4a", ".mp4"))
    before = [(p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(tmp_path.iterdir())]
    fetcher = _fake_fetcher(
        lambda req: (_ for _ in ()).throw(AssertionError(f"unexpected media request: {req.url}"))
    )
    try:
        stats = await download_tracks(
            [_track()],
            fetcher,
            1,
            tmp_path,
            "{title}",
            "h",
            keep_video=True,
            extract_audio=True,
            sidecar=True,
            min_free_mb=0,
        )
        assert (stats["downloaded"], stats["skipped_exists"]) == (0, 1)
        after = [
            (p.name, p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(tmp_path.iterdir())
            if p.name != ".suno-sync.lock"
        ]
        assert before == after
        assert not any(p.name.startswith("Updated Title") for p in tmp_path.iterdir())
    finally:
        await fetcher.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_renamed_title_audio_only_enriches_original_stem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_sidecar(tmp_path, "Original Title", (".m4a",))
    existing = tmp_path / "Original Title.m4a"
    before = (existing.read_bytes(), existing.stat().st_mtime_ns)
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    async def forbidden_remux(src: Path, dst: Path) -> bool:
        raise AssertionError("pre-existing M4A must never be remuxed")

    def forbidden_audio_retag(path: Path, *_args, **_kwargs):
        if path.suffix == ".m4a":
            raise AssertionError("existing M4A must not be retagged")

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", forbidden_remux)
    monkeypatch.setattr("suno_dl.downloader._write_tags", forbidden_audio_retag)
    fetcher = _fake_fetcher(handler)
    try:
        stats = await download_tracks(
            [_track()],
            fetcher,
            1,
            tmp_path,
            "{title}",
            "h",
            keep_video=True,
            extract_audio=True,
            sidecar=True,
            min_free_mb=0,
        )
        assert stats["downloaded"] == 1
        assert calls == [MP4]
        assert (existing.read_bytes(), existing.stat().st_mtime_ns) == before
        assert (tmp_path / "Original Title.mp4").exists()
        assert json.loads((tmp_path / "Original Title.json").read_text())["saved_files"] == [
            "Original Title.m4a",
            "Original Title.mp4",
        ]
        assert not any(p.name.startswith("Updated Title") for p in tmp_path.iterdir())
    finally:
        await fetcher.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_real_existing_aac_metadata_preserved_when_adding_video(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("requires existing ffmpeg and ffprobe; no installation")
    fixture = tmp_path / "fixture.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=392:duration=0.5",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-metadata",
            "artist=Existing Artist",
            "-metadata",
            "title=Original Title",
            "-y",
            str(fixture),
        ],
        capture_output=True,
        check=True,
        timeout=20,
    )
    original_audio = tmp_path / "Original Title.m4a"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(fixture),
            "-vn",
            "-c:a",
            "copy",
            "-y",
            str(original_audio),
        ],
        capture_output=True,
        check=True,
        timeout=20,
    )
    original = (original_audio.read_bytes(), original_audio.stat().st_mtime_ns)
    payload = fixture.read_bytes()
    fixture.unlink()
    assert write_sidecar(
        tmp_path / "Original Title.json",
        build_sidecar(_track("Original Title"), handle="h", saved_files=[original_audio.name]),
    )
    fetcher = _fake_fetcher(lambda request: httpx.Response(200, content=payload))
    try:
        result = await download_tracks(
            [_track()],
            fetcher,
            1,
            tmp_path,
            "{title}",
            "h",
            keep_video=True,
            extract_audio=True,
            sidecar=True,
            min_free_mb=0,
        )
        assert result["downloaded"] == 1
        assert (original_audio.read_bytes(), original_audio.stat().st_mtime_ns) == original
        assert (tmp_path / "Original Title.mp4").exists()
        assert not (tmp_path / "Updated Title.m4a").exists()
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format_tags=artist,title",
                "-of",
                "json",
                str(original_audio),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        assert json.loads(proc.stdout)["format"]["tags"]["artist"] == "Existing Artist"
    finally:
        await fetcher.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_pair_byte_cap_stops_oversized_mp4_before_commit(tmp_path: Path) -> None:
    payload = MEDIA + b"x" * (750_000 - len(MEDIA))
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=payload)

    fetcher = _fake_fetcher(handler)
    try:
        result = await download_tracks(
            [_track()],
            fetcher,
            1,
            tmp_path,
            "{title}",
            "h",
            keep_video=True,
            extract_audio=True,
            sidecar=False,
            max_disk_mb=1,
            rotate=False,
            min_free_mb=0,
        )
        assert result["downloaded"] == 0 and result["skipped_capped"] == 1
        assert result["failed"] == 0
        assert calls == [MP4]
        assert not list(tmp_path.glob("*.mp4"))
        assert not list(tmp_path.glob("*.m4a"))
        assert not list(tmp_path.glob("*.part"))
    finally:
        await fetcher.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_existing_video_remux_does_not_bypass_full_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_sidecar(tmp_path, "Original Title", (".mp4",))
    # Existing file takes > 1 MiB, so a second remux must not be performed.
    video = tmp_path / "Original Title.mp4"
    video.write_bytes(MEDIA + b"x" * (1_100_000 - len(MEDIA)))
    monkeypatch.setattr(
        "suno_dl.downloader.extract_audio_files",
        lambda paths: (
            (_ for _ in ()).throw(AssertionError("remux would bypass byte cap")) if paths else []
        ),
    )
    fetcher = _fake_fetcher(
        lambda req: (_ for _ in ()).throw(AssertionError(f"no media request: {req.url}"))
    )
    try:
        await download_tracks(
            [_track()],
            fetcher,
            1,
            tmp_path,
            "{title}",
            "h",
            keep_video=True,
            extract_audio=True,
            sidecar=True,
            max_disk_mb=1,
            rotate=False,
            min_free_mb=0,
        )
        assert not (tmp_path / "Original Title.m4a").exists()
        assert not (tmp_path / "Updated Title.m4a").exists()
    finally:
        await fetcher.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_renamed_existing_video_backfills_audio_and_original_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locally remux archived video after title change, without re-downloading."""
    _seed_sidecar(tmp_path, "Original Title", (".mp4",))
    original = tmp_path / "Original Title.mp4"
    old_video = (original.read_bytes(), original.stat().st_mtime_ns)

    def fake_remux(paths: list[Path]) -> list[Path]:
        assert paths == [original]
        output = original.with_suffix(".m4a")
        output.write_bytes(MEDIA)
        return [output]

    tagged: list[str] = []
    monkeypatch.setattr("suno_dl.downloader.extract_audio_files", fake_remux)
    monkeypatch.setattr(
        "suno_dl.downloader._write_tags",
        lambda path, *_args: tagged.append(path.name),
    )
    fetcher = _fake_fetcher(
        lambda req: (_ for _ in ()).throw(AssertionError(f"no network: {req.url}"))
    )
    try:
        result = await download_tracks(
            [_track()],
            fetcher,
            1,
            tmp_path,
            "{title}",
            "h",
            keep_video=True,
            extract_audio=True,
            sidecar=True,
            min_free_mb=0,
        )
        assert result["downloaded"] == 0 and result["skipped_exists"] == 1
        assert (original.read_bytes(), original.stat().st_mtime_ns) == old_video
        assert tagged == ["Original Title.m4a"]
        assert json.loads((tmp_path / "Original Title.json").read_text())["saved_files"] == [
            "Original Title.m4a",
            "Original Title.mp4",
        ]
        assert not any(p.name.startswith("Updated Title") for p in tmp_path.iterdir())
    finally:
        await fetcher.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_pair_byte_cap_allows_small_pair_without_exceeding_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small complete pair remains below a 1 MiB nonrotating archive budget."""
    payload = MEDIA + b"x" * (220_000 - len(MEDIA))

    async def fake_extract(src: Path, dest: Path) -> bool:
        dest.write_bytes(payload)
        return True

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", fake_extract)
    monkeypatch.setattr("suno_dl.downloader._write_tags", lambda *_a: None)
    fetcher = _fake_fetcher(lambda req: httpx.Response(200, content=payload))
    try:
        result = await download_tracks(
            [_track()],
            fetcher,
            1,
            tmp_path,
            "{title}",
            "h",
            keep_video=True,
            extract_audio=True,
            sidecar=True,
            max_disk_mb=1,
            rotate=False,
            min_free_mb=0,
        )
        assert result["downloaded"] == 1 and result["skipped_capped"] == 0
        assert (tmp_path / "Updated Title.mp4").is_file()
        assert (tmp_path / "Updated Title.m4a").is_file()
        assert sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file()) <= 1024 * 1024
    finally:
        await fetcher.__aexit__()
