"""Tests for the downloader: fallback chain, atomic writes, stats, containment."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from suno_dl.downloader import _suffix_for, _unique_base, download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.models import TrackInfo

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
GOOD = f"https://cdn1.suno.ai/{CLIP}.m4a"
BAD = f"https://cdn1.suno.ai/{CLIP}.mp3"
VIDEO = f"https://cdn1.suno.ai/{CLIP}.mp4"
COVER = f"https://cdn2.suno.ai/image_{CLIP}.jpeg"

# Valid media bytes for fake CDN responses: an MP4 'ftyp' box header plus
# padding. Structural validation (_looks_like_media) must accept this.
_VALID_MEDIA = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048
# Suno's live cloudfront primary serves an *encrypted* payload with HTTP 200
# and content-type audio/mp4 — unplayable, and indistinguishable by size.
_ENCRYPTED_PRIMARY = bytes(range(256)) * 24


def _track(**kw) -> TrackInfo:
    base = {
        "id": CLIP,
        "title": "Song",
        "handle": None,
        "page_url": f"https://suno.com/song/{CLIP}",
        "audio_url": BAD,
        "media_urls": [BAD],
    }
    base.update(kw)
    return TrackInfo(**base)


def _no_sidecars(directory: Path) -> list[Path]:
    """Directory listing without the ``.json`` sidecars (covered in test_sidecar.py)."""
    return [
        p
        for p in directory.iterdir()
        if p.suffix != ".json" and p.name not in {".suno-sync.lock", ".library"}
    ]


def _fetcher(handler, retries: int = 1) -> Fetcher:
    f = Fetcher(timeout_sec=1.0, max_retries=retries)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return f


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://x/a.m4a", ".m4a"),
        ("https://x/a.m4a?sig=1", ".m4a"),
        ("https://x/a.MP4", ".mp4"),
        ("https://x/a.xyz", ".mp3"),
    ],
)
def test_suffix_for(url: str, expected: str) -> None:
    assert _suffix_for(url) == expected


def test_unique_base_dedups(tmp_path: Path) -> None:
    base = tmp_path / "song"
    t1 = _unique_base(base, set())
    assert t1.name == "song"
    # Only in-run collisions force a new name; on-disk files do not (resume).
    t2 = _unique_base(base, {t1})
    assert t2.name == "song_2"


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_fallback_from_bad_to_good(tmp_path: Path) -> None:
    def handler(req):
        if "good" in str(req.url) or str(req.url).endswith(".m4a"):
            return httpx.Response(200, content=_VALID_MEDIA)
        return httpx.Response(403)

    f = _fetcher(handler)
    track = _track(media_urls=[BAD, GOOD])
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h")
    assert stats["downloaded"] == 1
    assert stats["failed"] == 0
    assert (tmp_path / "Song.m4a").exists()
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_all_candidates_403_marks_failed(tmp_path: Path) -> None:
    def handler(req):
        return httpx.Response(403)

    f = _fetcher(handler)
    track = _track()
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h")
    assert stats["failed"] == 1
    assert stats["downloaded"] == 0
    assert list(_no_sidecars(tmp_path)) == []
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_rejects_non_allowlisted_host(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track(
        audio_url="http://169.254.169.254/latest/meta-data/",
        media_urls=["http://169.254.169.254/latest/meta-data/"],
        page_url="https://suno.com/song/" + CLIP,
    )
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h")
    assert stats["failed"] == 1
    # The only network call permitted is the song-page resolution attempt,
    # never the metadata endpoint.
    assert all("169.254" not in str(c) for c in [calls])
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_skip_if_exists(tmp_path: Path) -> None:
    from suno_dl.sidecar import build_sidecar, write_sidecar

    (tmp_path / "Song.m4a").write_bytes(b"a" * 4096)
    assert write_sidecar(
        tmp_path / "Song.json",
        build_sidecar(_track(), handle="h", saved_files=["Song.m4a"]),
    )

    def handler(req):
        raise AssertionError("must not fetch when file exists")

    f = _fetcher(handler)
    stats = await download_tracks([_track()], f, 1, tmp_path, "{title}", "h")
    assert stats["skipped_exists"] == 1
    assert stats["downloaded"] == 0
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_small_response_discarded(tmp_path: Path) -> None:
    def handler(req):
        return httpx.Response(200, content=b"x")  # < 1024 bytes

    f = _fetcher(handler)
    stats = await download_tracks([_track()], f, 1, tmp_path, "{title}", "h")
    assert stats["downloaded"] == 0
    assert stats["failed"] == 1
    assert list(_no_sidecars(tmp_path)) == []
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    def handler(req):
        raise AssertionError("dry run must not fetch")

    f = _fetcher(handler)
    stats = await download_tracks(
        [_track(media_urls=[GOOD])], f, 1, tmp_path, "{title}", "h", dry_run=True
    )
    assert stats["dry_run"] == 1
    assert list(_no_sidecars(tmp_path)) == []
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_concurrency_many_tracks_stats_consistent(tmp_path: Path) -> None:
    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    tracks = []
    for i in range(12):
        cid = f"a1b2c3d4-1111-4111-8111-{i:012d}"
        url = f"https://cdn1.suno.ai/{cid}.m4a"
        tracks.append(_track(id=cid, title=f"Song {i}", audio_url=url, media_urls=[url]))
    stats = await download_tracks(tracks, f, 5, tmp_path, "{title}", "h")
    assert stats["downloaded"] == 12
    assert stats["failed"] == 0
    assert len(list(tmp_path.glob("*.m4a"))) == 12
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_identical_titles_do_not_overwrite(tmp_path: Path) -> None:
    """Two distinct tracks with the same sanitised title must both survive."""

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    cid_a = "a1b2c3d4-1111-4111-8111-000000000001"
    cid_b = "a1b2c3d4-1111-4111-8111-000000000002"
    tracks = [
        _track(
            id=cid_a,
            title="Same Name",
            audio_url=f"https://cdn1.suno.ai/{cid_a}.m4a",
            media_urls=[f"https://cdn1.suno.ai/{cid_a}.m4a"],
        ),
        _track(
            id=cid_b,
            title="Same Name",
            audio_url=f"https://cdn1.suno.ai/{cid_b}.m4a",
            media_urls=[f"https://cdn1.suno.ai/{cid_b}.m4a"],
        ),
    ]
    stats = await download_tracks(tracks, f, 1, tmp_path, "{title}", "h")
    assert stats["downloaded"] == 2
    assert len(list(tmp_path.glob("*.m4a"))) == 2
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_audio_only_skips_video_only_track(tmp_path: Path) -> None:
    """--audio-only: a track whose only candidate is an mp4 is skipped with no request."""

    def handler(req):
        raise AssertionError("video-only track must not be fetched in audio-only mode")

    f = _fetcher(handler)
    track = _track(audio_url=VIDEO, media_urls=[VIDEO])
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h", audio_only=True)
    assert stats["skipped_video"] == 1
    assert stats["downloaded"] == 0
    assert list(_no_sidecars(tmp_path)) == []
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_audio_only_downloads_audio_ignoring_mp4_duplicate(tmp_path: Path) -> None:
    """A track with both mp4 and m4a candidates downloads only the m4a."""

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track(audio_url=VIDEO, media_urls=[VIDEO, GOOD])
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h", audio_only=True)
    assert stats["downloaded"] == 1
    assert stats["skipped_video"] == 0
    assert [p.name for p in _no_sidecars(tmp_path)] == ["Song.m4a"]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_audio_only_still_resolves_via_song_page(tmp_path: Path) -> None:
    """audio_only filters AFTER song-page resolution (fixture-shaped track)."""
    page_id = "3e6c3f60-7a43-4ccb-a4e4-fc35d414e4d6"
    fixture_html = (Path(__file__).parent / "fixtures" / "real_song_page.html").read_text(
        encoding="utf-8"
    )

    def handler(req):
        if str(req.url).startswith("https://suno.com/song/"):
            return httpx.Response(200, text=fixture_html)
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    # No declared candidates: the downloader must resolve via the song page,
    # then audio_only keeps the m4a/mp3 and drops the fixture's mp4.
    track = _track(id=page_id, audio_url=None, media_urls=[])
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h", audio_only=True)
    assert stats["downloaded"] == 1
    assert stats["skipped_video"] == 0
    assert [p.name for p in tmp_path.glob("*.m4a")] == ["Song.m4a"]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_covers_download_and_recompression_never_worsens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--covers: cover saved next to the track; compression only applies when smaller."""

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)

    calls: list[tuple[str, int]] = []

    def fake_compress(path: Path, min_kb: int = 10) -> None:
        calls.append((path.name, path.stat().st_size))
        path.write_bytes(path.read_bytes()[:-1])  # shrink by 1 byte, like Pillow would

    monkeypatch.setattr("suno_dl.downloader._compress_image", fake_compress)
    track = _track(cover_url=COVER)
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h", covers=True)
    assert stats["downloaded"] == 1
    assert calls == [("Song.cover.jpeg", len(_VALID_MEDIA))]
    assert (tmp_path / "Song.cover.jpeg").stat().st_size == len(_VALID_MEDIA) - 1
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_covers_can_be_explicitly_disabled(tmp_path: Path) -> None:
    """Explicit covers=False suppresses optional artwork."""

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track(cover_url=COVER)
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h", covers=False)
    assert stats["downloaded"] == 1
    assert list(_no_sidecars(tmp_path)) == [tmp_path / "Song.mp3"]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_cover_missing_or_blocked_is_silent_noop(tmp_path: Path) -> None:
    """A 403 cover never fails the audio download and leaves no artifacts."""

    def handler(req):
        if str(req.url).endswith(".jpeg"):
            return httpx.Response(403)
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track(cover_url=COVER)
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h", covers=True)
    assert stats["downloaded"] == 1
    assert [p.name for p in _no_sidecars(tmp_path)] == ["Song.mp3"]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_cover_skipped_when_not_allowlisted(tmp_path: Path) -> None:
    """Covers honour the same SSRF allow-list as media URLs (https enforced, hosts allow-listed)."""

    def handler(req):
        if str(req.url).startswith("https://cdn1.suno.ai/"):
            return httpx.Response(200, content=_VALID_MEDIA)
        raise AssertionError("non-allow-listed cover must never be fetched")

    f = _fetcher(handler)
    for i, bad in enumerate(
        (
            "http://169.254.169.254/latest/meta-data",  # non-https scheme rejected
            "https://169.254.169.254/latest/meta-data",  # IP literal rejected
            "https://evil.example.com/image.jpeg",  # host not on allow-list
        )
    ):
        out = tmp_path / f"case{i}"
        track = _track(cover_url=bad, audio_url=GOOD, media_urls=[GOOD])
        stats = await download_tracks([track], f, 1, out, "{title}", "h", covers=True)
        assert stats["downloaded"] == 1
        assert [p.name for p in _no_sidecars(out)] == ["Song.m4a"]
    await f.__aexit__()


