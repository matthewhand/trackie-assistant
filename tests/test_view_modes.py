"""Source-only three-view-mode contract checks. No HTTP server or CLI jobs."""

from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from suno_dl.webui import _PAGE, _STATIC_FILES, _static_file

ROOT = Path(__file__).resolve().parents[1]
WEBUI_STATIC = ROOT / "src" / "suno_dl" / "webui_static"


class Inventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        props = dict(attrs)
        self.tags.append((tag, props))
        if props.get("id"):
            self.ids.append(props["id"] or "")


def inventory() -> Inventory:
    parsed = Inventory()
    parsed.feed(_PAGE)
    parsed.close()
    return parsed


def test_all_three_modes_and_appearance_settings_have_accessible_selects() -> None:
    doc = inventory()
    assert len(doc.ids) == len(set(doc.ids)), "duplicate element IDs"
    for element in ("view-mode", "appearance-mode", "density-mode"):
        assert element in doc.ids
    for value in ("classic", "studio", "minimal", "system", "dark", "light", "contrast"):
        assert ("option", {"value": value}) in doc.tags
    assert (
        "nav",
        {"class": "feature-nav", "aria-label": "Dashboard features", "id": "feature-nav"},
    ) in doc.tags


def test_every_feature_has_a_panel_and_a_navigation_entry() -> None:
    doc = inventory()
    for feature in ("library", "sync", "recommend", "activity"):
        panel = f"panel-{feature}"
        assert panel in doc.ids
        assert any(
            tag == "button"
            and props.get("data-section") == feature
            and props.get("aria-controls") == panel
            for tag, props in doc.tags
        )
        assert any(
            props.get("id") == panel and props.get("data-panel") == feature for _, props in doc.tags
        )
    assert doc.ids.count("lib") == 1
    assert doc.ids.count("sync-real") == 1
    assert doc.ids.count("rec") == 1
    assert doc.ids.count("opbar") == 1


def test_theme_and_responsive_layout_contract() -> None:
    for css in (
        'body[data-view="studio"]',
        'body[data-view="minimal"]',
        'body[data-theme="light"]',
        'body[data-theme="contrast"]',
        'body[data-theme="system"]',
        'body[data-density="compact"]',
        "prefers-color-scheme:light",
        "max-width:1199px",
        "max-width:700px",
        "prefers-reduced-motion:reduce",
    ):
        assert css in _PAGE
    assert 'src="static/suno-view-modes.js?v=1"' in _PAGE
    assert 'id="sync-dry"' in _PAGE and 'id="sync-real"' in _PAGE


def test_new_static_asset_is_explicitly_allowed_and_packaged() -> None:
    assert "suno-view-modes.js" in _STATIC_FILES
    assert _static_file("suno-view-modes.js") == (WEBUI_STATIC / "suno-view-modes.js").read_bytes()
    assert _static_file("../webui.py") is None
    assert "webui_static/*.js" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    card = (WEBUI_STATIC / "suno-genre-card.js").read_text(encoding="utf-8")
    assert "--sg-theme-surface" in card
    assert "--sg-theme-text" in card
    assert ":host([data-compact])" in card


def test_views_do_not_copy_backend_operations() -> None:
    view_script = (WEBUI_STATIC / "suno-view-modes.js").read_text(encoding="utf-8")
    for forbidden in (
        "fetch(",
        "setConfig(",
        "/api/sync",
        "/api/recommend",
        "innerHTML =",
        ".remove()",
        "replaceChildren(",
    ):
        assert forbidden not in view_script


@pytest.mark.skipif(shutil.which("node") is None, reason="existing Node required")
def test_synthetic_dom_and_preferences_without_a_browser() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/view_modes.mjs"), str(WEBUI_STATIC / "suno-view-modes.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "node identity passed" in result.stdout


