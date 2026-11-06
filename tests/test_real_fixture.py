"""Parser tests against a *real* captured Suno song page.

The synthetic fixtures in test_parser.py build their payload by re-encoding
JSON in Python, which cannot catch divergences in Suno's actual escaping
(e.g. ``\u0026`` in signed URLs). This test parses a real, signed-URL-redacted
page so those divergences fail loudly.
"""

from __future__ import annotations

from pathlib import Path

from suno_dl.parser import parse_song

FIXTURE = Path(__file__).parent / "fixtures" / "real_song_page.html"
CLIP_ID = "3e6c3f60-7a43-4ccb-a4e4-fc35d414e4d6"


def test_real_song_page_parses() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    tracks = parse_song(html)
    assert len(tracks) == 1
    track = tracks[0]
    assert track.id == CLIP_ID
    assert track.title
    assert track.page_url.endswith(CLIP_ID)


def test_real_page_yields_allowlisted_media() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    track = parse_song(html)[0]
    assert track.media_urls, "real payload must yield at least one media URL"
    for url in track.media_urls:
        assert url.startswith("https://")
        assert "suno.ai" in url or "suno.com" in url or "cloudfront.net" in url


def test_real_page_has_no_signature_material() -> None:
    """Guard against committing credentials with the fixture."""
    html = FIXTURE.read_text(encoding="utf-8")
    assert "sig=" not in html
    assert "\\u0026sig" not in html
