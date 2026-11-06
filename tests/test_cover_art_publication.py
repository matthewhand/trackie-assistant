"""Real image/audio checks for default Linux cover publication and MA detection."""

from __future__ import annotations

import json
import struct
import subprocess
import zlib
from pathlib import Path

import httpx
import pytest
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from suno_dl import downloader
from suno_dl.fetcher import Fetcher
from suno_dl.lossless_mp4 import UnsupportedMP4, write_isolated_mp4
from suno_dl.models import TrackInfo
from suno_dl.verified_sync import packet_signature
from tests.test_producer_lookup import ID, page

AUDIO = Path(__file__).parent / "fixtures/lossless-native/sample.m4a"


@pytest.fixture(params=["png", "jpeg"])
def image(request, tmp_path):
    # Generate a valid RGB PNG and (for JPEG) convert only the synthetic image.
    path = tmp_path / "fixture.png"

    # Real binary scanlines, not escaped text.
    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    data = (
        bytes.fromhex("89504e470d0a1a0a")
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes([0, 255, 0, 0, 255, 0, 0]) * 2))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(data)
    if request.param == "jpeg":
        target = tmp_path / "fixture.jpg"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-frames:v", "1", str(target)],
            check=True,
            capture_output=True,
        )
        data = target.read_bytes()
    return data, MP4Cover.FORMAT_PNG if request.param == "png" else MP4Cover.FORMAT_JPEG


async def test_default_public_sync_publishes_cover_embedded_and_as_image(
    tmp_path, mock_http, image
):
    data, image_format = image
    root = tmp_path / "library"
    requests = []
    cover_url = "https://cdn2.suno.ai/image_" + ID + ".jpeg"

    def handler(request):
        requests.append(str(request.url))
        if request.url.host == "suno.com":
            html = page().replace(
                "</head>", '<meta property="og:image" content="' + cover_url + '"></head>'
            )
            return httpx.Response(200, text=html)
        if str(request.url) == cover_url:
            return httpx.Response(200, content=data)
        return httpx.Response(200, content=AUDIO.read_bytes())

    mock_http(handler)
    async with Fetcher(10, 0) as fetcher:
        stats = await downloader.download_tracks(
            [TrackInfo(page_url="https://suno.com/song/" + ID, id=ID, title="ignored")],
            fetcher,
            1,
            root,
            "{artist}_{title}",
            "owner",
            min_free_mb=0,
            extract_audio=True,
        )
    assert stats["downloaded"] == 1
    audio = next(root.glob("*.m4a"))
    cover = next(root.glob("*.cover.*"))
    assert cover.read_bytes() == data
    tags = MP4(audio)
    assert tags["covr"] == [data]
    assert tags["covr"][0].imageformat == image_format
    assert tags["©ART"] == ["Actual Producer"]
    assert packet_signature(audio) == packet_signature(AUDIO)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(audio)],
        capture_output=True,
        check=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    # This is the marker Music Assistant uses to find embedded artwork.
    assert any(s.get("disposition", {}).get("attached_pic") == 1 for s in streams)
    assert not list(root.glob("*.mp4"))
    assert not any(url.endswith(".mp4") for url in requests)
    assert cover_url in requests
    # Repeated sync must not download media/artwork again or alter indexed files.
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    requests.clear()
    async with Fetcher(10, 0) as fetcher:
        stats = await downloader.download_tracks(
            [TrackInfo(page_url="https://suno.com/song/" + ID, id=ID, title="ignored")],
            fetcher,
            1,
            root,
            "{artist}_{title}",
            "owner",
            min_free_mb=0,
            extract_audio=True,
        )
    assert stats["downloaded"] == 0 and requests == []
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before


def test_isolated_cover_keeps_opaque_metadata_and_audio(tmp_path, image):
    data, image_format = image
    audio = tmp_path / "private.m4a"
    audio.write_bytes(AUDIO.read_bytes())
    tags = MP4(audio)
    key = "----:org.example.test:opaque"
    opaque = b"opaque test payload\x00\xff"
    tags[key] = [MP4FreeForm(opaque)]
    tags.save()
    before = packet_signature(audio)
    write_isolated_mp4(audio, {}, cover=data)
    tags = MP4(audio)
    assert bytes(tags[key][0]) == opaque
    assert bytes(tags["covr"][0]) == data
    assert tags["covr"][0].imageformat == image_format
    assert packet_signature(audio) == before
    # Artist-only migration must preserve the image byte for byte.
    write_isolated_mp4(audio, {bytes.fromhex("a9415254"): "Corrected Producer"})
    assert bytes(MP4(audio)["covr"][0]) == data


@pytest.mark.parametrize("payload", [b"not an image", b"\x00\x00\x00\x18ftypmp42"])
async def test_video_or_invalid_cover_is_not_published(tmp_path, mock_http, payload):
    from suno_dl.linux_downloads import _stage_cover

    def handler(_request):
        return httpx.Response(200, content=payload)

    mock_http(handler)
    track = TrackInfo(
        page_url="https://suno.com/song/" + ID,
        id=ID,
        title="T",
        cover_url="https://cdn2.suno.ai/" + ID + ".jpg",
    )
    async with Fetcher(10, 0) as fetcher:
        result = await _stage_cover(track, tmp_path / "image", tmp_path, fetcher)
    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_bad_cover_refuses_before_altering_private_audio(tmp_path):
    audio = tmp_path / "private.m4a"
    before = AUDIO.read_bytes()
    audio.write_bytes(before)
    with pytest.raises(UnsupportedMP4):
        write_isolated_mp4(audio, {}, cover=b"not an image")
    assert audio.read_bytes() == before
