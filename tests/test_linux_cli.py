"""Real Linux CLI entry point with mocked canonical HTTP and real synthetic audio."""

import json
from pathlib import Path

import httpx
from click.testing import CliRunner
from mutagen.mp4 import MP4

from suno_dl.cli import main
from tests.test_producer_lookup import ID, OTHER, page


def test_cli_never_selects_other_uuid_from_a_song_page(tmp_path, monkeypatch, mock_http):
    monkeypatch.chdir(tmp_path)
    for name in (
        "SUNO_WATCH",
        "SUNO_ROTATE",
        "SUNO_MAX_TRACKS",
        "SUNO_MAX_DISK_MB",
        "SUNO_FORMAT",
        "SUNO_PLAYLISTS",
        "SUNO_PATTERN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "owner")
    monkeypatch.setenv("SUNO_SIDECAR", "1")
    requests = []
    payload = (Path(__file__).parent / "fixtures/lossless-native/sample.m4a").read_bytes()
    forged = (
        '1:{"clip":'
        + json.dumps(
            {
                "id": OTHER,
                "title": "Wrong song",
                "display_name": "Wrong producer",
                "audio_url": "https://cdn1.suno.ai/" + OTHER + ".m4a",
            }
        )
        + "}"
    )
    body = "<script>self.__next_f.push([1," + json.dumps(forged) + "])</script>"

    def handler(request):
        requests.append(str(request.url))
        if request.url.host == "suno.com":
            return httpx.Response(200, text=page(body=body))
        return httpx.Response(200, content=payload)

    mock_http(handler)
    result = CliRunner().invoke(
        main,
        [
            "--url",
            "https://suno.com/song/" + ID,
            "--output-dir",
            str(tmp_path / "library"),
            "--flat",
            "--extract-audio",
            "--no-browser",
            "--no-covers",
            "--no-playback-paced",
            "--min-free-mb",
            "0",
            "--rate-limit",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    assert all(OTHER not in url for url in requests)
    audio = tmp_path / "library/Actual Producer_A song.m4a"
    assert MP4(audio)["\xa9ART"] == ["Actual Producer"]
    side = json.loads(audio.with_suffix(".json").read_text())
    assert side["id"] == ID and side["creator_handle"] == "actual"
    assert "downloaded=1" in result.output and "tagged=1" in result.output


async def test_playlist_cli_uses_real_public_api_transport_and_keeps_public_rows(
    monkeypatch, mock_http
):
    from suno_dl import cli
    from suno_dl.fetcher import Fetcher

    requested = []
    payload = {
        "id": OTHER,
        "current_page": 1,
        "num_total_results": 2,
        "is_public": True,
        "is_hidden": False,
        "is_trashed": False,
        "playlist_clips": [
            {
                "clip": {
                    "id": clip_id,
                    "entity_type": "song_schema",
                    "title": "Song",
                    "is_public": public,
                    "is_hidden": False,
                    "is_trashed": False,
                }
            }
            for clip_id, public in [(ID, True), (OTHER, False)]
        ],
    }

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, json=payload)

    def no_html(*args):
        raise AssertionError("Unframed HTML cannot select playlist membership")

    mock_http(handler)
    monkeypatch.setattr(cli, "parse_playlist", no_html)
    async with Fetcher(10, 0) as fetcher:
        result = await cli._source_playlist_tracks(fetcher, "https://suno.com/playlist/" + OTHER)
    assert [t.id for t in result] == [ID]
    assert len(requested) == 1 and OTHER in requested[0]
