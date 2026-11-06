"""MP4 cover preservation and recovery; no network or real-library access."""

import hashlib
import json
import struct
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from suno_dl import producer_migration as m
from suno_dl.lossless_mp4 import write_isolated_mp4
from tests.test_producer_migration import ID1, _m4a_with_opaque_locale


def mdat(path):
    raw = path.read_bytes()
    pos = 0
    while pos < len(raw):
        size, kind = struct.unpack_from(">I4s", raw, pos)
        if kind == b"mdat":
            return raw[pos : pos + size]
        pos += size
    raise AssertionError("Missing encoded audio")


@pytest.mark.parametrize("image", ["png", "jpg"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_covered_artist_retag_and_recovery_preserve_audio_art_and_opaque_tags(
    tmp_path, monkeypatch, image, interrupted
):
    source = _m4a_with_opaque_locale(tmp_path, ".m4a", "Old")
    cover = (Path(__file__).parent / "fixtures" / ("cover-square." + image)).read_bytes()
    write_isolated_mp4(source, {bytes.fromhex("a9415254"): "Old"}, cover=cover)
    raw = source.read_bytes()
    offset = raw.index(b"zzzz") - 4
    opaque = raw[offset : offset + int.from_bytes(raw[offset : offset + 4], "big")]
    original = (mdat(source), m._audio_signature(source), m._metadata_fingerprint(source, ".m4a"))
    old_mtime = int(source.stat().st_mtime)
    mapping = {ID1: "Producer"}
    handles = {ID1: "producer"}
    plan = m.plan_migration(tmp_path, mapping, creator_handle_by_id=handles)
    replace = m.os.replace

    def after_replace(old, new):
        replace(old, new)
        if Path(old).suffix == ".part" and Path(new).suffix == ".m4a":
            raise RuntimeError("interrupted covered publication")

    if interrupted:
        monkeypatch.setattr(m.os, "replace", after_replace)
    kwargs = {
        "creator_handle_by_id": handles,
        "reviewed_creator_handles": handles,
        "writers_stopped": True,
        "producer_mapping_reviewed": True,
        "reviewed_producers": mapping,
        "reviewed_plan_sha256": m.review_plan_digest(plan),
    }
    if interrupted:
        with pytest.raises(RuntimeError, match="interrupted covered"):
            m.apply_migration(tmp_path, mapping, **kwargs)
        journal = m.journal_path(tmp_path)
        assert journal.exists()
        monkeypatch.setattr(m.os, "replace", replace)
        assert (
            m.resume_migration(
                tmp_path,
                writers_stopped=True,
                producer_mapping_reviewed=True,
                reviewed_journal_sha256=hashlib.sha256(journal.read_bytes()).hexdigest(),
            )
            == 1
        )
    else:
        assert m.apply_migration(tmp_path, mapping, **kwargs) == 1
    target = tmp_path / "Producer_A song.m4a"
    assert m._read_artist(target) == "Producer"
    assert bytes(MP4(target)["covr"][0]) == cover
    assert opaque in target.read_bytes()
    assert (
        mdat(target),
        m._audio_signature(target),
        m._metadata_fingerprint(target, ".m4a"),
    ) == original
    assert int(target.stat().st_mtime) != old_mtime
    assert not source.exists() and not m.journal_path(tmp_path).exists()
    assert len(list(tmp_path.glob("*.m4a"))) == 1
    assert json.loads((tmp_path / "Producer_A song.json").read_text())["id"] == ID1
    assert not m.plan_migration(tmp_path, mapping, creator_handle_by_id=handles)["groups"]


@pytest.mark.parametrize(
    "extra",
    [
        {"codec_type": "audio"},
        {"codec_type": "video", "codec_name": "h264"},
        {
            "codec_type": "video",
            "codec_name": "hevc",
            "width": 8,
            "height": 8,
            "disposition": {"attached_pic": 1},
        },
        {
            "codec_type": "video",
            "codec_name": "png",
            "width": 16384,
            "height": 16384,
            "disposition": {"attached_pic": 1},
        },
    ],
)
def test_cover_admission_still_refuses_other_audio_video_and_oversized_covers(extra):
    with pytest.raises(m.MigrationError):
        m._supported_audio_streams([{"codec_type": "audio"}, extra], allow_cover=True)


def test_mismatched_native_cover_type_refuses_before_any_live_move(tmp_path):
    from mutagen.mp4 import MP4Cover

    source = _m4a_with_opaque_locale(tmp_path, ".m4a", "Old")
    tagged = MP4(source)
    jpeg = (Path(__file__).parent / "fixtures/cover-square.jpg").read_bytes()
    tagged["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_PNG)]
    tagged.save()
    before = source.read_bytes()
    plan = m.plan_migration(tmp_path, {ID1: "Producer"})
    with pytest.raises(m.MigrationError):
        m.apply_migration(
            tmp_path,
            {ID1: "Producer"},
            writers_stopped=True,
            producer_mapping_reviewed=True,
            reviewed_producers={ID1: "Producer"},
            reviewed_plan_sha256=m.review_plan_digest(plan),
        )
    assert source.read_bytes() == before
    assert not m.journal_path(tmp_path).exists()


def test_cover_change_by_tag_writer_is_detected_before_audio_publication(tmp_path, monkeypatch):
    source = _m4a_with_opaque_locale(tmp_path, ".m4a", "Old")
    cover = (Path(__file__).parent / "fixtures/cover-square.png").read_bytes()
    replacement = (Path(__file__).parent / "fixtures/cover-square.jpg").read_bytes()
    write_isolated_mp4(source, {bytes.fromhex("a9415254"): "Old"}, cover=cover)
    before = source.read_bytes()
    plan = m.plan_migration(tmp_path, {ID1: "Producer"})
    write_artist = m._write_artist

    def damage_cover(path, extension, artist):
        write_artist(path, extension, artist)
        write_isolated_mp4(path, {bytes.fromhex("a9415254"): artist}, cover=replacement)

    monkeypatch.setattr(m, "_write_artist", damage_cover)
    with pytest.raises(m.MigrationError):
        m.apply_migration(
            tmp_path,
            {ID1: "Producer"},
            writers_stopped=True,
            producer_mapping_reviewed=True,
            reviewed_producers={ID1: "Producer"},
            reviewed_plan_sha256=m.review_plan_digest(plan),
        )
    assert (tmp_path / "Producer_A song.m4a").read_bytes() == before
    assert m.journal_path(tmp_path).exists()


@pytest.mark.parametrize("topology", ["no_audio", "unattached_png", "multiple_pictures"])
def test_cover_admission_refuses_non_audio_and_non_single_picture_topologies(topology):
    audio = {"codec_type": "audio"}
    picture = {
        "codec_type": "video",
        "codec_name": "png",
        "width": 8,
        "height": 8,
        "disposition": {"attached_pic": 1},
    }
    if topology == "no_audio":
        streams = [picture]
    elif topology == "unattached_png":
        streams = [audio, dict(picture, disposition={"attached_pic": 0})]
    else:
        streams = [audio, picture, dict(picture)]
    with pytest.raises(m.MigrationError):
        m._supported_audio_streams(streams, allow_cover=True)
