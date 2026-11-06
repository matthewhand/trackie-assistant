"""Production Linux sync regressions; no safety guards are disabled."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from mutagen.mp4 import MP4

from suno_dl.lossless_mp4 import UnsupportedMP4, _children, write_isolated_mp4
from suno_dl.producer_lookup import ProducerLookupError, producer_identity_from_page
from suno_dl.verified_sync import digest, durable_json, packet_signature, publish_intent

ID = "0b04ccf5-b80a-4ce4-80ae-ef9aa95cb0be"


def page(artist="KidJim", handle="kidjim"):
    return f"""<html><head>
    <title>Song by {artist} | Suno</title>
    <meta property="og:title" content="Song">
    <meta property="og:audio" content="https://cdn1.suno.ai/{ID}.m4a">
    <meta name="description"
    content="Song by {artist} (@{handle}). Listen and make your own on Suno.">
    <link rel="canonical" href="https://suno.com/song/{ID}">
    </head><body></body></html>"""


def test_head_producer_ignores_spoofed_lyrics_and_flight():
    html = page().replace(
        "</body>",
        '<script>self.__next_f.push([1,\'T{"id":"'
        + ID
        + '","display_name":"drinko"}\'])</script></body>',
    )
    assert producer_identity_from_page(ID, html).artist == "KidJim"


@pytest.mark.parametrize(
    "alter",
    [
        lambda x: x.replace(ID, "b34f55fe-b63e-4251-b6f8-18f59f564cce"),
        lambda x: x.replace("</head>", '<meta property="og:title" content="Other"></head>'),
        lambda x: x.replace("Song by KidJim |", "Song by Wrong |"),
        lambda x: x.replace("Song by KidJim (@", "Other by KidJim (@"),
        lambda x: x.replace("<head>", "<body>"),
    ],
)
def test_producer_rejects_ambiguous_or_mismatched_head(alter):
    with pytest.raises(ProducerLookupError):
        producer_identity_from_page(ID, alter(page()))


@pytest.fixture
def audio(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    p = tmp_path / "test.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.3",
            "-c:a",
            "aac",
            str(p),
        ],
        check=True,
    )
    tags = MP4(p)
    tags["\xa9cmt"] = ["opaque comment"]
    tags["\xa9ART"] = ["old"]
    tags["\xa9nam"] = ["Song"]
    tags.save()
    # Mutagen has known locale normalization: make a nonzero comment locale.
    blob = p.read_bytes()
    name = b"\xa9cmt"
    pos = blob.index(name)
    atom = bytearray(blob)
    atom[pos + 16 : pos + 20] = (42).to_bytes(4, "big")
    p.write_bytes(atom)
    return p


def test_real_mp4_preserves_audio_and_opaque_locale(audio):
    before = audio.read_bytes()
    packets = packet_signature(audio)
    title = MP4(audio).get("\xa9nam")
    write_isolated_mp4(audio, {b"\xa9ART": "Actual producer name much longer"})
    after = audio.read_bytes()
    assert MP4(audio)["\xa9ART"] == ["Actual producer name much longer"]
    assert MP4(audio).get("\xa9nam") == title
    assert packet_signature(audio) == packets
    # Every byte before moov remains at its exact original offset.
    moov = before.index(b"moov") - 4
    assert before[:moov] == after[:moov]
    cmt = before.index(b"\xa9cmt") - 4
    n = int.from_bytes(before[cmt : cmt + 4], "big")
    assert before[cmt : cmt + n] in after
    assert b"opaque comment" in after


def test_unsupported_layout_leaves_working_copy_unchanged(audio):
    data = audio.read_bytes()
    boxes = _children(data)
    moov = next(raw for k, raw in boxes if k == b"moov")
    moved = moov + b"".join(raw for k, raw in boxes if k != b"moov")
    audio.write_bytes(moved)
    with pytest.raises(UnsupportedMP4):
        write_isolated_mp4(audio, {b"\xa9ART": "New"})
    assert audio.read_bytes() == moved


def intent(root):
    out = root / "drinko"
    out.mkdir()
    stage = root / ".suno-staging" / "clip"
    stage.mkdir(parents=True)
    intents = root / ".library" / "verified-sync-intents"
    intents.mkdir(parents=True)
    audio = stage / "audio.m4a"
    audio.write_bytes(b"A" * 2048)
    side = stage / "sidecar.json"
    from suno_dl.models import TrackInfo
    from suno_dl.sidecar import build_sidecar

    side.write_text(
        json.dumps(
            build_sidecar(
                TrackInfo(
                    id=ID, title="Song", artist="KidJim", page_url="https://suno.com/song/" + ID
                ),
                handle="drinko",
                saved_files=["KidJim_Song.m4a"],
            )
        )
    )
    marker = intents / (ID + ".json")
    durable_json(
        marker,
        {
            "schema": 1,
            "id": ID,
            "profile": "drinko",
            "members": [
                {
                    "stage": p.relative_to(root).as_posix(),
                    "destination": "drinko/" + name,
                    "sha256": digest(p),
                    "inode": [p.stat().st_dev, p.stat().st_ino],
                }
                for p, name in [(audio, "KidJim_Song.m4a"), (side, "KidJim_Song.json")]
            ],
        },
    )
    return marker, out


def test_interrupted_pair_publication_recovers_without_duplicate(tmp_path, monkeypatch):
    marker, out = intent(tmp_path)
    real = os.link
    count = 0

    def interrupt(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("simulated process death between audio and sidecar")
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "link", interrupt)
    with pytest.raises(OSError):
        publish_intent(tmp_path, marker)
    assert marker.exists() and len(list(out.glob("*.m4a"))) == 1
    monkeypatch.setattr(os, "link", real)
    assert publish_intent(tmp_path, marker) == ID
    assert not marker.exists()
    assert len(list(out.glob("*.m4a"))) == 1 and len(list(out.glob("*.json"))) == 1
    assert next(out.glob("*.m4a")).stat().st_nlink == 1


def test_recovery_refuses_unowned_destination(tmp_path):
    marker, out = intent(tmp_path)
    other = out / "KidJim_Song.m4a"
    other.write_bytes(b"B" * 2048)
    with pytest.raises(ValueError, match="belongs to another"):
        publish_intent(tmp_path, marker)
    assert marker.exists() and other.read_bytes() == b"B" * 2048


def test_real_process_death_recovers_owned_pair(tmp_path):
    marker, out = intent(tmp_path)
    script = """
