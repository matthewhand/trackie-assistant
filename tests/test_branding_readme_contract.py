"""Guard the user-facing brand and public repository hygiene."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
MASCOT = ROOT / "src/suno_dl/webui_static/trackie-dacks.svg"
ARTWORK = ROOT / "docs/trackie-assistant-logo.webp"


def test_branding_is_user_facing_without_breaking_legacy_identifiers() -> None:
    text = README.read_text()
    assert text.startswith("# Trackie Assistant\n\n![")
    assert "**Meet Trackie Dacks.**" in text
    assert "Brand preview, not a software rename" not in text
    assert "## Compatibility with existing installations" in text
    assert "user-facing product name" in text
    for command in ("suno-download-cli", "suno-dl", "suno-webui", "suno-recommend"):
        assert command in text
    assert "Home Assistant ingress deliberately does not retain MP4" in text
    assert "anonymous HTTP feed is attempted" in text
    assert "nonrotating disk" in text
    assert "own or have permission" in text
    assert "Playwright** (a real browser) and" not in text


def test_original_art_and_decorative_svg_are_present() -> None:
    assert ARTWORK.read_bytes()[:4] == b"RIFF"
    assert ARTWORK.read_bytes()[8:12] == b"WEBP"
    text = MASCOT.read_text()
    assert '<title id="trackie-title">Trackie Dacks</title>' in text
    assert "prefers-reduced-motion:reduce" in text
    assert "animation:none!important" in text
    assert "Tracky Dacks" not in text
    assert (
        ROOT / "src/suno_dl/webui_static/trackie-assistant-logo.webp"
    ).read_bytes() == ARTWORK.read_bytes()
