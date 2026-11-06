"""Tests for filename safety: traversal, reserved names, collisions.

These lock in the guarantees that `sanitize_component` / `build_filename` must
uphold, so a future refactor cannot silently reintroduce an arbitrary-write bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from suno_dl.naming import build_filename, sanitize_component


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/cron.d/evil",
        "..\\..\\..\\windows\\system32\\evil",
        "/absolute/path",
        "C:\\Windows\\evil",
        "~/.ssh/authorized_keys",
        "....//....//etc",
        "a/b/c",
        "a\x00b",
        "\n\t\r",
        "%2e%2e%2f",
        "  ",
        ".",
        "..",
        "",
    ],
)
def test_sanitize_never_escapes_component(hostile: str) -> None:
    out = sanitize_component(hostile)
    assert "/" not in out
    assert "\\" not in out
    assert not out.startswith(".")
    assert out not in (".", "..")
    assert out  # never empty


@pytest.mark.parametrize("reserved", ["CON", "con", "NUL", "COM1", "LPT9", "AUX", "PRN"])
def test_windows_reserved_names_are_neutralised(reserved: str) -> None:
    out = sanitize_component(reserved)
    assert out.upper().split(".", 1)[0] != reserved.upper()
    assert out.upper().startswith("_")


def test_build_filename_contains_output(tmp_path: Path) -> None:
    hostile_pattern = "../../{title}"
    name = build_filename(hostile_pattern, title="../../etc/passwd", handle="h", clip_id="id")
    assert "/" not in name and "\\" not in name
    resolved = (tmp_path / name).resolve()
    assert resolved.parent == tmp_path.resolve()


def test_build_filename_unknown_placeholder_falls_back() -> None:
    name = build_filename("{nope}", title="T", handle="H", clip_id="I")
    assert name  # no exception, safe fallback
    assert "/" not in name


def test_build_filename_unicode_preserved() -> None:
    name = build_filename("{title}", title="日本語のうた 🎵", handle="h", clip_id="i")
    assert "日本語のうた" in name


def test_build_filename_is_bounded() -> None:
    name = build_filename("{title}", title="x" * 10_000, handle="h", clip_id="i")
    assert len(name) <= 180


# --- #70: media basenames must not look like they carry an extension ---------


@pytest.mark.parametrize(
    "title",
    [
        "Ajani, Nacatl Pariah (feat. Pyotr Kropotkin)",
        "Song (feat. Alice) - Pt. 2",
        "Version 2.0",
        "Mr. Blue Sky",
        "a.b.c",
    ],
)
def test_build_filename_has_no_period_in_basename(title: str) -> None:
    """A tag-guessing player splits on the first '.' and truncates there."""
    name = build_filename("{handle}_{title}", title=title, handle="drinko", clip_id="abc")
    assert "." not in name
    assert name.startswith("drinko_")


def test_build_filename_encodes_feat_as_underscore() -> None:
    name = build_filename(
        "{handle}_{title}", title="Song (feat. Alice)", handle="drinko", clip_id="abc"
    )
    assert name == "drinko_Song (feat_ Alice)"


def test_legacy_filename_keeps_the_pre_16_period() -> None:
    """The old name is still computed — to recognise an existing archive file."""
    title = "Song (feat. Alice)"
    old = build_filename(
        "{handle}_{title}", title=title, handle="drinko", clip_id="abc", ma_safe=False
    )
    new = build_filename("{handle}_{title}", title=title, handle="drinko", clip_id="abc")
    assert old == "drinko_Song (feat. Alice)"
    assert new == "drinko_Song (feat_ Alice)"


def test_legacy_and_current_names_agree_without_periods() -> None:
    """No period in the title means no rename, so no orphan on disk."""
    title = "Absolutely Certain That I Might Be Odd"
    old = build_filename(
        "{handle}_{title}", title=title, handle="drinko", clip_id="abc", ma_safe=False
    )
    new = build_filename("{handle}_{title}", title=title, handle="drinko", clip_id="abc")
    assert old == new


def test_ma_safe_basename_leaves_ordinary_names_alone() -> None:
    from suno_dl.naming import ma_safe_basename

    assert ma_safe_basename("drinko_Song - Pt 2") == "drinko_Song - Pt 2"
    assert ma_safe_basename("drinko_Song (feat. Alice)") == "drinko_Song (feat_ Alice)"


def test_source_slug_profiles_and_ids() -> None:
    from suno_dl.naming import source_slug

    assert source_slug("https://suno.com/@drinko") == "drinko"
    assert source_slug("https://suno.com/@drinko/") == "drinko"
    assert source_slug("https://suno.com/playlist/abc123/") == "abc123"
    assert source_slug("https://suno.com/song/xyz789") == "xyz789"


def test_source_slug_is_containment_safe() -> None:
    from suno_dl.naming import source_slug

    # Traversal and separator attempts collapse to a safe single component.
    assert "/" not in source_slug("https://suno.com/@../../etc")
    assert ".." not in source_slug("https://suno.com/@..")
    assert source_slug("https://suno.com/@") == "downloads"
    assert source_slug("https://suno.com") == "downloads"
    # Long handles are bounded.
    assert len(source_slug("https://suno.com/@" + "x" * 500)) <= 64