import os,sys
from pathlib import Path
from suno_dl.verified_sync import publish_intent
real=os.link
count=0
def crash(*a,**kw):
 global count
 count+=1
 if count==2: os._exit(77)
 return real(*a,**kw)
os.link=crash
publish_intent(Path(sys.argv[1]),Path(sys.argv[2]))
"""
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(marker)])
    assert child.returncode == 77
    assert marker.exists()
    assert publish_intent(tmp_path, marker) == ID
    assert len(list(out.glob("*.m4a"))) == 1


@pytest.mark.asyncio
async def test_end_to_end_download_tags_then_resume_without_second_transfer(
    tmp_path, audio, monkeypatch, mock_http
):
    import httpx

    from suno_dl import verified_sync as sync
    from suno_dl.models import TrackInfo

    library = tmp_path / "library"
    library.mkdir()
    audit = tmp_path / "audit.jsonl"
    payload = audio.read_bytes()
    transfers = []

    def handler(request):
        if request.url.host == "suno.com":
            return httpx.Response(200, text=page())
        transfers.append(str(request.url))
        return httpx.Response(200, content=payload)

    mock_http(handler)

    async def inv(*a):
        return [
            TrackInfo(id=ID, title="Wrong", artist="drinko", page_url="https://suno.com/song/" + ID)
        ], {"profile:drinko": [ID]}

    async def no_wait(*a):
        return 0

    monkeypatch.setattr(sync, "inventory", inv)
    monkeypatch.setattr(sync.PlaybackPacer, "wait", no_wait)
    real_publish = sync.publish_intent

    def interrupt_before_publication(*args):
        raise OSError("simulated death after preparing the durable intent")

    monkeypatch.setattr(sync, "publish_intent", interrupt_before_publication)
    with pytest.raises(OSError):
        await sync.run(library, "drinko", apply=True, audit=audit, min_free_mb=2048)
    assert not list((library / "drinko").glob("*.m4a"))
    # Even a scanner that enters hidden directories cannot index staged audio.
    assert not list((library / ".suno-staging").rglob("*.m4a"))
    monkeypatch.setattr(sync, "publish_intent", real_publish)
    await sync.run(library, "drinko", apply=True, audit=audit, min_free_mb=2048)
    output = library / "drinko" / "KidJim_Song.m4a"
    assert output.exists()
    assert MP4(output)["\xa9ART"] == ["KidJim"]
    assert MP4(output)["\xa9nam"] == ["Song"]
    data = json.loads(output.with_suffix(".json").read_text())
    assert data["artist"] == "KidJim" and data["creator_handle"] == "kidjim"
    source_comment = payload.index(b"\xa9cmt") - 4
    comment_size = int.from_bytes(payload[source_comment : source_comment + 4], "big")
    assert payload[source_comment : source_comment + comment_size] in output.read_bytes()
    original = output.read_bytes()
    first_count = len(transfers)
    await sync.run(library, "drinko", apply=True, audit=audit, min_free_mb=2048)
    assert len(transfers) == first_count
    assert output.read_bytes() == original
    assert len(list((library / "drinko").glob("*.m4a"))) == 1


@pytest.mark.asyncio
async def test_sync_startup_recovers_partial_pair_before_ownership_check(tmp_path, monkeypatch):
    from suno_dl import verified_sync as sync
    from suno_dl.models import TrackInfo

    marker, out = intent(tmp_path)
    d = json.loads(marker.read_text())
    first = d["members"][0]
    os.link(tmp_path / first["stage"], tmp_path / first["destination"])

    async def inv(*args):
        return [TrackInfo(id=ID, title="Song", page_url="https://suno.com/song/" + ID)], {
            "profile:drinko": [ID]
        }

    monkeypatch.setattr(sync, "inventory", inv)
    await sync.run(tmp_path, "drinko", apply=True, audit=tmp_path / "audit.jsonl", min_free_mb=2048)
    assert not marker.exists()
    assert len(list(out.glob("*.m4a"))) == 1
    assert len(list(out.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_private_playlist_row_is_excluded_without_losing_public_rows():
    from suno_dl.playlist_api import PublicPlaylistApiError
    from suno_dl.playlists import PlaylistRef, _fetch_public_api_membership

    other = "9a1475cf-ecf1-4811-80e9-8d4bc956787e"
    ref = PlaylistRef(id=ID, name="Mixed", track_count=2)

    async def api(*args):
        return {
            "id": ID,
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
                        "is_public": public,
                        "is_hidden": False,
                        "is_trashed": False,
                        "title": "Song",
                        "display_name": "KidJim",
                        "handle": "kidjim",
                    }
                }
                for clip_id, public in [(ID, True), (other, False)]
            ],
        }

    with pytest.raises(PublicPlaylistApiError):
        await _fetch_public_api_membership(api, ref)
    excluded = set()
    tracks = await _fetch_public_api_membership(api, ref, on_excluded=excluded.add)
    assert [t.id for t in tracks] == [ID]
    assert excluded == {other}


def test_demuxable_corrupt_audio_never_passes_preparation(audio, tmp_path):
    from suno_dl.verified_sync import prepare_audio

    # Packet enumeration alone cannot prove audio is decodable.
    raw = bytearray(audio.read_bytes())
    boxes = _children(bytes(raw))
    offset = 0
    for kind, box in boxes:
        if kind == b"mdat":
            raw[offset + 8 : offset + len(box)] = b"\xff" * (len(box) - 8)
        offset += len(box)
    audio.write_bytes(raw)
    assert packet_signature(audio)
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        prepare_audio(audio, tmp_path / "audio.part", "Producer", "Song")


def test_unsupported_audio_layout_cannot_fall_through_to_remux(audio, tmp_path, monkeypatch):
    from suno_dl import verified_sync as sync

    fast = tmp_path / "faststart.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(audio),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(fast),
        ],
        check=True,
    )
    before = fast.read_bytes()
    assert sync.packet_signature(fast)
    real = sync.run_media_process
    tools = []

    def observed(args, **kwargs):
        tools.append(args[0])
        return real(args, **kwargs)

    monkeypatch.setattr(sync, "run_media_process", observed)
    output = tmp_path / "audio.part"
    with pytest.raises(UnsupportedMP4):
        sync.prepare_audio(fast, output, "Producer", "Song")
    assert fast.read_bytes() == before and not output.exists()
    assert "ffmpeg" not in tools
