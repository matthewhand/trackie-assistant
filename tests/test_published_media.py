"""Only explicit own-clip sources can be transferred; encoded streams are skipped."""

import json

import pytest

from suno_dl.published_media import published_sources
from tests.test_cover_art_publication import image as image
from tests.test_producer_lookup import ID, OTHER, page


def own_page(**changes):
    data = {
        "id": ID,
        "title": "A song",
        "entity_type": "song_schema",
        "is_public": True,
        "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
        "video_url": "https://cdn1.suno.ai/" + ID + ".mp4",
        "media_urls": [
            {"url": "https://example.cloudfront.net/" + ID + ".m4a", "encoding": "1.0.0"}
        ],
        "metadata": {"duration": 251.2},
    }
    data.update(changes)
    flight = "0:" + json.dumps({"clip": data}) + chr(10)
    return page(
        body="<script>self.__next_f.push("
        + json.dumps([1, flight], separators=(",", ":"))
        + ")</script>"
    )


def test_encoded_audio_is_not_downloaded_and_published_video_is_bound_to_own_uuid():
    urls, track = published_sources(own_page(), ID)
    assert urls == ["https://cdn1.suno.ai/" + ID + ".mp4"]
    assert track.duration_sec == 252


@pytest.mark.parametrize(
    "video",
    [
        "https://evil.cloudfront.net/" + ID + ".mp4",
        "https://cdn1.suno.ai/" + OTHER + ".mp4",
        "https://cdn1.suno.ai/" + ID + ".mp4?other=1",
        "http://cdn1.suno.ai/" + ID + ".mp4",
    ],
)
def test_unbound_video_does_not_become_a_source(video):
    urls, _ = published_sources(own_page(video_url=video), ID)
    assert urls == []


def test_dom_without_media_does_not_invent_cdn_addresses():
    html = page().replace(
        '<meta property="og:audio" content="https://cdn1.suno.ai/' + ID + '.m4a">', ""
    )
    assert published_sources(html, ID)[0] == []


def test_explicit_dom_audio_remains_supported():
    assert published_sources(page(), ID)[0] == ["https://cdn1.suno.ai/" + ID + ".m4a"]


def test_nonpublic_clip_and_wrong_canonical_are_rejected():
    with pytest.raises(ValueError):
        published_sources(own_page(is_public=False), ID)
    with pytest.raises(ValueError):
        published_sources(page(clip_id=OTHER), ID)


def test_plain_published_audio_precedes_video():
    url = "https://cdn1.suno.ai/" + ID + ".mp3"
    urls, _ = published_sources(own_page(audio_url=url, media_urls=[]), ID)
    assert urls == [url, "https://cdn1.suno.ai/" + ID + ".mp4"]


@pytest.mark.parametrize("cover_available", [True, False])
async def test_published_video_extracts_audio_and_cover_without_retaining_video(
    tmp_path, mock_http, image, cover_available
):
    import subprocess

    import httpx
    from mutagen.mp4 import MP4

    from suno_dl.downloader import download_tracks
    from suno_dl.fetcher import Fetcher
    from suno_dl.models import TrackInfo
    from suno_dl.verified_sync import packet_signature

    video = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=64x64:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    image_bytes, _ = image
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.host == "suno.com":
            return httpx.Response(
                200, text=own_page(image_large_url="https://cdn2.suno.ai/cover.jpeg")
            )
        if request.url.path.endswith(".mp4"):
            return httpx.Response(200, content=video.read_bytes())
        if request.url.host == "cdn2.suno.ai":
            return httpx.Response(200 if cover_available else 404, content=image_bytes)
        raise AssertionError("Unpublished or encoded source requested")

    mock_http(handler)
    root = tmp_path / "library"
    async with Fetcher(10, 0) as fetcher:
        stats = await download_tracks(
            [TrackInfo(id=ID, title="", page_url="https://suno.com/song/" + ID)],
            fetcher,
            1,
            root,
            "{artist}_{title}",
            "owner",
            min_free_mb=0,
        )
    assert stats["downloaded"] == 1
    audio = next(root.glob("*.m4a"))
    if cover_available:
        assert MP4(audio)["covr"] == [image_bytes]
    else:
        assert bytes(MP4(audio)["covr"][0]).startswith(bytes.fromhex("ffd8ff"))
    assert sum(url.endswith(".mp4") for url in requested) == 1
    assert packet_signature(audio) == packet_signature(video)
    assert not list(root.glob("*.mp4"))
    assert all("cloudfront" not in url for url in requested)