def _install_fake_pillow(monkeypatch: pytest.MonkeyPatch, result: bytes | Exception) -> None:
    """Install a fake PIL.Image whose open() yields a controllable fake image."""
    import sys
    import types

    class _FakeImageObj:
        def load(self):
            return None

        def save(self, buf, fmt, **kw):
            if isinstance(result, Exception):
                raise result
            buf.write(result)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    fake_pil = types.ModuleType("PIL")
    fake_image_mod = types.ModuleType("PIL.Image")

    class _FakeImage:
        @staticmethod
        def open(p):
            return _FakeImageObj()

    # NB: only register 'PIL' in sys.modules. If 'PIL.Image' were registered
    # as a module, `from PIL import Image` would return that (empty) module
    # instead of the parent attribute, shadowing the fake.
    fake_image_mod.Image = _FakeImage
    fake_pil.Image = fake_image_mod.Image
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)


def test_compression_never_worsens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recompression replaces the file only when strictly smaller."""
    from suno_dl.downloader import _compress_image

    cover = tmp_path / "a.jpg"
    cover.write_bytes(b"j" * 512)

    _install_fake_pillow(monkeypatch, b"smaller")
    _compress_image(cover)
    assert cover.read_bytes() == b"smaller"

    _install_fake_pillow(monkeypatch, b"way bigger than the original payload")
    _compress_image(cover)
    assert cover.read_bytes() == b"smaller"  # bigger output discarded


def test_compression_survives_corrupt_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decode failure keeps the original bytes — never fails the run."""
    from suno_dl.downloader import _compress_image

    cover = tmp_path / "b.jpg"
    payload = b"original-bytes"
    cover.write_bytes(payload)

    _install_fake_pillow(monkeypatch, ValueError("corrupt image"))
    _compress_image(cover)
    assert cover.read_bytes() == payload


