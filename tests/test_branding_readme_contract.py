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
    assert "Trackie Dacks" in text
    assert "## Compatibility with existing installations" in text
    for command in ("suno-download-cli", "suno-dl", "suno-webui", "suno-recommend"):
        assert command in text
    for guide in ("docs/usage.md", "addon/suno_dl/DOCS.md", "docs/releases/v1.6.2rc1.md"):
        assert guide in text
        assert (ROOT / guide).is_file()
    assert "own or have permission" in text


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
