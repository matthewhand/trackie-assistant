"""Distribution docs must describe the actual guarded Linux draft."""

from pathlib import Path

import pytest

from suno_dl.tagging import TaggingUnavailable, require_embedded_tag_writes


def test_guarded_draft_status_is_visible_in_distribution_docs():
    with pytest.raises(TaggingUnavailable):
        require_embedded_tag_writes()
    root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "docs/usage.md", "addon/suno_dl/DOCS.md"):
        opening = (root / name).read_text(encoding="utf-8")[:1000].lower()
        assert "linux preview 1.6.2rc1" in opening, name
        assert "linux audio sync and previews verify producers" in opening, name
        assert "existing-file repairs require the reviewed offline migration" in opening, name
        assert "indexed recordings are reused unchanged" in opening, name