def test_compression_noop_without_pillow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the optional [image] extra, covers are kept exactly as served."""
    import sys

    from suno_dl.downloader import _compress_image

    cover = tmp_path / "c.jpg"
    payload = b"as-served"
    cover.write_bytes(payload)

    monkeypatch.setitem(sys.modules, "PIL", None)  # None => ImportError
    _compress_image(cover)
    assert cover.read_bytes() == payload


# ---------------------------------------------------------------------------
# Branch-closure tests: helpers and error paths not exercised end-to-end.
# ---------------------------------------------------------------------------


async def test_resolve_media_urls_prefers_declared() -> None:
    """Declared, allow-listed media URLs short-circuit (no page fetch)."""
    from suno_dl.downloader import _resolve_media_urls

    def handler(req):
        raise AssertionError("page must not be fetched when media_urls are declared")

    f = _fetcher(handler)
    track = _track(audio_url=GOOD, media_urls=[GOOD, BAD])
    resolved = await _resolve_media_urls(track, f)
    assert resolved == [GOOD, BAD]
    await f.__aexit__()


async def test_resolve_media_urls_drops_disallowed() -> None:
    """Every declared URL off the allow-list -> the page fetch happens."""
    from suno_dl.downloader import _resolve_media_urls
    from suno_dl.urls import is_allowed_media_url

    evil = "https://evil.example.com/clip.m4a"
    fixture_html = (Path(__file__).parent / "fixtures" / "real_song_page.html").read_text(
        encoding="utf-8"
    )

    def handler(req):
        return httpx.Response(200, text=fixture_html)

    f = _fetcher(handler)
    page_id = "3e6c3f60-7a43-4ccb-a4e4-fc35d414e4d6"
    track = _track(id=page_id, audio_url=evil, media_urls=[evil])
    resolved = await _resolve_media_urls(track, f)
    assert resolved and all(is_allowed_media_url(u) for u in resolved)
    await f.__aexit__()


async def test_resolve_media_urls_redacts_page_url_in_error() -> None:
    """ResolutionError never carries a signed query string from the page URL."""
    from suno_dl.downloader import _resolve_media_urls
    from suno_dl.errors import ResolutionError

    def handler(req):
        raise OSError("network down")

    f = _fetcher(handler)
    track = _track(
        audio_url=None,
        media_urls=[],
        page_url=f"https://suno.com/song/{CLIP}?sig=TOPSECRET",
    )
    with pytest.raises(ResolutionError) as excinfo:
        await _resolve_media_urls(track, f)
    assert "TOPSECRET" not in str(excinfo.value)
    await f.__aexit__()


async def test_resolve_media_urls_refuses_untrusted_page_url() -> None:
    """A hostile page_url (e.g. from a forged canonical link) is never fetched;
    the canonical suno.com/song/<id> URL is used instead."""
    from suno_dl.downloader import _resolve_media_urls

    fixture_html = (Path(__file__).parent / "fixtures" / "real_song_page.html").read_text(
        encoding="utf-8"
    )
    fetched: list[str] = []

    def handler(req):
        fetched.append(str(req.url))
        return httpx.Response(200, text=fixture_html)

    f = _fetcher(handler)
    page_id = "3e6c3f60-7a43-4ccb-a4e4-fc35d414e4d6"  # id present in the fixture page
    track = _track(
        id=page_id,
        audio_url=None,
        media_urls=[],
        page_url="https://evil.example.com/steal?sig=TOPSECRET",
    )
    resolved = await _resolve_media_urls(track, f)
    assert fetched == [f"https://suno.com/song/{page_id}"]
    assert "TOPSECRET" not in " ".join(fetched)
    assert resolved
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_encrypted_primary_falls_through_to_valid_candidate(tmp_path: Path) -> None:
    """Live-CDN regression: Suno's cloudfront primary serves HTTP 200 with an
    encrypted payload (audio/mp4, unplayable). The downloader must reject it
    structurally and save from the next valid candidate instead."""
    from suno_dl.downloader import download_tracks

    primary = f"https://d2lwuy8qc234o3.cloudfront.net/1/clip/{CLIP}.m4a?sig=SECRET"

    def handler(req):
        if "cloudfront" in str(req.url):
            return httpx.Response(
                200,
                content=_ENCRYPTED_PRIMARY,
                headers={"content-type": "audio/mp4"},
            )
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track(audio_url=primary, media_urls=[primary, GOOD])
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h")
    assert stats["downloaded"] == 1
    assert list(_no_sidecars(tmp_path)) == [tmp_path / "Song.m4a"]
    assert (tmp_path / "Song.m4a").read_bytes()[:8] == _VALID_MEDIA[:8]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_all_candidates_invalid_yields_failed_and_no_files(tmp_path: Path) -> None:
    """If every candidate fails structural validation, the track counts as
    failed and nothing (including .part debris) is written."""
    from suno_dl.downloader import download_tracks

    primary = f"https://d2lwuy8qc234o3.cloudfront.net/1/clip/{CLIP}.m4a"

    def handler(req):
        return httpx.Response(200, content=_ENCRYPTED_PRIMARY)

    f = _fetcher(handler)
    track = _track(audio_url=primary, media_urls=[primary])
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h")
    assert stats["failed"] == 1
    assert stats["downloaded"] == 0
    assert list(_no_sidecars(tmp_path)) == []
    await f.__aexit__()


def test_suffix_for_wav_urls() -> None:
    """Real wav objects must keep the .wav extension (not be mislabelled .mp3)."""
    assert _suffix_for(f"https://cdn1.suno.ai/{CLIP}.wav?sig=S") == ".wav"
    assert _suffix_for(f"https://cdn1.suno.ai/{CLIP}.m4a") == ".m4a"
    assert _suffix_for(f"https://cdn1.suno.ai/{CLIP}.mp4") == ".mp4"
    assert _suffix_for("https://cdn1.suno.ai/noext") == ".mp3"  # legacy default


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_wav_media_saved_with_wav_extension(tmp_path: Path) -> None:
    from suno_dl.downloader import download_tracks

    wav_url = f"https://cdn1.suno.ai/{CLIP}.wav"

    def handler(req):
        return httpx.Response(200, content=b"RIFF\x24\x08\x00\x00WAVEfmt " + b"x" * 2048)

    f = _fetcher(handler)
    track = _track(audio_url=wav_url, media_urls=[wav_url])
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h")
    assert stats["downloaded"] == 1
    assert list(_no_sidecars(tmp_path)) == [tmp_path / "Song.wav"]
    await f.__aexit__()


async def test_png_cover_is_never_reencoded_as_jpeg(tmp_path: Path) -> None:
    """Compression must keep a PNG cover byte-identical (no JPEG-in-.png)."""
    import base64

    from suno_dl.downloader import _compress_image

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    p = tmp_path / "cover.png"
    p.write_bytes(png)
    _compress_image(p)
    assert p.read_bytes() == png  # unchanged whether or not Pillow is installed


# --- #69/#70: tags on sync, and the legacy-name skip -------------------------


def _real_m4a() -> bytes:
    return (Path(__file__).parent / "fixtures" / "tiny.m4a").read_bytes()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_download_writes_artist_and_title_tags(tmp_path: Path) -> None:
    """A saved track carries its known producer and title tags."""
    from suno_dl.tagging import read_tags

    def handler(req):
        return httpx.Response(200, content=_real_m4a())

    f = _fetcher(handler)
    track = _track(title="Absolutely Certain", artist="drinko", audio_url=GOOD, media_urls=[GOOD])
    stats = await download_tracks([track], f, 1, tmp_path, "{handle}_{title}", "drinko")
    assert stats["downloaded"] == 1
    assert stats["tagged"] == 1
    tags = read_tags(tmp_path / "drinko_Absolutely Certain.m4a")
    assert tags["artist"] == "drinko"
    assert tags["title"] == "Absolutely Certain"
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_feat_title_lands_in_full_tag_and_safe_filename(tmp_path: Path) -> None:
    """#70 regression: nothing after 'feat' may be lost, in tag or filename."""
    from suno_dl.tagging import read_tags

    title = "Ajani, Nacatl Pariah (feat. Pyotr Kropotkin)"

    def handler(req):
        return httpx.Response(200, content=_real_m4a())

    f = _fetcher(handler)
    track = _track(title=title, audio_url=GOOD, media_urls=[GOOD])
    stats = await download_tracks([track], f, 1, tmp_path, "{handle}_{title}", "drinko")
    assert stats["downloaded"] == 1
    (saved,) = list(tmp_path.glob("*.m4a"))
    assert saved.name == "drinko_Ajani, Nacatl Pariah (feat_ Pyotr Kropotkin).m4a"
    assert "." not in saved.stem
    assert read_tags(saved)["title"] == title
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_legacy_filename_is_skipped_not_refetched(tmp_path: Path) -> None:
    """A pre-1.6 archive keeps its files: the old name counts as 'present'."""

    def handler(req):
        raise AssertionError("an already-downloaded track must never be re-fetched")

    from suno_dl.sidecar import build_sidecar, sidecar_path, write_sidecar

    existing = tmp_path / "drinko_Song (feat. Alice).m4a"
    existing.write_bytes(_real_m4a())
    f = _fetcher(handler)
    track = _track(title="Song (feat. Alice)", audio_url=GOOD, media_urls=[GOOD])
    assert write_sidecar(
        sidecar_path(existing),
        build_sidecar(track, handle="drinko", saved_files=[existing.name]),
    )
    stats = await download_tracks([track], f, 1, tmp_path, "{handle}_{title}", "drinko")
    assert stats["skipped_exists"] == 1
    assert stats["downloaded"] == 0
    assert existing.exists()  # untouched, not renamed, not re-downloaded
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_two_same_titled_tracks_do_not_borrow_each_others_legacy_name(
    tmp_path: Path,
) -> None:
    """The legacy fallback must not turn track B into a false 'already there'."""

    def handler(req):
        return httpx.Response(200, content=_real_m4a())

    f = _fetcher(handler)
    cid_a = "a1b2c3d4-1111-4111-8111-00000000000a"
    cid_b = "a1b2c3d4-1111-4111-8111-00000000000b"
    tracks = [
        _track(
            id=cid_a,
            title="Same Name (feat. X)",
            audio_url=f"https://cdn1.suno.ai/{cid_a}.m4a",
            media_urls=[f"https://cdn1.suno.ai/{cid_a}.m4a"],
        ),
        _track(
            id=cid_b,
            title="Same Name (feat. X)",
            audio_url=f"https://cdn1.suno.ai/{cid_b}.m4a",
            media_urls=[f"https://cdn1.suno.ai/{cid_b}.m4a"],
        ),
    ]
    stats = await download_tracks(tracks, f, 1, tmp_path, "{handle}_{title}", "drinko")
    assert stats["downloaded"] == 2
    assert stats["skipped_exists"] == 0
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_tagging_failure_never_fails_the_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Untaggable audio is still a successful download: the file is the point."""
    from suno_dl.tagging import TagResult

    def handler(req):
        return httpx.Response(200, content=_real_m4a())

    monkeypatch.setattr(
        "suno_dl.downloader.tag_file",
        lambda *a, **kw: TagResult(False, "none", "", "", "boom"),
    )
    f = _fetcher(handler)
    track = _track(audio_url=GOOD, media_urls=[GOOD])
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h")
    assert stats["downloaded"] == 1
    assert stats["tagged"] == 0
    assert (tmp_path / "Song.m4a").exists()
    await f.__aexit__()


async def test_resolve_media_urls_wraps_fetch_errors() -> None:
    """A page-fetch failure surfaces as ResolutionError, never a raw exception."""
    from suno_dl.downloader import _resolve_media_urls
    from suno_dl.errors import ResolutionError

    def handler(req):
        raise OSError("network down")

    f = _fetcher(handler)
    track = _track(audio_url=None, media_urls=[])
    with pytest.raises(ResolutionError, match="could not fetch"):
        await _resolve_media_urls(track, f)
    await f.__aexit__()


async def test_resolve_media_urls_empty_when_song_not_on_page() -> None:
    """A page that parses but does not contain the track yields []."""
    from suno_dl.downloader import _resolve_media_urls

    fixture_html = (Path(__file__).parent / "fixtures" / "real_song_page.html").read_text(
        encoding="utf-8"
    )

    def handler(req):
        return httpx.Response(200, text=fixture_html)

    f = _fetcher(handler)
    other = "b2c3d4e5-2222-4222-8222-222222222222"
    track = _track(id=other, audio_url=None, media_urls=[])
    assert await _resolve_media_urls(track, f) == []
    await f.__aexit__()


def test_is_contained_rejects_escape(tmp_path: Path) -> None:
    """Path containment: direct and nested children allowed, escapes rejected."""
    from suno_dl.downloader import _is_contained

    root = tmp_path / "out"
    root.mkdir()
    assert _is_contained(root / "a.mp3", root)
    assert _is_contained(root / "sub" / "a.mp3", root)
    assert not _is_contained(tmp_path / "elsewhere.mp3", root)
    assert not _is_contained(root.parent.parent / "evil.mp3", root)


def test_unique_base_dup_fallback(tmp_path: Path) -> None:
    """If _2.._999 are all taken, the _dup suffix is the last resort."""
    from suno_dl.downloader import _unique_base

    base = tmp_path / "song"
    taken = {base, *{Path(f"{base}_{n}") for n in range(2, 1000)}}
    assert _unique_base(base, taken) == Path(f"{base}_dup")


def _fresh_stats(total: int = 1) -> dict[str, int]:
    return {
        "total": total,
        "dry_run": 0,
        "downloaded": 0,
        "skipped_exists": 0,
        "failed": 0,
        "skipped_video": 0,
    }


async def test_download_single_tolerates_bar_none(tmp_path: Path) -> None:
    """Every outcome must tolerate bar=None (the API surface, unlike the CLI)."""
    from suno_dl.downloader import _download_single
    from suno_dl.models import TrackInfo

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    semaphore = asyncio.Semaphore(1)
    stats = _fresh_stats()

    dl_url = f"https://cdn1.suno.ai/{CLIP}.m4a"
    track_dl = TrackInfo(
        id=CLIP,
        title="DL",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=dl_url,
        media_urls=[dl_url],
    )
    assert (
        await _download_single(track_dl, tmp_path / "dl", tmp_path, f, semaphore, stats, None)
        == "downloaded"
    )

    video_url = f"https://cdn1.suno.ai/{CLIP}.mp4"
    track_video = TrackInfo(
        id="d4e5f6a7-4444-4444-8444-444444444444",
        title="Vid",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=video_url,
        media_urls=[video_url],
    )
    assert (
        await _download_single(
            track_video, tmp_path / "vid", tmp_path, f, semaphore, stats, None, audio_only=True
        )
        == "failed"
    )

    track_empty = TrackInfo(
        id="e5f6a7b8-5555-4555-8555-555555555555",
        title="None",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=None,
        media_urls=[],
    )
    assert (
        await _download_single(track_empty, tmp_path / "empty", tmp_path, f, semaphore, stats, None)
        == "failed"
    )

    assert stats["downloaded"] == 1
    assert stats["skipped_video"] == 1
    assert stats["failed"] == 1
    await f.__aexit__()


async def test_download_single_resolution_failure_counts_as_failed(tmp_path: Path) -> None:
    """When page resolution raises, the track fails without aborting the run."""
    from suno_dl.downloader import _download_single
    from suno_dl.models import TrackInfo

    def handler(req):
        raise OSError("network down")

    f = _fetcher(handler)
    track = TrackInfo(
        id=CLIP,
        title="X",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=None,
        media_urls=[],
    )
    stats = _fresh_stats()
    assert (
        await _download_single(
            track, tmp_path / "x", tmp_path, f, asyncio.Semaphore(1), stats, None
        )
        == "failed"
    )
    assert stats["failed"] == 1
    assert list(_no_sidecars(tmp_path)) == []
    await f.__aexit__()


async def test_download_single_transport_error_falls_through(tmp_path: Path) -> None:
    """A transport error on one candidate falls through to the next URL."""
    from suno_dl.downloader import _download_single
    from suno_dl.models import TrackInfo

    def handler(req):
        if str(req.url).endswith(".mp3"):
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = TrackInfo(
        id=CLIP,
        title="X",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=BAD,
        media_urls=[BAD, GOOD],
    )
    stats = _fresh_stats()
    assert (
        await _download_single(
            track, tmp_path / "x", tmp_path, f, asyncio.Semaphore(1), stats, None
        )
        == "downloaded"
    )
    assert stats["downloaded"] == 1
    assert [p.name for p in _no_sidecars(tmp_path)] == ["x.m4a"]
    await f.__aexit__()


async def test_media_refused_outside_output_dir(tmp_path: Path) -> None:
    """A containment violation is refused before any network request."""
    from suno_dl.downloader import _download_single
    from suno_dl.models import TrackInfo

    root = (tmp_path / "out").resolve()
    escape_base = (tmp_path / "escape-me").resolve()  # sibling of `out`: outside

    def handler(req):
        raise AssertionError("nothing may be fetched for a refused path")

    f = _fetcher(handler)
    track = TrackInfo(
        id=CLIP,
        title="X",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=GOOD,
        media_urls=[GOOD],
    )
    stats = _fresh_stats()
    assert (
        await _download_single(track, escape_base, root, f, asyncio.Semaphore(1), stats, None)
        == "failed"
    )
    assert stats["failed"] == 1
    assert not (tmp_path / "escape-me.m4a").exists()
    await f.__aexit__()


async def test_cover_refused_outside_output_dir(tmp_path: Path) -> None:
    """Cover paths honour containment too: refused without a request."""
    from suno_dl.downloader import _download_cover
    from suno_dl.models import TrackInfo

    root = (tmp_path / "out").resolve()
    escape_base = (tmp_path / "escape-me").resolve()

    def handler(req):
        raise AssertionError("cover must never be fetched when refused")

    f = _fetcher(handler)
    track = TrackInfo(
        id=CLIP,
        title="X",
        page_url=f"https://suno.com/song/{CLIP}",
        cover_url=COVER,
    )
    await _download_cover(track, escape_base, root, f)  # must be a silent no-op
    await f.__aexit__()


async def test_cover_existing_file_short_circuits(tmp_path: Path) -> None:
    """An existing cover file is never re-fetched (resume-friendly)."""
    from suno_dl.models import TrackInfo

    existing = tmp_path / "Song.cover.jpeg"
    existing.write_bytes(b"already-here")

    def handler(req):
        if str(req.url).endswith(".jpeg"):
            raise AssertionError("existing cover must not be re-fetched")
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=GOOD,
        media_urls=[GOOD],
        cover_url=COVER,
    )
    stats = _fresh_stats()
    from suno_dl.downloader import _download_single

    assert (
        await _download_single(
            track, tmp_path / "Song", tmp_path, f, asyncio.Semaphore(1), stats, None, covers=True
        )
        == "downloaded"
    )
    assert existing.read_bytes() == b"already-here"  # untouched
    await f.__aexit__()


async def test_cover_suffix_defaults_without_image_extension(tmp_path: Path) -> None:
    """A cover URL with no recognisable image extension still saves as .jpg."""
    from suno_dl.models import TrackInfo

    no_ext = f"https://cdn2.suno.ai/image_{CLIP}"  # no suffix at all

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=GOOD,
        media_urls=[GOOD],
        cover_url=no_ext,
    )
    stats = _fresh_stats()
    from suno_dl.downloader import _download_single

    await _download_single(
        track, tmp_path / "Song", tmp_path, f, asyncio.Semaphore(1), stats, None, covers=True
    )
    assert (tmp_path / "Song.cover.jpg").exists()
    await f.__aexit__()


async def test_cover_zero_byte_response_discarded(tmp_path: Path) -> None:
    """A 200 with an empty body leaves no cover file and no .part debris."""
    from suno_dl.models import TrackInfo

    def handler(req):
        if str(req.url).endswith(".jpeg"):
            return httpx.Response(200, content=b"")
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=GOOD,
        media_urls=[GOOD],
        cover_url=COVER,
    )
    stats = _fresh_stats()
    from suno_dl.downloader import _download_single

    assert (
        await _download_single(
            track, tmp_path / "Song", tmp_path, f, asyncio.Semaphore(1), stats, None, covers=True
        )
        == "downloaded"
    )
    assert [p.name for p in _no_sidecars(tmp_path)] == ["Song.m4a"]
    await f.__aexit__()


async def test_cover_transport_error_is_silent(tmp_path: Path) -> None:
    """A network error during cover fetch never fails the audio download."""
    from suno_dl.models import TrackInfo

    def handler(req):
        if str(req.url).endswith(".jpeg"):
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=GOOD,
        media_urls=[GOOD],
        cover_url=COVER,
    )
    stats = _fresh_stats()
    from suno_dl.downloader import _download_single

    assert (
        await _download_single(
            track, tmp_path / "Song", tmp_path, f, asyncio.Semaphore(1), stats, None, covers=True
        )
        == "downloaded"
    )
    assert [p.name for p in _no_sidecars(tmp_path)] == ["Song.m4a"]  # no .part debris
    await f.__aexit__()


async def test_blocked_candidate_falls_through_to_next(tmp_path: Path) -> None:
    """A BlockedError on one candidate moves on to the next URL (no debris)."""
    from suno_dl.downloader import _download_single
    from suno_dl.errors import BlockedError
    from suno_dl.models import TrackInfo

    class _StubLimiter:
        async def acquire(self, url: str) -> None:
            if str(url).endswith(".mp3"):
                raise BlockedError("blocked by CDN")

    f = _fetcher(lambda req: httpx.Response(200, content=_VALID_MEDIA))
    f.rate_limiter = _StubLimiter()
    track = TrackInfo(
        id=CLIP,
        title="X",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=BAD,
        media_urls=[BAD, GOOD],
    )
    stats = _fresh_stats()
    assert (
        await _download_single(
            track, tmp_path / "x", tmp_path, f, asyncio.Semaphore(1), stats, None
        )
        == "downloaded"
    )
    assert [p.name for p in _no_sidecars(tmp_path)] == ["x.m4a"]
    await f.__aexit__()


async def test_download_single_skip_exists_with_bar_none(tmp_path: Path) -> None:
    """Resume-skip must work without a progress bar too."""
    from suno_dl.downloader import _download_single
    from suno_dl.models import TrackInfo

    (tmp_path / "Song.m4a").write_bytes(b"a" * 4096)

    def handler(req):
        raise AssertionError("must not fetch when the file exists")

    f = _fetcher(handler)
    track = TrackInfo(
        id=CLIP,
        title="Song",
        page_url=f"https://suno.com/song/{CLIP}",
        audio_url=GOOD,
        media_urls=[GOOD],
    )
    stats = _fresh_stats()
    assert (
        await _download_single(
            track,
            tmp_path / "Song",
            tmp_path,
            f,
            asyncio.Semaphore(1),
            stats,
            None,
            known_existing=True,
        )
        == "skipped_exists"
    )
    assert stats["skipped_exists"] == 1
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_dry_run_video_only_note_and_covers_line(tmp_path: Path, capsys) -> None:
    """Dry-run annotates video-only tracks and lists covers when asked."""
    video_track = _track(title="Vid Only", audio_url=VIDEO, media_urls=[])
    normal_track = _track(title="Normal", audio_url=GOOD, media_urls=[GOOD], cover_url=COVER)

    def handler(req):
        raise AssertionError("dry run must not fetch")

    f = _fetcher(handler)
    stats = await download_tracks(
        [video_track, normal_track],
        f,
        1,
        tmp_path,
        "{title}",
        "h",
        dry_run=True,
        audio_only=True,
        covers=True,
    )
    out = capsys.readouterr().out
    assert "(video-only; skipped with --audio-only)" in out
    assert ".cover.jpg" in out
    assert stats["dry_run"] == 2
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_subdir_nests_output(tmp_path: Path) -> None:
    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track()
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h", subdir="drinko")
    assert stats["downloaded"] == 1
    nested = tmp_path / "drinko"
    assert nested.is_dir()
    assert any(p.is_file() for p in nested.iterdir())


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_subdir_sanitized_before_nesting(tmp_path: Path) -> None:
    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track()
    # A traversal attempt in the subdir must be neutralized by naming.
    await download_tracks([track], f, 1, tmp_path, "{title}", "h", subdir="../escape")
    escaped = tmp_path.parent / "escape"
    assert not escaped.exists()
    assert (tmp_path / "_escape").is_dir() or any(p.is_dir() for p in tmp_path.iterdir())


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
def test_extract_audio_skips_without_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    from suno_dl.downloader import extract_audio_files

    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("SUNO_FFMPEG", raising=False)
    src = tmp_path / "x.mp4"
    src.write_bytes(_VALID_MEDIA)
    assert extract_audio_files([src]) == []


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_extract_audio_real_remux(tmp_path: Path) -> None:
    import shutil as _sh
    import subprocess as _sp

    ffmpeg = _sh.which("ffmpeg")
    if not ffmpeg:  # CI image may lack ffmpeg; the no-ffmpeg path is tested above
        pytest.skip("ffmpeg not available")
    src = tmp_path / "tone.mp4"
    _sp.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-c:a",
            "aac",
            str(src),
            "-y",
        ],
        check=True,
        capture_output=True,
    )
    from suno_dl.downloader import extract_audio_files

    written = extract_audio_files([src])
    assert written == [src.with_suffix(".m4a")]
    assert src.with_suffix(".m4a").stat().st_size > 0
    # Idempotent: existing output is not rewritten.
    assert extract_audio_files([src]) == []


# --- #73: audio-only fallback to mp4 with extraction -------------------------


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_audio_only_fallback_mp4_extracts_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--audio-only --extract-audio: when m4a/mp3 return 403 but mp4 returns 200,
    the mp4 is downloaded, audio extracted to m4a, and mp4 deleted."""
    CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
    M4A_URL = f"https://cdn1.suno.ai/{CLIP}.m4a"
    MP3_URL = f"https://cdn1.suno.ai/{CLIP}.mp3"
    MP4_URL = f"https://cdn1.suno.ai/{CLIP}.mp4"

    call_log: list[tuple[str, int]] = []

    def handler(req):
        url = str(req.url)
        if url.endswith(".m4a") or url.endswith(".mp3"):
            call_log.append((url, 403))
            return httpx.Response(403)
        if url.endswith(".mp4"):
            call_log.append((url, 200))
            return httpx.Response(200, content=_VALID_MEDIA)
        return httpx.Response(404)

    # Mock extraction to succeed
    async def mock_extract(src: Path, dest: Path) -> bool:
        dest.write_bytes(b"extracted-audio")
        return True

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", mock_extract)

    f = _fetcher(handler)
    track = _track(audio_url=M4A_URL, media_urls=[M4A_URL, MP3_URL, MP4_URL])
    stats = await download_tracks(
        [track], f, 1, tmp_path, "{title}", "h", audio_only=True, extract_audio=True
    )
    assert stats["downloaded"] == 1
    assert stats["failed"] == 0
    assert stats["skipped_video"] == 0
    # Should have the .m4a file, not the .mp4
    assert (tmp_path / "Song.m4a").exists()
    assert not (tmp_path / "Song.mp4").exists()
    # Verify calls: m4a (403), mp3 (403), mp4 (200)
    assert len(call_log) == 3
    assert call_log[0][1] == 403  # m4a
    assert call_log[1][1] == 403  # mp3
    assert call_log[2][1] == 200  # mp4
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_audio_only_no_extract_audio_skips_when_only_mp4(tmp_path: Path) -> None:
    """--audio-only without --extract-audio: track with only mp4 is skipped."""
    CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
    MP4_URL = f"https://cdn1.suno.ai/{CLIP}.mp4"

    def handler(req):
        raise AssertionError("video-only track must not be fetched without --extract-audio")

    f = _fetcher(handler)
    track = _track(audio_url=MP4_URL, media_urls=[MP4_URL])
    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h", audio_only=True)
    assert stats["skipped_video"] == 1
    assert stats["downloaded"] == 0
    assert list(_no_sidecars(tmp_path)) == []
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_audio_only_fallback_fails_without_extract_audio_flag(tmp_path: Path) -> None:
    """--audio-only without --extract-audio: when m4a/mp3 fail, track fails (no mp4 fallback)."""
    CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
    M4A_URL = f"https://cdn1.suno.ai/{CLIP}.m4a"
    MP3_URL = f"https://cdn1.suno.ai/{CLIP}.mp3"
    MP4_URL = f"https://cdn1.suno.ai/{CLIP}.mp4"

    def handler(req):
        url = str(req.url)
        if url.endswith(".m4a") or url.endswith(".mp3"):
            return httpx.Response(403)
        if url.endswith(".mp4"):
            return httpx.Response(200, content=_VALID_MEDIA)
        return httpx.Response(404)

    f = _fetcher(handler)
    track = _track(audio_url=M4A_URL, media_urls=[M4A_URL, MP3_URL, MP4_URL])
    stats = await download_tracks(
        [track], f, 1, tmp_path, "{title}", "h", audio_only=True, extract_audio=False
    )
    # Audio candidates failed, extract_audio=False so no mp4 fallback
    assert stats["failed"] == 1
    assert stats["downloaded"] == 0
    assert list(_no_sidecars(tmp_path)) == []
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_audio_only_fallback_mp4_extraction_failure_when_no_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--audio-only --extract-audio: if ffmpeg is missing, track fails cleanly."""
    CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
    M4A_URL = f"https://cdn1.suno.ai/{CLIP}.m4a"
    MP4_URL = f"https://cdn1.suno.ai/{CLIP}.mp4"

    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("SUNO_FFMPEG", raising=False)

    def handler(req):
        url = str(req.url)
        if url.endswith(".m4a"):
            return httpx.Response(403)
        if url.endswith(".mp4"):
            return httpx.Response(200, content=_VALID_MEDIA)
        return httpx.Response(404)

    f = _fetcher(handler)
    track = _track(audio_url=M4A_URL, media_urls=[M4A_URL, MP4_URL])
    stats = await download_tracks(
        [track], f, 1, tmp_path, "{title}", "h", audio_only=True, extract_audio=True
    )
    # Extraction failed due to missing ffmpeg
    assert stats["failed"] == 1
    assert stats["downloaded"] == 0
    assert list(_no_sidecars(tmp_path)) == []
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_audio_only_fallback_preserves_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--audio-only --extract-audio: extracted m4a has correct artist/title tags."""
    from suno_dl.tagging import TagResult

    CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
    M4A_URL = f"https://cdn1.suno.ai/{CLIP}.m4a"
    MP4_URL = f"https://cdn1.suno.ai/{CLIP}.mp4"

    def handler(req):
        url = str(req.url)
        if url.endswith(".m4a"):
            return httpx.Response(403)
        if url.endswith(".mp4"):
            return httpx.Response(200, content=_VALID_MEDIA)
        return httpx.Response(404)

    # Mock extraction to succeed
    async def mock_extract(src: Path, dest: Path) -> bool:
        # Write a minimal valid M4A that mutagen can tag
        dest.write_bytes(_VALID_MEDIA)
        return True

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", mock_extract)

    # Also mock tag_file to succeed on the M4A
    monkeypatch.setattr(
        "suno_dl.downloader.tag_file",
        lambda path, artist, title, lyrics=None: TagResult(True, "mutagen", artist, title, ""),
    )

    f = _fetcher(handler)
    track = _track(id=CLIP, title="Test Song", audio_url=M4A_URL, media_urls=[M4A_URL, MP4_URL])
    stats = await download_tracks(
        [track], f, 1, tmp_path, "{handle}_{title}", "myhandle", audio_only=True, extract_audio=True
    )
    assert stats["downloaded"] == 1
    # Tagging happens twice: once on temp MP4, once on final M4A
    assert stats["tagged"] == 2
    m4a_path = tmp_path / "myhandle_Test Song.m4a"
    assert m4a_path.exists()
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_audio_only_fallback_covers_still_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--audio-only --extract-audio --covers: cover is downloaded after extraction."""
    CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
    M4A_URL = f"https://cdn1.suno.ai/{CLIP}.m4a"
    MP4_URL = f"https://cdn1.suno.ai/{CLIP}.mp4"
    COVER_URL = f"https://cdn2.suno.ai/image_{CLIP}.jpeg"

    def handler(req):
        url = str(req.url)
        if url.endswith(".m4a"):
            return httpx.Response(403)
        if url.endswith(".mp4"):
            return httpx.Response(200, content=_VALID_MEDIA)
        if url.endswith(".jpeg"):
            return httpx.Response(200, content=b"cover-bytes")
        return httpx.Response(404)

    # Mock extraction to succeed
    async def mock_extract(src: Path, dest: Path) -> bool:
        dest.write_bytes(b"extracted-audio")
        return True

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", mock_extract)

    f = _fetcher(handler)
    track = _track(
        audio_url=M4A_URL,
        media_urls=[M4A_URL, MP4_URL],
        cover_url=COVER_URL,
    )
    stats = await download_tracks(
        [track], f, 1, tmp_path, "{title}", "h", audio_only=True, extract_audio=True, covers=True
    )
    assert stats["downloaded"] == 1
    assert (tmp_path / "Song.m4a").exists()
    assert (tmp_path / "Song.cover.jpeg").exists()
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_audio_only_fallback_prefers_audio_when_available(tmp_path: Path) -> None:
    """--audio-only --extract-audio: if m4a works, mp4 is never tried."""
    CLIP = "a1b2c3d4-1111-4111-8111-111111111111"
    M4A_URL = f"https://cdn1.suno.ai/{CLIP}.m4a"
    MP4_URL = f"https://cdn1.suno.ai/{CLIP}.mp4"

    call_log: list[str] = []

    def handler(req):
        url = str(req.url)
        call_log.append(url)
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track(audio_url=M4A_URL, media_urls=[M4A_URL, MP4_URL])
    stats = await download_tracks(
        [track], f, 1, tmp_path, "{title}", "h", audio_only=True, extract_audio=True
    )
    assert stats["downloaded"] == 1
    # Only m4a should have been called
    assert call_log == [M4A_URL]
    assert (tmp_path / "Song.m4a").exists()
    assert not (tmp_path / "Song.mp4").exists()
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_extract_audio_flag_without_audio_only_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing CLI/HA --videos --extract-audio still deletes the temporary MP4."""

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    async def mock_extract(src: Path, dest: Path) -> bool:
        dest.write_bytes(b"extracted-audio")
        return True

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", mock_extract)
    f = _fetcher(handler)
    track = _track(audio_url=VIDEO, media_urls=[VIDEO])
    stats = await download_tracks(
        [track], f, 1, tmp_path, "{title}", "h", audio_only=False, extract_audio=True
    )
    assert stats["downloaded"] == 1
    assert (tmp_path / "Song.m4a").exists()
    assert not (tmp_path / "Song.mp4").exists()
    await f.__aexit__()


