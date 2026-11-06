"""Metadata reconciliation: --metadata-only, --update-existing and --dry-run (#159).

Tagging is stubbed in the writes-it test so the assertions are about *which*
metadata is proposed and persisted; a separate case uses the real tagger on
non-media bytes to prove a tagging failure leaves the sidecar untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from suno_dl import metadata as metadata_mod
from suno_dl.cli import main
from suno_dl.downloader import download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo
from suno_dl.sidecar import build_sidecar, write_sidecar
from suno_dl.tagging import TagResult

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
OTHER = "b2c3d4e5-2222-4222-8222-222222222222"
MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048


def _track(clip_id: str = CLIP, title: str = "New Title", artist: str | None = "SmileDJ"):
    url = f"https://cdn1.suno.ai/{clip_id}.m4a"
    return TrackInfo(
        id=clip_id,
        title=title,
        artist=artist,
        page_url=f"https://suno.com/song/{clip_id}",
        audio_url=url,
        media_urls=[url],
        duration_sec=200,
        tags="chipfunk",
    )


def _seed(folder: Path, clip_id: str = CLIP, stem: str = "drinko_Old Title") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / f"{stem}.m4a"
    audio.write_bytes(MEDIA)
    old = _track(clip_id, title="Old Title", artist="drinko")
    assert write_sidecar(
        folder / f"{stem}.json",
        build_sidecar(old, handle="drinko", saved_files=[audio.name]),
    )
    return audio


def _fetcher(handler=None) -> Fetcher:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"metadata mode must not request media: {request.url}")

    fetcher = Fetcher(timeout_sec=1.0, max_retries=1)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler or refuse))
    return fetcher


def _record_tag_calls(monkeypatch) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []

    def fake_tag(path, *, artist, title, lyrics=None):
        calls.append((path.name, artist, title))
        return TagResult(True, "fake", artist, title, lyrics)

    monkeypatch.setattr(metadata_mod, "tag_file", fake_tag)
    return calls


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_metadata_only_repairs_tags_and_sidecar_without_a_transfer(
    tmp_path: Path, monkeypatch
) -> None:
    audio = _seed(tmp_path)
    calls = _record_tag_calls(monkeypatch)

    stats = await download_tracks(
        [_track()],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        metadata_only=True,
        source="drinko",
    )
    assert stats["metadata_updated"] == 1
    assert stats["downloaded"] == 0
    assert calls == [("drinko_Old Title.m4a", "SmileDJ", "New Title")]

    sidecar = json.loads((tmp_path / "drinko_Old Title.json").read_text(encoding="utf-8"))
    assert sidecar["artist"] == "SmileDJ"
    assert sidecar["title"] == "New Title"
    assert sidecar["saved_files"] == [audio.name]
    assert audio.read_bytes() == MEDIA


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_metadata_only_dry_run_reports_and_writes_nothing(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    stats = await download_tracks(
        [_track()],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        metadata_only=True,
        dry_run=True,
    )
    assert stats["metadata_updated"] == 1
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert after == before
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "artist" in out


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_unknown_producer_never_overwrites_existing_attribution(
    tmp_path: Path, monkeypatch
) -> None:
    _seed(tmp_path)
    calls = _record_tag_calls(monkeypatch)

    stats = await download_tracks(
        [_track(artist=None)],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        metadata_only=True,
    )
    assert stats["metadata_unresolved"] == 1
    # Only the title changed; the artist tag stays the one already archived.
    assert calls == [("drinko_Old Title.m4a", "drinko", "New Title")]
    sidecar = json.loads((tmp_path / "drinko_Old Title.json").read_text(encoding="utf-8"))
    assert sidecar["artist"] == "drinko"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_update_existing_repairs_owned_and_downloads_missing(
    tmp_path: Path, monkeypatch
) -> None:
    _seed(tmp_path)
    _record_tag_calls(monkeypatch)
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, content=MEDIA)

    stats = await download_tracks(
        [_track(), _track(OTHER, title="Brand New")],
        _fetcher(handler),
        1,
        tmp_path,
        "{title}",
        "drinko",
        update_existing=True,
    )
    assert stats["metadata_updated"] == 1
    assert stats["downloaded"] == 1
    assert fetched == [f"https://cdn1.suno.ai/{OTHER}.m4a"]
    assert (tmp_path / "Brand New.m4a").is_file()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_metadata_only_reports_missing_clip_without_requesting(tmp_path: Path) -> None:
    stats = await download_tracks(
        [_track(OTHER)],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        metadata_only=True,
    )
    assert stats["metadata_missing"] == 1
    assert stats["metadata_updated"] == 0
    assert not list(tmp_path.glob("*.m4a"))


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_tagging_failure_keeps_sidecar_and_audio_unchanged(tmp_path: Path) -> None:
    audio = _seed(tmp_path)
    sidecar = tmp_path / "drinko_Old Title.json"
    before_sidecar = sidecar.read_bytes()

    stats = await download_tracks(
        [_track()],
        _fetcher(),
        1,
        tmp_path,
        "{title}",
        "drinko",
        metadata_only=True,
    )
    assert stats["metadata_failed"] == 1
    assert stats["metadata_updated"] == 0
    assert sidecar.read_bytes() == before_sidecar
    assert audio.read_bytes() == MEDIA


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_second_pass_is_a_no_op(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path)
    _record_tag_calls(monkeypatch)
    first = await download_tracks(
        [_track()], _fetcher(), 1, tmp_path, "{title}", "drinko", metadata_only=True
    )
    second = await download_tracks(
        [_track()], _fetcher(), 1, tmp_path, "{title}", "drinko", metadata_only=True
    )
    assert first["metadata_updated"] == 1
    assert second["metadata_updated"] == 0
    assert second["metadata_unchanged"] == 1


def test_flag_conflicts_are_not_masked_by_a_missing_default_handle(tmp_path: Path) -> None:
    """A bad combination must win over config errors, not be hidden by them."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--url", "https://suno.com/@drinko", "--metadata-only", "--force-redownload"],
        env={"SUNO_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code != 0
    assert "never fetches media" in result.output
    assert "SUNO_DEFAULT_HANDLE" not in result.output


def test_cli_rejects_metadata_only_without_sidecars(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--url", "https://suno.com/@drinko", "--metadata-only", "--no-sidecar"],
        env={"SUNO_DEFAULT_HANDLE": "drinko", "SUNO_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code != 0
    assert "--metadata-only" in result.output


def test_cli_rejects_metadata_only_with_force_redownload(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--url", "https://suno.com/@drinko", "--metadata-only", "--force-redownload"],
        env={"SUNO_DEFAULT_HANDLE": "drinko", "SUNO_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.exit_code != 0
    assert "never fetches media" in result.output


@pytest.mark.parametrize("flag", ["--metadata-only", "--update-existing", "--force-redownload"])
def test_cli_flags_are_documented(flag: str) -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert flag in result.output