def _css_color(selector: str, token: str) -> str:
    """Read a literal six-digit color from a known CSS declaration block."""
    css = _PAGE.split(selector, 1)[1].split("}", 1)[0]
    color = css.split(f"{token}:", 1)[1].split(";", 1)[0].strip()
    assert color.startswith("#") and len(color) in (4, 7), (selector, token, color)
    if len(color) == 4:
        color = "#" + "".join(channel * 2 for channel in color[1:])
    return color


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return sum(x * weight for x, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def _contrast(one: str, two: str) -> float:
    lighter, darker = sorted((_luminance(one), _luminance(two)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize(
    "theme_rule",
    [
        "body{--ui-bg:",
        'body[data-theme="light"]{',
        'body[data-theme="contrast"]{',
        'body[data-theme="system"]{',
    ],
)
def test_theme_alerts_and_details_meet_normal_text_contrast(theme_rule: str) -> None:
    for suffix in ("", "-error", "-cancel"):
        fg = _css_color(theme_rule, f"--ui-op{suffix}-text")
        bg = _css_color(theme_rule, f"--ui-op{suffix}-bg")
        assert _contrast(fg, bg) >= 4.5, (theme_rule, suffix, fg, bg)
    assert (
        _contrast(_css_color(theme_rule, "--ui-text"), _css_color(theme_rule, "--ui-raised")) >= 4.5
    )
    assert (
        _contrast(_css_color(theme_rule, "--ui-warn"), _css_color(theme_rule, "--ui-surface"))
        >= 4.5
    )


def test_home_assistant_palette_requires_explicit_standalone_opt_in() -> None:
    card = (WEBUI_STATIC / "suno-genre-card.js").read_text(encoding="utf-8")
    bridge = card.split("/* The standalone host alone opts in;", 1)[1]
    assert ":host([data-suno-theme]) .pane" in bridge
    assert ":host([data-suno-theme]) .op.failed" in bridge
    assert ":host([data-suno-theme]) .pane-title" in bridge
    assert ":host([data-suno-theme]) .stat .val" in bridge
    assert ":host([data-suno-theme]) .chip.selected" in bridge
    assert 'lib.setAttribute("data-suno-theme", "")' in (
        WEBUI_STATIC / "suno-view-modes.js"
    ).read_text(encoding="utf-8")


def test_studio_narrow_breakpoint_matches_focus_controller() -> None:
    assert "@media(max-width:1199px)" in _PAGE
    view = (WEBUI_STATIC / "suno-view-modes.js").read_text(encoding="utf-8")
    assert '"(max-width: 1199px)"' in view
    assert "panel.hidden = !visible" in view
    assert 'button.setAttribute("aria-pressed"' in view
    assert "previouslySingle && lastFocusedPanel" in view
    assert "aria-current" not in view


@pytest.mark.skipif(shutil.which("node") is None, reason="existing Node required")
def test_keyboard_shortcuts_do_not_steal_external_button_events() -> None:
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tests/js/keyboard_scope.mjs"),
            str(WEBUI_STATIC / "suno-genre-card.js"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "external focus safe" in result.stdout


def test_selected_feature_style_matches_navigation_state() -> None:
    assert '.feature-nav button[aria-pressed="true"]' in _PAGE
    assert '.feature-nav button[aria-current="page"]' not in _PAGE


def test_standalone_shortcut_region_is_focusable_and_recovers_focus() -> None:
    card = (WEBUI_STATIC / "suno-genre-card.js").read_text(encoding="utf-8")
    assert 'id="genre-keyboard-region" role="region" tabindex="0"' in card
    assert "var(--sg-theme-accent);outline-offset:2px" in card
    assert "const focusId = active && active.id ? active.id : null;" in card
    assert "const el = root.getElementById(focusId);" in card
    assert "if (closingMenu) this._focusMenuTrigger(closingMenu);" in card


@pytest.mark.skipif(shutil.which("node") is None, reason="existing Node required")
@pytest.mark.parametrize(
    ("script", "source", "expected"),
    [
        (
            "dashboard_resilience.mjs",
            "src/suno_dl/webui.py",
            "dashboard resilience: offline/malformed status",
        ),
        ("menu_focus.mjs", "src/suno_dl/webui_static/suno-genre-card.js", "menu focus: open"),
    ],
)
def test_new_low_load_synthetic_resilience(script: str, source: str, expected: str) -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js" / script), str(ROOT / source)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="existing Node required")
def test_standalone_touch_targets_preserve_home_assistant_and_narrow_layout() -> None:
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tests/js/touch_targets.mjs"),
            str(WEBUI_STATIC / "suno-genre-card.js"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "HA default hash passed" in result.stdout


@pytest.mark.parametrize(
    "theme_rule",
    [
        "body{--ui-bg:",
        'body[data-theme="light"]{',
        'body[data-theme="contrast"]{',
        'body[data-theme="system"]{',
    ],
)
def test_standalone_selected_sort_meets_text_contrast(theme_rule: str) -> None:
    """Verify the token pair actually used by the standalone selected sort button."""
    card = (WEBUI_STATIC / "suno-genre-card.js").read_text(encoding="utf-8")
    assert ":host([data-suno-theme]) .seg button.on{" in card
    selected = card.split(":host([data-suno-theme]) .seg button.on{", 1)[1].split("}", 1)[0]
    assert "background:var(--sg-theme-raised)" in selected
    assert "color:var(--sg-theme-text)" in selected
    assert "box-shadow:inset 0 -3px var(--sg-theme-accent)" in selected
    # The dashboard binds --sg-theme-* to these --ui-* variables.
    assert "--sg-theme-raised:var(--ui-raised)" in _PAGE
    assert "--sg-theme-text:var(--ui-text)" in _PAGE
    foreground = _css_color(theme_rule, "--ui-text")
    background = _css_color(theme_rule, "--ui-raised")
    assert _contrast(foreground, background) >= 4.5, (theme_rule, foreground, background)


@pytest.mark.skipif(shutil.which("node") is None, reason="existing Node required")
def test_sheet_handle_native_state_and_standalone_render_without_browser() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/sheet_handle.mjs"), str(WEBUI_STATIC / "suno-genre-card.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "drag suppression passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="existing Node required")
def test_sync_request_safety_with_only_synthetic_mocked_posts() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/operation_safety.mjs"), str(ROOT / "src/suno_dl/webui.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=8,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ambiguous refusal" in result.stdout


def test_classic_mobile_navigation_sticky_and_responsive() -> None:
    assert ".feature-nav{position:sticky;top:0;" in _PAGE
    assert 'body[data-view="classic"] [data-panel]:not([data-active="true"]){display:none}' in _PAGE
    assert ".feature-nav{display:grid!important" in _PAGE
    assert 'id="connection-state"' in _PAGE
    assert 'id="sync-uncertain"' in _PAGE