# --- #92: explicit keep-video for the WebUI only ------------------------------


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_keep_video_saves_both_with_tags_sidecar_and_idempotent_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prefer MP4 despite an audio-first URL; save, tag, inventory and resume both."""
    from suno_dl.tagging import TagResult

    calls: list[str] = []
    tagged: list[str] = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, content=_VALID_MEDIA)

    async def mock_extract(src: Path, dest: Path) -> bool:
        assert src.suffix == ".mp4"
        dest.write_bytes(_VALID_MEDIA)
        return True

    def fake_tag(path, artist, title, lyrics=None):
        tagged.append(path.suffix)
        return TagResult(True, "synthetic", artist, title, "")

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", mock_extract)
    monkeypatch.setattr("suno_dl.downloader.tag_file", fake_tag)
    f = _fetcher(handler)
    track = _track(audio_url=GOOD, media_urls=[GOOD, VIDEO])
    first = await download_tracks(
        [track],
        f,
        1,
        tmp_path,
        "{title}",
        "h",
        audio_only=False,
        extract_audio=True,
        keep_video=True,
        sidecar=True,
    )
    assert first["downloaded"] == 1 and first["failed"] == 0
    assert calls == [VIDEO]
    assert (tmp_path / "Song.mp4").is_file()
    assert (tmp_path / "Song.m4a").is_file()
    import json

    sidecar = json.loads((tmp_path / "Song.json").read_text())
    assert sidecar["saved_files"] == ["Song.m4a", "Song.mp4"]
    assert ".mp4" in tagged and ".m4a" in tagged
    before = [
        (p.name, p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.iterdir()
        if p.name != ".library"
    ]
    second = await download_tracks(
        [track],
        f,
        1,
        tmp_path,
        "{title}",
        "h",
        audio_only=False,
        extract_audio=True,
        keep_video=True,
        sidecar=True,
    )
    assert second["downloaded"] == 0 and second["skipped_exists"] == 1
    assert calls == [VIDEO]
    assert before == [
        (p.name, p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.iterdir()
        if p.name != ".library"
    ]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_keep_video_upgrades_audio_and_existing_sidecar_without_overwriting_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prior audio-only archive can gain MP4 without overwriting its M4A."""
    import json

    from suno_dl.sidecar import build_sidecar, write_sidecar

    audio = tmp_path / "Song.m4a"
    audio.write_bytes(_VALID_MEDIA)
    track = _track(audio_url=GOOD, media_urls=[GOOD, VIDEO])
    sidecar = tmp_path / "Song.json"
    assert write_sidecar(sidecar, build_sidecar(track, handle="h", saved_files=[audio.name]))
    original_audio = (audio.read_bytes(), audio.stat().st_mtime_ns)
    calls: list[str] = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, content=_VALID_MEDIA)

    async def mock_extract(src: Path, dest: Path) -> bool:
        raise AssertionError("Existing M4A should not be re-extracted")

    monkeypatch.setattr("suno_dl.downloader._extract_audio_from_mp4", mock_extract)
    f = _fetcher(handler)
    stats = await download_tracks(
        [track],
        f,
        1,
        tmp_path,
        "{title}",
        "h",
        extract_audio=True,
        keep_video=True,
        sidecar=True,
        audio_only=False,
    )
    assert stats["downloaded"] == 1
    assert calls == [VIDEO]
    assert (audio.read_bytes(), audio.stat().st_mtime_ns) == original_audio
    assert (tmp_path / "Song.mp4").exists()
    assert json.loads(sidecar.read_text())["saved_files"] == ["Song.m4a", "Song.mp4"]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_video_enrichment_failure_never_overwrites_existing_audio(
    tmp_path: Path,
) -> None:
    """Unavailable MP4 must not force a redundant or destructive audio request."""
    audio = tmp_path / "Song.m4a"
    audio.write_bytes(_VALID_MEDIA)
    original = (audio.read_bytes(), audio.stat().st_mtime_ns)
    calls: list[str] = []

    def handler(req):
        calls.append(str(req.url))
        if str(req.url) == VIDEO:
            return httpx.Response(403)
        raise AssertionError("existing audio must not be fetched or overwritten")

    f = _fetcher(handler)
    from suno_dl.sidecar import build_sidecar, write_sidecar

    track = _track(audio_url=GOOD, media_urls=[GOOD, VIDEO])
    assert write_sidecar(
        tmp_path / "Song.json", build_sidecar(track, handle="h", saved_files=[audio.name])
    )
    stats = await download_tracks(
        [track],
        f,
        1,
        tmp_path,
        "{title}",
        "h",
        extract_audio=True,
        keep_video=True,
        sidecar=False,
        audio_only=False,
    )
    assert stats["downloaded"] == 0 and stats["skipped_exists"] == 1
    assert calls == [VIDEO]
    assert not (tmp_path / "Song.mp4").exists()
    assert (audio.read_bytes(), audio.stat().st_mtime_ns) == original
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_keep_video_backfills_audio_from_existing_mp4_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved video missing audio is remuxed locally with correct sidecar inventory."""
    import json

    from suno_dl.sidecar import build_sidecar, write_sidecar

    track = _track(audio_url=VIDEO, media_urls=[VIDEO])
    mp4 = tmp_path / "Song.mp4"
    mp4.write_bytes(_VALID_MEDIA)
    write_sidecar(tmp_path / "Song.json", build_sidecar(track, handle="h", saved_files=[mp4.name]))

    def handler(req):
        raise AssertionError("existing video must not be downloaded again")

    def fake_remux(paths):
        assert paths == [mp4]
        m4a = tmp_path / "Song.m4a"
        m4a.write_bytes(_VALID_MEDIA)
        return [m4a]

    monkeypatch.setattr("suno_dl.downloader.extract_audio_files", fake_remux)
    f = _fetcher(handler)
    stats = await download_tracks(
        [track],
        f,
        1,
        tmp_path,
        "{title}",
        "h",
        extract_audio=True,
        keep_video=True,
        sidecar=True,
        audio_only=False,
    )
    assert stats["downloaded"] == 0 and stats["skipped_exists"] == 1
    assert mp4.exists() and (tmp_path / "Song.m4a").exists()
    assert json.loads((tmp_path / "Song.json").read_text())["saved_files"] == [
        "Song.m4a",
        "Song.mp4",
    ]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_keep_video_remux_failure_retains_video_without_spurious_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing ffmpeg must never delete the original video or claim an M4A exists."""
    monkeypatch.setattr("suno_dl.downloader.find_ffmpeg", lambda: None)
    f = _fetcher(lambda req: httpx.Response(200, content=_VALID_MEDIA))
    track = _track(audio_url=VIDEO, media_urls=[VIDEO])
    stats = await download_tracks(
        [track],
        f,
        1,
        tmp_path,
        "{title}",
        "h",
        extract_audio=True,
        keep_video=True,
        sidecar=True,
        audio_only=False,
    )
    assert stats["downloaded"] == 1 and stats["failed"] == 0
    assert (tmp_path / "Song.mp4").exists()
    assert not (tmp_path / "Song.m4a").exists()
    import json

    assert json.loads((tmp_path / "Song.json").read_text())["saved_files"] == ["Song.mp4"]
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_keep_video_does_not_bypass_nonrotating_archive_cap(
    tmp_path: Path,
) -> None:
    """Existing M4A stays safe if the library is full and rotation is disabled."""
    audio = tmp_path / "Song.m4a"
    already_full = _VALID_MEDIA + b"X" * (1024 * 1024)
    audio.write_bytes(already_full)
    f = _fetcher(lambda req: (_ for _ in ()).throw(AssertionError("cap must block video")))
    from suno_dl.sidecar import build_sidecar, write_sidecar

    track = _track(audio_url=GOOD, media_urls=[GOOD, VIDEO])
    assert write_sidecar(
        tmp_path / "Song.json", build_sidecar(track, handle="h", saved_files=[audio.name])
    )
    stats = await download_tracks(
        [track],
        f,
        1,
        tmp_path,
        "{title}",
        "h",
        extract_audio=True,
        keep_video=True,
        max_tracks=1,
        max_disk_mb=1,
        rotate=False,
        sidecar=False,
        audio_only=False,
    )
    assert stats["downloaded"] == 0 and stats["skipped_capped"] == 1
    assert audio.read_bytes() == already_full
    assert not (tmp_path / "Song.mp4").exists()
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_keep_video_real_offline_ffmpeg_both_containers_and_tags(
    tmp_path: Path,
) -> None:
    """Actual synthetic AAC-in-MP4 fixture remuxes into a separately tagged M4A."""
    import json
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("existing ffmpeg/ffprobe required; no installation")
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
            "sine=frequency=523:duration=0.5",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-y",
            str(fixture),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    payload = fixture.read_bytes()
    assert len(payload) > 1024
    fixture.unlink()

    calls: list[str] = []

    def handler(req):
        calls.append(str(req.url))
        assert str(req.url) == VIDEO
        return httpx.Response(200, content=payload)

    f = _fetcher(handler)
    track = _track(
        audio_url=GOOD, media_urls=[GOOD, VIDEO], title="Real Synthetic Song", artist="drinko"
    )
    stats = await download_tracks(
        [track],
        f,
        1,
        tmp_path,
        "{title}",
        "drinko",
        extract_audio=True,
        keep_video=True,
        sidecar=True,
        audio_only=False,
    )
    assert stats["downloaded"] == 1 and calls == [VIDEO]
    for ext in (".m4a", ".mp4"):
        path = tmp_path / ("Real Synthetic Song" + ext)
        assert path.is_file() and path.stat().st_size > 1024
        report = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format_tags=artist,title",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        tags = json.loads(report.stdout)["format"]["tags"]
        assert tags["artist"] == "drinko"
        assert tags["title"] == "Real Synthetic Song"
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_video_off_never_requests_video_only_media_even_if_keep_requested(
    tmp_path: Path,
) -> None:
    """Even a direct downloader caller cannot bypass audio_only with keep_video=True."""
    f = _fetcher(lambda req: (_ for _ in ()).throw(AssertionError("unexpected media fetch")))
    track = _track(audio_url=VIDEO, media_urls=[VIDEO])
    stats = await download_tracks(
        [track],
        f,
        1,
        tmp_path,
        "{title}",
        "h",
        audio_only=True,
        extract_audio=False,
        keep_video=True,
    )
    assert stats["downloaded"] == 0 and stats["skipped_video"] == 1
    assert list(_no_sidecars(tmp_path)) == []
    await f.__aexit__()


