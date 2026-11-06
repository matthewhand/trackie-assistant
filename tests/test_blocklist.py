"""Tests for the track blocklist: parsing, config wiring, and skip behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from suno_dl.blocklist import load_blocked_ids
from suno_dl.config import load_config
from suno_dl.downloader import download_tracks
from suno_dl.errors import ConfigError
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo

CLIP_BLOCKED = "7c68eb8d-4bad-43d5-8d37-8be1fd48fb58"
CLIP_OK = "a1b2c3d4-1111-4111-8111-111111111111"

_VALID_MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048


def _track(clip: str, title: str) -> TrackInfo:
    return TrackInfo(
        id=clip,
        title=title,
        page_url=f"https://suno.com/song/{clip}",
        audio_url=f"https://cdn1.suno.ai/{clip}.m4a",
        media_urls=[f"https://cdn1.suno.ai/{clip}.m4a"],
    )


# ─────────────────────────────── parsing ───────────────────────────────


def test_load_blocked_ids_parses_comments_blanks_and_case(tmp_path: Path) -> None:
    file = tmp_path / ".blocked"
    file.write_text(
        "# Spring Has No Alibi — 403 for anonymous fetches; get it via Suno's UI\n"
        f"{CLIP_BLOCKED}\n"
        "\n"
        f"  {CLIP_OK.upper()}  # uppercase spelling must normalize\n",
        encoding="utf-8",
    )
    assert load_blocked_ids(file) == frozenset({CLIP_BLOCKED, CLIP_OK})


def test_load_blocked_ids_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_blocked_ids(tmp_path / "nope") == frozenset()


def test_load_blocked_ids_rejects_malformed_lines(tmp_path: Path) -> None:
    file = tmp_path / ".blocked"
    file.write_text(f"{CLIP_BLOCKED}\nnot-a-uuid\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not-a-uuid"):
        load_blocked_ids(file)


def test_blocklist_file_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "my-blocked.txt"
    custom.write_text(f"{CLIP_BLOCKED}\n", encoding="utf-8")
    monkeypatch.setenv("SUNO_BLOCKED_FILE", str(custom))
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    cfg = load_config()
    assert cfg.blocked_file == custom
    assert load_blocked_ids(cfg.blocked_file) == frozenset({CLIP_BLOCKED})


# ─────────────────────────── downloader wiring ───────────────────────────


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
def test_download_tracks_skips_blocked_before_any_request(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=_VALID_MEDIA)

    fetcher = Fetcher(timeout_sec=1.0, max_retries=1)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    stats = asyncio.run(
        download_tracks(
            [_track(CLIP_BLOCKED, "Spring Has No Alibi"), _track(CLIP_OK, "Other Song")],
            fetcher,
            concurrency=1,
            output_dir=tmp_path,
            pattern="{handle}_{title}",
            handle="drinko",
            blocked_ids=frozenset({CLIP_BLOCKED}),
        )
    )
    assert stats["skipped_blocked"] == 1
    assert stats["downloaded"] == 1
    assert stats["failed"] == 0
    # The blocked object was never touched — that is the whole point.
    assert requested and all(CLIP_BLOCKED not in url for url in requested)


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
def test_download_tracks_dry_run_respects_blocklist(tmp_path: Path) -> None:
    stats = asyncio.run(
        download_tracks(
            [_track(CLIP_BLOCKED, "Spring Has No Alibi")],
            Fetcher(timeout_sec=1.0, max_retries=1),
            concurrency=1,
            output_dir=tmp_path,
            pattern="{handle}_{title}",
            handle="drinko",
            dry_run=True,
            blocked_ids=frozenset({CLIP_BLOCKED}),
        )
    )
    assert stats["skipped_blocked"] == 1
    assert stats["dry_run"] == 0  # nothing left to list
