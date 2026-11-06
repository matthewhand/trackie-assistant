"""Viewport, layout and accessibility tests for the genre card (#95 follow-up).

Runs the real ``suno-genre-card.js`` (served by the real web UI handler) in
headless Chromium over a synthetic library at the standard viewports and asserts
*computed* behaviour: overflow, where the detail pane sits, hit-testing of the
working/error banner, tap-target size, contrast, focus handling, ARIA live
regions and verbatim titles. Probes live in ``fixtures/genre_ui_probes.js``.

Set ``SUNO95_SHOTS_DIR`` to save screenshots (dark theme, one light-host shot).
``SUNO_REQUIRE_BROWSER=1`` turns a missing Chromium into a failure, not a skip.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from .genre_ui_fixture import CATEGORY, HARNESS, N_SONGS, build_library, serve

sync_api = pytest.importorskip("playwright.sync_api")

SHOTS = os.environ.get("SUNO95_SHOTS_DIR")
PROBES = (Path(__file__).parent / "fixtures" / "genre_ui_probes.js").read_text(encoding="utf-8")

# (width, height, touch). Touch viewports use a mobile / coarse-pointer context.
VIEWPORTS = [
    pytest.param((360, 800, True), id="360x800"),
    pytest.param((768, 1024, True), id="768x1024"),
    pytest.param((1280, 720, False), id="1280x720"),
]


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    httpd, url = serve(tmp_path_factory.mktemp("ui95vp"))
    yield url
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(executable_path=os.environ.get("SUNO_BROWSER_EXECUTABLE"))
        except Exception as exc:  # noqa: BLE001 - browser binary not installed
            if os.environ.get("SUNO_REQUIRE_BROWSER"):
                pytest.fail(f"chromium required but unavailable: {exc}")
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _open(browser, base, vp, query="timeout=30"):
    width, height, touch = vp
    ctx = browser.new_context(
        viewport={"width": width, "height": height}, has_touch=touch, is_mobile=touch
    )
    ctx.add_init_script(PROBES)
    pg = ctx.new_page()
    pg.set_default_timeout(6000)
    pg.vp = vp  # type: ignore[attr-defined]
    pg.errors = []  # type: ignore[attr-defined]
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.route(
        "**/harness*",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body=HARNESS.read_text(encoding="utf-8")
        ),
    )
    pg.goto(f"{base}/harness?{query}")
    pg.wait_for_function("window.__ready === true")
    pg.wait_for_selector(f'button[data-cat="{CATEGORY}"]')
    return ctx, pg


@pytest.fixture(params=VIEWPORTS)
def page(request, browser, base):
    ctx, pg = _open(browser, base, request.param)
    yield pg
    ctx.close()


def _narrow(page) -> bool:
    return page.vp[0] <= 700  # the card becomes a bottom sheet at 700px of card width


def _probe(page, name: str, arg=None):
    return page.evaluate("([n, a]) => window.__probe[n](a)", [name, arg])


def _hit(page, selector: str) -> bool:
    return _probe(page, "hit", selector)


def _shot(page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        w, h, _ = page.vp
        page.screenshot(path=str(Path(SHOTS) / f"{w}x{h}-{name}.png"), full_page=False)


def _open_pane(page) -> None:
    page.locator(f'button[data-cat="{CATEGORY}"]').first.click()
    page.wait_for_selector("#genre-pane", state="attached")


def _expand_sheet(page) -> None:
    """A narrow card starts as a peek; Split / Edit live in the expanded sheet."""
    if page.locator("#peek-more").is_visible():
        page.locator("#peek-more").click()
    page.wait_for_selector("#pane-split", state="visible")


def _start_split(page) -> None:
    _open_pane(page)
    _expand_sheet(page)
    page.locator("#pane-split").click()
    page.wait_for_function("window.__pending.length >= 1")


def _speech(text: str) -> dict:
    return {"response": {"response": {"speech": {"plain": {"speech": text}}}}}


def _taxonomy() -> dict:
    subs = [{"name": "Neo Soul", "description": "w"}, {"name": "House", "description": "f"}]
    return _speech(json.dumps({"subcategories": subs}))


def _assign(page, skip_every: int = 0) -> dict:
    text = page.evaluate("window.__pending[0].msg.service_data.text")
    ids = re.findall(r'"id":"([^"]+)"', text)
    out = {i: ("Neo Soul" if n % 2 else "House") for n, i in enumerate(ids)}
    if skip_every:
        keep = [(i, v) for n, (i, v) in enumerate(out.items()) if n % skip_every != skip_every - 1]
        out = dict(keep)
    return _speech(json.dumps({"assignments": out}))


def _resolve(page, payload) -> None:
    page.evaluate("(p) => window.__pending.shift().res(p)", payload)


def _reject(page, err) -> None:
    page.evaluate("(e) => window.__pending.shift().rej(e)", err)


def _pending(page, n=1) -> None:
    page.wait_for_function(f"window.__pending.length >= {n}")


def _to_batch_two(page) -> None:
    _start_split(page)
    _resolve(page, _taxonomy())
    _pending(page)
    _resolve(page, _assign(page))
    _pending(page)


# ───────────────────────── layout ─────────────────────────


def _assert_list_and_detail_pane_layout(page) -> None:
    assert _probe(page, "noOverflow")
    _shot(page, "1-genre-list-dark")
    _open_pane(page)
    page.wait_for_timeout(150)
    geo = _probe(page, "geometry")
    pane, main = geo["pane"], geo["main"]
    if _narrow(page):  # bottom sheet: full card width, docked to the bottom edge
        assert pane["w"] >= geo["vw"] - 60  # card + host padding only
        assert abs(pane["b"] - geo["vh"]) <= 2
        assert pane["t"] > geo["vh"] * 0.25  # the peek leaves the list visible
    else:  # right pane: beside the list, fully on screen
        assert pane["l"] >= main["r"] - 1
        assert pane["r"] <= geo["vw"] + 1
        assert pane["t"] >= 0
        assert pane["b"] <= geo["vh"] + 1
    assert _probe(page, "noOverflow")
    assert page.locator("#genre-pane").get_attribute("aria-label") == f"Genre details: {CATEGORY}"
    _shot(page, "2-detail-pane-dark")
    _expand_sheet(page)
    assert _hit(page, "#pane-split")
    assert _hit(page, "#pane-edit")
    assert _probe(page, "noOverflow")
    _shot(page, "3-detail-actions-dark")
    assert page.errors == []


def test_chip_rows_do_not_squash_or_overlap(page) -> None:
    """Chips keep their natural width (the row scrolls) instead of shrinking over each other."""
    _open_pane(page)
    assert _probe(page, "chipProblems") == []


def test_song_rows_keep_the_title_visible_and_controls_apart(page) -> None:
    """With the pane open the list is narrow: titles must not collapse or sit under Copy / ⋯."""
    _open_pane(page)
    page.wait_for_timeout(100)
    assert _probe(page, "songRowProblems") == []


def test_no_split_button_on_chips_or_song_rows(page) -> None:
    """Design rule: Split lives in the detail pane only, never on every chip / song."""
    _open_pane(page)
    n = page.evaluate(
        "() => Array.from(window.__probe.root().querySelectorAll('button'))"
        ".filter(b => /^split$/i.test(b.textContent.trim())).length"
    )
    assert n == 1
    assert page.locator("#genre-pane #pane-split").count() == 1


def test_light_host_theme_is_readable(browser, base) -> None:
    """A light Home Assistant theme must not produce unreadable text (one light shot)."""
    for vp in [(360, 800, True), (1280, 720, False)]:
        ctx, pg = _open(browser, base, vp, "timeout=30&theme=light")
        _open_pane(pg)
        if vp[2]:
            _expand_sheet(pg)
        low = _probe(pg, "contrast")
        assert low == [], low[:5]
        _shot(pg, "light-host-detail")
        ctx.close()


# ───────────────────────── tap targets and contrast ─────────────────────────


def test_touch_targets_are_at_least_44px(page) -> None:
    """Touch viewports: every control is >= 44x44. Mouse viewports: >= 24x24 (WCAG 2.5.8)."""
    minimum = 43.5 if page.vp[2] else 23.5
    page.locator("#toggle-edit").click()
    page.locator("button[data-menu-more]").first.click()  # the ⋯ menu items count too
    page.wait_for_selector(".menu")
    assert _probe(page, "smallTargets", minimum) == []
    page.locator("#toggle-edit").click()  # closes the menu with an outside click
    _open_pane(page)
    assert _probe(page, "smallTargets", minimum) == []  # list + peek sheet / right pane
    _expand_sheet(page)
    assert _probe(page, "smallTargets", minimum) == []  # expanded sheet


def test_text_contrast_meets_aa(page) -> None:
    _open_pane(page)
    if _narrow(page):
        _expand_sheet(page)
    low = _probe(page, "contrast")
    assert low == [], low[:5]


# ───────────────────────── verbatim titles ─────────────────────────


def test_titles_are_shown_exactly_as_stored(page) -> None:
    _open_pane(page)
    _, clips = build_library()
    stored = {c["id"]: c["title"] for c in clips}
    shown = _probe(page, "titles")
    assert len(shown) == N_SONGS
    for row in shown:
        assert row["text"] == stored[row["id"]]  # never altered, trimmed or re-cased
        assert row["tip"] == stored[row["id"]]  # the full text is always reachable
        assert row["kids"] == 0  # '<b>' in a title is text, not markup
        assert row["ws"] in ("pre", "pre-wrap")  # spaces are not collapsed on screen
    long_rows = [r for r in shown if len(stored[r["id"]]) > 100]
    assert long_rows
    assert all(r["clipped"] for r in long_rows)  # truncated visually only
    assert _probe(page, "noOverflow")


# ───────────────────────── keyboard / focus / names ─────────────────────────


def test_keyboard_open_close_and_focus_kept(page) -> None:
    chip = page.locator(f'button[data-cat="{CATEGORY}"]').first
    chip.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("#genre-pane", state="attached")
    assert _probe(page, "activeInPane")  # focus moves to the detail pane, not lost to <body>
    assert chip.get_attribute("aria-pressed") == "true"
    page.keyboard.press("Tab")  # the next stop is a control inside the pane
    assert _probe(page, "activeInPane")
    page.keyboard.press("Escape")
    page.wait_for_function("window.__probe.root().querySelector('#genre-pane') === null")
    assert _probe(page, "activeAttr", "data-cat") == CATEGORY  # back on the chip that opened it
    # a plain re-render (sort) must not drop keyboard focus either
    page.locator('button[data-sort="az"]').focus()
    page.keyboard.press("Enter")
    page.wait_for_function(
        "window.__probe.root().querySelector('[data-sort=az]')"
        ".getAttribute('aria-pressed') === 'true'"
    )
    assert _probe(page, "activeAttr", "data-sort") == "az"


def test_every_control_is_keyboard_operable_and_named(page) -> None:
    _open_pane(page)
    _expand_sheet(page)
    assert _probe(page, "unnamedOrUnfocusable") == []


# ───────────────────────── split flow: working state ─────────────────────────


def test_working_state_is_visible_and_cancel_is_reachable(page) -> None:
    _open_pane(page)
    page.evaluate("window.scrollTo(0, 400)")  # the user has scrolled the list
    _expand_sheet(page)
    page.locator("#pane-split").click()
    _pending(page)
    # the status line and Cancel are on screen and not hidden under sticky chips / the sheet
    assert _hit(page, "#op-line")
    assert _hit(page, "#op-cancel")
    assert page.locator("#genre-pane #op-line").count() == 1  # shown where it was started
    assert _probe(page, "noOverflow")
    _shot(page, "4-split-working-dark")
    if _narrow(page):  # collapsing the sheet must not hide the running state
        page.locator("#sheet-handle").click()
        assert _hit(page, "#op-line")
        assert _hit(page, "#op-cancel")
    page.locator("#op-cancel").click()
    page.wait_for_selector("#op-banner")


def test_progress_is_announced_in_a_persistent_live_region(page) -> None:
    _open_pane(page)
    _expand_sheet(page)
    _probe(page, "keepLiveNode")
    live = page.locator("#sg-live")
    assert live.get_attribute("role") == "status"
    assert live.get_attribute("aria-live") == "polite"
    page.locator("#pane-split").click()
    _pending(page)
    assert "Splitting" in live.text_content()
    _resolve(page, _taxonomy())
    _pending(page)
    _resolve(page, _assign(page))
    _pending(page)
    assert re.search(rf"analysing 12 of {N_SONGS} tracks", live.text_content())
    bar = page.locator("#op-bar")
    assert bar.get_attribute("role") == "progressbar"
    assert bar.get_attribute("aria-label")
    assert bar.get_attribute("aria-valuenow") == "40"
    assert _probe(page, "sameLiveNode")  # the same node survived every re-render
    assert page.locator(".op.working[aria-live]").count() == 0  # no second competing region
    page.locator("#op-cancel").click()


def test_partial_reply_preview_is_shown_and_reachable(page) -> None:
    _start_split(page)
    _resolve(page, _taxonomy())
    omitted = 0
    for _ in range(3):
        _pending(page)
        asked = page.evaluate("window.__pending[0].msg.service_data.text")
        reply = _assign(page, skip_every=3)
        got = json.loads(reply["response"]["response"]["speech"]["plain"]["speech"])
        omitted += len(re.findall(r'"id":"', asked)) - len(got["assignments"])
        _resolve(page, reply)
    page.wait_for_selector("#split-unclassified", state="attached")
    note = page.locator("#split-unclassified")
    assert note.inner_text().startswith(f"{omitted} song(s) were not classified")
    # the finished preview is brought into view by itself (the sheet opens on a narrow card)
    assert _hit(page, "#split-unclassified")
    page.locator("#apply-split").scroll_into_view_if_needed()
    assert _hit(page, "#apply-split")
    assert _hit(page, "#cancel-split")
    assert "Split preview ready" in page.locator("#sg-live").text_content()
    assert _probe(page, "noOverflow")
    _shot(page, "5-split-partial-preview-dark")
    page.locator("#cancel-split").click()
    assert page.evaluate("window.__saves.length") == 0


# ───────────────────────── error banners ─────────────────────────

FAILURES = [
    ("timeout", "AI service timeout"),
    ("unreachable", "AI service unreachable"),
    ("invalid", "AI service sent an invalid reply"),
    ("empty", "AI service sent an empty reply"),
    ("perm", "File or permission problem"),
    ("unexpected", "Unexpected error"),
]


def _trigger(page, kind: str) -> None:
    if kind == "unreachable":
        _reject(
            page, {"code": "unknown_error", "message": "InternalServerError: Connection error."}
        )
    elif kind == "invalid":
        _resolve(page, _speech("Sure! Here are some thoughts, no JSON though."))
    elif kind == "empty":
        _resolve(page, _speech("   "))
    elif kind == "perm":
        _reject(page, {"code": "unknown_error", "message": "EACCES: permission denied"})
    elif kind == "unexpected":
        _reject(page, {"code": "boom", "message": "kaboom"})
    # timeout: the parked request hits the card's own limit


@pytest.mark.parametrize(("kind", "cause"), FAILURES, ids=[f[0] for f in FAILURES])
def test_error_banner_is_visible_actionable_and_announced_once(
    browser, base, page, kind, cause
) -> None:
    if kind == "timeout":  # reopen with a short agent limit
        page.context.close()
        _, page = _open(browser, base, page.vp, "timeout=0.4")
    _to_batch_two(page)
    _trigger(page, kind)
    page.wait_for_selector("#op-banner")
    banner = page.locator("#op-banner")
    assert cause in banner.inner_text()
    assert banner.get_attribute("role") != "alert"  # a re-rendered alert would be re-read
    for sel in ("#op-retry", "#op-copy", "#op-dismiss"):
        assert _hit(page, sel), sel
        if page.vp[2]:
            box = page.locator(sel).bounding_box()
            assert box["height"] >= 43.5 and box["width"] >= 43.5, (sel, box)
    assert _probe(page, "noOverflow")
    # the failure is spoken once, by the persistent assertive region
    alert = page.locator("#sg-alert")
    assert alert.get_attribute("role") == "alert"
    assert cause in alert.text_content()
    _probe(page, "watchAlert")
    for sort in ("az", "newest"):  # unrelated re-renders (the sheet may cover the list)
        _probe(page, "click", f"[data-sort={sort}]")
    assert page.evaluate("window.__alertMuts") == 0
    if page.vp[0] in (360, 1280):
        _shot(page, f"6-error-{kind}-dark")
    page.locator("#op-dismiss").click()
    assert page.locator("#op-banner").count() == 0
    assert page.locator("#sg-alert").text_content() == ""


def test_copy_details_button_works_at_every_viewport(page) -> None:
    _to_batch_two(page)
    _reject(page, {"code": "unknown_error", "message": "Failed to fetch"})
    page.wait_for_selector("#op-banner")
    page.locator("#op-copy").click()
    page.wait_for_function("window.__copied !== null")
    assert "Request id:" in page.evaluate("window.__copied")


def test_cancel_banner_dismiss_and_retry_flow(page) -> None:
    _start_split(page)
    page.locator("#op-cancel").click()
    page.wait_for_selector("#op-banner")
    assert page.locator("#op-banner").get_attribute("data-op-code") == "cancelled"
    assert "Cancelled" in page.locator("#sg-live").text_content()
    assert _hit(page, "#op-dismiss")
    page.locator("#op-retry").click()  # starts again, locked
    _pending(page)
    assert page.locator("#pane-split").is_disabled()
    page.locator("#op-cancel").click()
    page.locator("#op-dismiss").click()
    assert page.locator(".op").count() == 0


# ───────────────────────── top-level actions ─────────────────────────


def test_banner_outside_a_pane_is_not_hidden_under_sticky_chips(page) -> None:
    """Reclassify has no pane: its working banner sits at the top and must stay visible."""
    page.on("dialog", lambda d: d.accept())
    page.evaluate("window.scrollTo(0, 300)")
    page.locator("#reclassify").scroll_into_view_if_needed()
    page.locator("#reclassify").click()  # the harness rest_command resolves; the card waits 2 s
    page.wait_for_selector(".op.working")
    page.evaluate("window.scrollTo(0, 500)")
    page.wait_for_timeout(100)
    assert page.locator(".op.working").count() == 1  # still working, not yet finished
    assert _hit(page, "#op-line")
    _shot(page, "7-reclassify-working-dark")


def test_list_and_detail_pane_layout(page) -> None:
    _assert_list_and_detail_pane_layout(page)


@pytest.mark.parametrize("viewport", [(390, 844, True), (1920, 1080, False)])
def test_additional_viewport_layout(browser, base, viewport) -> None:
    context, page = _open(browser, base, viewport)
    try:
        _assert_list_and_detail_pane_layout(page)
    finally:
        context.close()