# --- disk guard (min_free_mb) -----------------------------------------------


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_disk_guard_stops_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When free space is below min_free_mb, download_tracks raises DiskSpaceError."""
    # Mock shutil.disk_usage to return low free space
    import shutil

    from suno_dl.downloader import download_tracks
    from suno_dl.errors import DiskSpaceError

    class FakeUsage:
        free = 100 * 1024 * 1024  # 100 MB

    monkeypatch.setattr(shutil, "disk_usage", lambda _: FakeUsage())

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track(audio_url=GOOD, media_urls=[GOOD])

    with pytest.raises(DiskSpaceError, match="Insufficient free space"):
        await download_tracks([track], f, 1, tmp_path, "{title}", "h", min_free_mb=2048)

    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_disk_guard_allows_when_above_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When free space is above min_free_mb, downloads proceed normally."""
    import shutil

    from suno_dl.downloader import download_tracks

    class FakeUsage:
        free = 5 * 1024 * 1024 * 1024  # 5 GB

    monkeypatch.setattr(shutil, "disk_usage", lambda _: FakeUsage())

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track(audio_url=GOOD, media_urls=[GOOD])

    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h", min_free_mb=2048)
    assert stats["downloaded"] == 1
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_disk_guard_disabled_when_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """min_free_mb=0 disables the check (unlimited)."""
    import shutil

    from suno_dl.downloader import download_tracks

    class FakeUsage:
        free = 1  # 1 byte — would fail any positive threshold

    monkeypatch.setattr(shutil, "disk_usage", lambda _: FakeUsage())

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track = _track(audio_url=GOOD, media_urls=[GOOD])

    stats = await download_tracks([track], f, 1, tmp_path, "{title}", "h", min_free_mb=0)
    assert stats["downloaded"] == 1
    await f.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_disk_guard_checked_before_each_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disk space is checked before each track, not just once at start."""
    import shutil

    from suno_dl.downloader import download_tracks
    from suno_dl.errors import DiskSpaceError

    # First check: plenty of space. Second check: below threshold.
    calls = {"n": 0}

    class FakeUsage:
        @property
        def free(self):
            calls["n"] += 1
            if calls["n"] == 1:
                return 5 * 1024 * 1024 * 1024  # 5 GB for first track
            return 100 * 1024 * 1024  # 100 MB for second track

    monkeypatch.setattr(shutil, "disk_usage", lambda _: FakeUsage())

    def handler(req):
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    track1 = _track(id="aaaaaaaa-1111-4111-8111-111111111111", audio_url=GOOD, media_urls=[GOOD])
    track2 = _track(id="bbbbbbbb-2222-4222-8222-222222222222", audio_url=GOOD, media_urls=[GOOD])

    with pytest.raises(DiskSpaceError, match="Insufficient free space"):
        await download_tracks([track1, track2], f, 1, tmp_path, "{title}", "h", min_free_mb=2048)

    assert calls["n"] == 2
    await f.__aexit__()


def test_check_disk_space_returns_none_when_disabled(tmp_path: Path) -> None:
    """_check_disk_space returns None when min_free_mb <= 0."""
    from suno_dl.downloader import _check_disk_space

    # Should not raise
    _check_disk_space(tmp_path, 0)
    _check_disk_space(tmp_path, -1)


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.parametrize("creator", ["SmileDJ", "Æsthaeth 🎵 e\u0301", "Ricktron 5000"])
async def test_explicit_per_track_creator_beats_source_handle_in_embedded_tags(
    tmp_path: Path, creator: str
) -> None:
    """#157: a public playlist/profile source is not the clip's producer."""
    from mutagen.mp4 import MP4

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_real_m4a())

    fetcher = _fetcher(handler)
    try:
        track = _track(
            artist=creator, title="Mixed Creator / Song?", audio_url=GOOD, media_urls=[GOOD]
        )
        stats = await download_tracks([track], fetcher, 1, tmp_path, "{handle}_{title}", "drinko")
        assert stats["downloaded"] == stats["tagged"] == 1
        (audio,) = tmp_path.glob("*.m4a")
        assert "/" not in audio.name and "?" not in audio.name
        assert "drinko_" in audio.name  # source naming remains stable
        assert MP4(audio)["\xa9ART"] == [creator]  # independent tag reader
        assert MP4(audio)["\xa9nam"] == [track.title]
    finally:
        await fetcher.__aexit__()


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
@pytest.mark.parametrize("outcome", ["wrong_id", "unavailable", "malformed"])
async def test_playlist_unverified_song_page_never_credits_owner_or_other_song(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    import json
    from types import SimpleNamespace

    import suno_dl.downloader as downloader
    from suno_dl.parser import parse_playlist

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "suno.com":
            return httpx.Response(503 if outcome == "unavailable" else 200, text="synthetic")
        return httpx.Response(200, content=_VALID_MEDIA)

    f = _fetcher(handler)
    (track,) = parse_playlist(f'<a href="/song/{CLIP}">Song</a>')
    other = _track(id="b2c3d4e5-2222-4222-8222-222222222222", artist="Wrong Producer")

    def parse(_):
        if outcome == "malformed":
            raise ValueError("untrusted malformed page")
        return [other]

    monkeypatch.setattr(downloader, "parse_song", parse)
    credited: list[str] = []

    def fake_tag(path, *, artist, title, lyrics=None):
        credited.append(artist)
        return SimpleNamespace(tagged=False, backend="fake", reason="unknown")

    monkeypatch.setattr(downloader, "tag_file", fake_tag)
    stats = await download_tracks(
        [track], f, 1, tmp_path, "{title}", "playlist_owner", audio_only=False
    )
    await f.__aexit__()
    assert stats["downloaded"] == 1
    assert credited == ["Unknown artist"]
    assert json.loads((tmp_path / "Song.json").read_text())["artist"] is None


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_uncertain_tag_publication_keeps_intent_and_cancels_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl import download_intent, tagging

    clip2 = "a1b2c3d4-2222-4222-8222-222222222222"
    url2 = f"https://cdn1.suno.ai/{clip2}.m4a"
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    audio = (Path(__file__).parent / "fixtures" / "tiny.m4a").read_bytes()

    async def handler(req):
        if clip2 in str(req.url):
            sibling_started.set()
            try:
                await asyncio.Future()
            finally:
                sibling_cancelled.set()
        await sibling_started.wait()
        return httpx.Response(200, content=audio)

    original_replace = tagging.os.replace
    original_fsync = tagging.os.fsync
    replaced = False

    def observed_replace(src, dst):
        nonlocal replaced
        original_replace(src, dst)
        if Path(src).name == "audio.part":
            replaced = True

    def fail_after_tag_replace(fd):
        if replaced:
            raise OSError("injected post-replacement fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(tagging.os, "replace", observed_replace)
    monkeypatch.setattr(tagging.os, "fsync", fail_after_tag_replace)
    first = _track(title="First", artist="Producer", audio_url=GOOD, media_urls=[GOOD])
    second = _track(
        id=clip2,
        title="Second",
        artist="Producer",
        page_url=f"https://suno.com/song/{clip2}",
        audio_url=url2,
        media_urls=[url2],
    )
    fetcher = _fetcher(handler)
    try:
        with pytest.raises(tagging.TagPublicationUncertain):
            await asyncio.wait_for(
                download_tracks([first, second], fetcher, 2, tmp_path, "{title}", "owner"),
                timeout=10,
            )
    finally:
        await fetcher.__aexit__()
    assert replaced and sibling_cancelled.is_set()
    assert (tmp_path / "First.m4a").exists()
    assert not (tmp_path / "First.json").exists()
    assert not (tmp_path / "Second.m4a").exists()
    marker = tmp_path / ".library" / "download-intents" / f"{CLIP}.json"
    assert marker.is_file()
    with pytest.raises(download_intent.UnsafeDownloadIntent, match="interrupted publication"):
        download_intent.prepare(tmp_path, CLIP)


@pytest.mark.parametrize("failure", ["http", "request"])
async def test_signed_transfer_failures_do_not_leak_tokens_in_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    import logging

    from suno_dl.downloader import _download_cover, _try_download_candidate

    media = GOOD + "?token=MEDIA_SECRET"
    cover = COVER + "?token=COVER_SECRET"
    track = _track(audio_url=media, media_urls=[media], cover_url=cover)

    def handler(request):
        if failure == "http":
            return httpx.Response(500)
        raise httpx.ConnectError("request failed " + str(request.url), request=request)

    fetcher = _fetcher(handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="suno_dl.downloader"):
            cover_result = await _download_cover(track, tmp_path / "Song", tmp_path, fetcher)
            media_result = await _try_download_candidate(
                media,
                track,
                tmp_path / "Song",
                tmp_path,
                fetcher,
                {"downloaded": 0},
                None,
                "h",
                False,
            )
    finally:
        await fetcher.__aexit__()
    assert cover_result is None
    assert media_result == "failed"
    records = [
        record.getMessage() for record in caplog.records if record.name == "suno_dl.downloader"
    ]
    output = "\n".join(records)
    assert "Cover fetch failed" in output
    assert "Media candidate failed" in output
    assert "MEDIA_SECRET" not in output and "COVER_SECRET" not in output
    assert "?...redacted" in output


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_untrusted_title_cannot_forge_blocked_or_dry_run_output(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    title = "Song\nFORGED\u202eBIDI"
    blocked = _track(title=title)
    other_id = "a1b2c3d4-2222-4222-8222-222222222222"
    visible = _track(id=other_id, title=title)
    fetcher = _fetcher(lambda _request: pytest.fail("dry run must not access network"))
    try:
        stats = await download_tracks(
            [blocked, visible],
            fetcher,
            1,
            tmp_path,
            "{title}",
            "h",
            dry_run=True,
            blocked_ids=frozenset({CLIP}),
        )
    finally:
        await fetcher.__aexit__()
    output = capsys.readouterr().out
    assert stats["skipped_blocked"] == 1
    assert "\nFORGED" not in output and "\u202e" not in output
    assert r"\u000aFORGED\u202eBIDI" in output
    assert blocked.title == title and visible.title == title


async def test_untrusted_title_cannot_forge_cap_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from suno_dl.downloader import DownloadBudget, _download_single

    title = "Song\nFORGED\u202eBIDI"
    track = _track(title=title)
    fetcher = _fetcher(lambda _request: pytest.fail("capped track must not access network"))
    budget = DownloadBudget(tracks_left=0, bytes_left=None)
    try:
        with caplog.at_level(logging.DEBUG, logger="suno_dl.downloader"):
            await _download_single(
                track,
                tmp_path / "Song",
                tmp_path,
                fetcher,
                asyncio.Semaphore(1),
                {"skipped_capped": 0},
                None,
                budget=budget,
            )
    finally:
        await fetcher.__aexit__()
    assert "\nFORGED" not in caplog.text and "\u202e" not in caplog.text
    assert r"\u000aFORGED\u202eBIDI" in caplog.text
    assert track.title == title


@pytest.mark.parametrize("branch", ["missing_pillow", "non_jpeg", "recompressed", "bad_image"])
def test_cover_compression_escapes_untrusted_filename_in_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, branch: str
) -> None:
    import logging
    import sys

    from suno_dl.downloader import _compress_image

    suffix = ".png" if branch == "non_jpeg" else ".jpg"
    cover = tmp_path / ("cover\nFORGED\u202eBIDI" + suffix)
    cover.write_bytes(b"x" * 512)
    if branch == "missing_pillow":
        monkeypatch.setitem(sys.modules, "PIL", None)
    elif branch == "bad_image":
        _install_fake_pillow(monkeypatch, ValueError("bad image"))
    else:
        _install_fake_pillow(monkeypatch, b"small")
    with caplog.at_level(logging.DEBUG, logger="suno_dl.downloader"):
        _compress_image(cover)
    assert "\nFORGED" not in caplog.text
    assert "\u202e" not in caplog.text
    assert r"\u000aFORGED\u202eBIDI" in caplog.text
    assert cover.exists()
