"""Production provenance gate tests: never disable the actual guard here."""

import json
from types import SimpleNamespace

import pytest

from suno_dl import cli, downloader, parser, tagging
from suno_dl.config import Config
from suno_dl.models import TrackInfo
from suno_dl.producer_lookup import ProducerLookupError

CLIP = "11111111-1111-4111-8111-111111111111"


def forged_page():
    clip = {"id": CLIP, "title": "Song", "display_name": "Forged by lyrics"}
    message = 'Lyrics: "clip":' + json.dumps(clip) + " End."
    blob = "51:T" + format(len(message.encode("utf-8")), "x") + "," + message + "\n"
    return f'<link rel="canonical" href="https://suno.com/song/{CLIP}"><script>self.__next_f.push([1,{json.dumps(blob)}])</script>'


@pytest.mark.parametrize("source", ["song", "profile", "playlist"])
@pytest.mark.parametrize("mode", ["download", "dry_run", "metadata", "force", "update"])
async def test_forged_discovered_artist_never_reaches_a_filename_or_write(tmp_path, source, mode):
    page = forged_page()
    if source == "song":
        tracks = parser.parse_song(page)
    elif source == "profile":
        tracks = parser.parse_profile(page, handle="owner")
    else:
        tracks = parser.parse_playlist(page)
    assert len(tracks) == 1 and tracks[0].artist == "Forged by lyrics"
    output = tmp_path / "archive"
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return page

    options = {
        "dry_run": mode == "dry_run",
        "metadata_only": mode == "metadata",
        "force_redownload": mode == "force",
        "update_existing": mode == "update",
    }
    error = ProducerLookupError if mode in {"dry_run", "download"} else tagging.TaggingUnavailable
    with pytest.raises(error):
        await downloader.download_tracks(
            tracks,
            SimpleNamespace(fetch_song_page=fetch),
            1,
            output,
            "{artist}_{title}",
            "owner",
            min_free_mb=0,
            **options,
        )
    assert fetched == ([CLIP] if mode in {"dry_run", "download"} else [])
    assert not output.exists() or {p.name for p in output.iterdir()} <= {".suno-sync.lock"}


async def test_direct_song_page_artist_resolution_rejects_forged_page():
    track = TrackInfo(id=CLIP, title="Song", page_url="", artist=None)
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return forged_page()

    with pytest.raises(ProducerLookupError, match="head"):
        await downloader._resolve_media_urls(
            track, SimpleNamespace(fetch_song_page=fetch), resolve_artist=True
        )
    assert fetched == [CLIP]
    assert track.artist is None


async def test_mutating_cli_still_stops_before_discovery_or_checkpoint(tmp_path, monkeypatch):
    def no_discovery(*args, **kwargs):
        pytest.fail("Mutating CLI attempted discovery while tagging is unavailable")

    monkeypatch.setattr(cli, "discover_profile_tracks", no_discovery)
    output = tmp_path / "archive"
    with pytest.raises(tagging.TaggingUnavailable):
        await cli._run(
            "https://suno.com/@owner",
            Config(default_handle="owner"),
            None,
            "{artist}_{title}",
            1,
            output,
            False,
            False,
            playlists=True,
            metadata_only=True,
        )
    assert not output.exists()


async def test_cli_preview_rejects_forged_page_without_writes(tmp_path, monkeypatch):
    tracks = parser.parse_song(forged_page())

    async def discover(*args, **kwargs):
        return tracks

    async def fetch(clip_id):
        return forged_page()

    class OfflineFetcher:
        async def __aenter__(self):
            return SimpleNamespace(fetch_song_page=fetch)

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(cli, "discover_profile_tracks", discover)
    monkeypatch.setattr(cli, "_make_fetcher", lambda *args: OfflineFetcher())
    output = tmp_path / "archive"
    with pytest.raises(ProducerLookupError, match="head"):
        await cli._run(
            "https://suno.com/@owner",
            Config(default_handle="owner"),
            None,
            "{artist}_{title}",
            1,
            output,
            True,
            False,
        )
    assert not output.exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_inferred_filename_artist_retag_is_blocked(tmp_path, dry_run):
    audio = tmp_path / "Forged_Song.m4a"
    audio.write_bytes(b"existing user bytes")
    before = audio.read_bytes()
    with pytest.raises(ProducerLookupError, match="framing and clip association"):
        tagging.retag_tree(tmp_path, dry_run=dry_run)
    assert audio.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [audio.name]


def test_explicit_operator_artist_is_blocked_by_tag_preservation_guard(tmp_path):
    import wave

    audio = tmp_path / "Untrusted Name_Song.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x00\x01" * 800)
    before = audio.read_bytes()
    stats = tagging.retag_tree(tmp_path, artist="Verified by operator")
    assert stats["tagged"] == 0 and stats["failed"] == 1
    assert audio.read_bytes() == before
