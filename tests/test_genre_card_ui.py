"""Browser tests for the genre card's working / error states (#95).

Loads the real ``suno-genre-card.js`` served by the real web UI handler into a
headless Chromium page with a fake Home Assistant whose conversation agent the
test controls. Skipped when Playwright or its Chromium build is unavailable
(``SUNO_REQUIRE_BROWSER=1`` turns that skip into a failure; CI sets it).

Set ``SUNO95_SHOTS_DIR`` to also save screenshots (desktop 1280x800 and phone
390x844) of the working and error states.
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from suno_dl.webui import RunManager, _make_handler

sync_api = pytest.importorskip("playwright.sync_api")

HARNESS = Path(__file__).parent / "fixtures" / "genre_card_harness.html"
CATEGORY = "Neo Soul & House"
N_SONGS = 30  # 3 assignment batches of 12/12/6
SHOTS = os.environ.get("SUNO95_SHOTS_DIR")


def _write_library(root: Path) -> Path:
    genres = root / "genres"
    genres.mkdir()
    clips = [
        {
            "id": f"aaaaaaaa-{i:04d}",
            "title": f"Song {i}",
            "style": "neo soul, warm keys" if i % 2 else "deep house, 4/4 groove",
            "model": "v4",
        }
        for i in range(N_SONGS)
    ] + [
        {
            "id": f"bbbbbbbb-{i:04d}",
            "title": f"Industrial {i}",
            "style": "industrial",
            "model": "v4",
        }
        for i in range(6)
    ]
    assignments = {c["id"]: CATEGORY for c in clips[:N_SONGS]}
    assignments.update({c["id"]: "Cyber Industrial" for c in clips[N_SONGS:]})
    (genres / "genres.json").write_text(
        json.dumps(
            {
                "categories": [
                    {"name": CATEGORY, "count": N_SONGS},
                    {"name": "Cyber Industrial", "count": 6},
                ],
                "assignments": assignments,
                "pinned": [],
            }
        ),
        encoding="utf-8",
    )
    (genres / "clips.json").write_text(json.dumps(clips), encoding="utf-8")
    return genres


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    root = tmp_path_factory.mktemp("ui95")
    genres = _write_library(root)
    (root / ".env").write_text("", encoding="utf-8")
    manager = RunManager(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(root, manager, genres_dir=genres))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browser binary not installed
            if os.environ.get("SUNO_REQUIRE_BROWSER"):  # CI: never skip silently
                pytest.fail(f"chromium required but unavailable: {exc}")
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture(params=[(1280, 800, "desktop"), (390, 844, "phone")], ids=["desktop", "phone"])
def page(request, browser, base):
    width, height, label = request.param
    ctx = browser.new_context(viewport={"width": width, "height": height})
    pg = ctx.new_page()
    pg.label = label  # type: ignore[attr-defined]
    pg.route(
        "**/harness*",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body=HARNESS.read_text(encoding="utf-8")
        ),
    )
    pg.goto(base + "/harness?timeout=30")
    pg.wait_for_function("window.__ready === true")
    pg.wait_for_selector(f'button[data-cat="{CATEGORY}"]')
    yield pg
    ctx.close()


def _shot(page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}-{page.label}.png"), full_page=False)


def _open_pane(page) -> None:
    page.locator(f'button[data-cat="{CATEGORY}"]').first.dispatch_event("click")
    page.wait_for_selector("#pane-split", state="attached")


def _click(page, selector: str) -> None:
    page.locator(selector).first.dispatch_event("click")


def _pending(page, n: int = 1) -> None:
    page.wait_for_function(f"window.__pending.length >= {n}")


def _speech(text: str) -> dict:
    return {"response": {"response": {"speech": {"plain": {"speech": text}}}}}


def _resolve(page, payload: dict) -> None:
    page.evaluate("(p) => window.__pending.shift().res(p)", payload)


def _reject(page, error: dict) -> None:
    page.evaluate("(e) => window.__pending.shift().rej(e)", error)


def _taxonomy() -> dict:
    return _speech(
        json.dumps(
            {
                "subcategories": [
                    {"name": "Neo Soul", "description": "warm"},
                    {"name": "House", "description": "four on the floor"},
                ]
            }
        )
    )


def _assignment_reply(page) -> dict:
    text = page.evaluate("window.__pending[0].msg.service_data.text")
    ids = re.findall(r'"id":"([^"]+)"', text)
    return _speech(
        json.dumps(
            {"assignments": {i: ("Neo Soul" if n % 2 else "House") for n, i in enumerate(ids)}}
        )
    )


def _no_horizontal_overflow(page) -> bool:
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


LOCKED = ["#pane-split", "#pane-edit", "#pane-clear", "#reclassify", "#undo", "#send"]


def _assert_locked(page, locked: bool) -> None:
    for sel in LOCKED:
        el = page.locator(sel)
        if el.count() == 0:
            continue
        assert el.first.is_disabled() == locked, (sel, locked)


def test_split_working_state_progress_and_lock(page) -> None:
    _open_pane(page)
    _assert_locked(page, False)
    _click(page, "#pane-split")
    _pending(page)
    line = page.locator("#op-line")
    assert line.inner_text() == f'Splitting "{CATEGORY}"… asking the AI for subcategories'
    assert page.locator(".op .spin").count() == 1
    assert page.locator("#op-cancel").is_enabled()
    _assert_locked(page, True)  # a second split cannot be started
    _click(page, "#pane-split")  # ignored while running
    assert page.evaluate("window.__pending.length") == 1
    _shot(page, "working-1-subcategories")

    _resolve(page, _taxonomy())
    _pending(page)
    assert (
        line.inner_text()
        == f'Splitting "{CATEGORY}" into Neo Soul + House… analysing 0 of {N_SONGS} tracks'
    )
    _resolve(page, _assignment_reply(page))
    _pending(page)
    assert line.inner_text().endswith(f"analysing 12 of {N_SONGS} tracks")
    bar = page.locator("#op-bar")
    assert bar.get_attribute("aria-valuenow") == "40"
    assert _no_horizontal_overflow(page)
    _shot(page, "working-2-progress")

    _resolve(page, _assignment_reply(page))
    _pending(page)
    _resolve(page, _assignment_reply(page))
    page.wait_for_selector("text=Split preview", state="attached")
    assert page.locator(".op").count() == 0
    _assert_locked(page, False)


def _fail_at_batch_two(page) -> None:
    _open_pane(page)
    _click(page, "#pane-split")
    _pending(page)
    _resolve(page, _taxonomy())
    _pending(page)
    _resolve(page, _assignment_reply(page))
    _pending(page)


@pytest.mark.parametrize(
    ("failure", "cause", "code"),
    [
        ("timeout", "AI service timeout", "ai_timeout"),
        ("unreachable", "AI service unreachable", "ai_unreachable"),
        ("invalid", "AI service sent an invalid reply", "ai_invalid_reply"),
        ("empty", "AI service sent an empty reply", "ai_empty_reply"),
        ("agent_error", "AI service unreachable", "ai_unreachable"),
        ("perm", "File or permission problem", "file_permission"),
        ("unexpected", "Unexpected error", "unexpected"),
    ],
)
def test_split_failure_banner_names_cause_and_what_completed(page, failure, cause, code) -> None:
    if failure == "timeout":
        page.goto(page.url.replace("timeout=30", "timeout=0.4"))
        page.wait_for_function("window.__ready === true")
        page.wait_for_selector(f'button[data-cat="{CATEGORY}"]')
    _fail_at_batch_two(page)
    if failure == "unreachable":
        _reject(
            page, {"code": "unknown_error", "message": "InternalServerError: Connection error."}
        )
    elif failure == "invalid":
        _resolve(page, _speech("Sure! Here are some thoughts, no JSON though."))
    elif failure == "empty":
        _resolve(page, _speech("   "))
    elif failure == "agent_error":
        _resolve(
            page,
            {
                "response": {
                    "response": {
                        "response_type": "error",
                        "data": {"code": "unknown"},
                        "speech": {"plain": {"speech": "Sorry, I had a problem talking to the AI"}},
                    }
                }
            },
        )
    elif failure == "perm":
        _reject(page, {"code": "unknown_error", "message": "EACCES: permission denied"})
    elif failure == "unexpected":
        _reject(page, {"code": "boom", "message": "kaboom"})
    # timeout: leave the request parked until the 0.4 s limit fires
    banner = page.locator("#op-banner")
    banner.wait_for()
    text = banner.inner_text()
    assert cause in text
    assert banner.get_attribute("data-op-code") == code
    assert f"12 of {N_SONGS} tracks analysed, {N_SONGS - 12} not analysed" in text
    assert "Nothing was changed" in text
    assert re.search(r"Request\s+[0-9a-f]{8}", text)
    assert page.locator("#op-retry").is_visible()
    assert page.locator("#op-copy").is_visible()
    assert page.locator(".op .spin").count() == 0  # spinner replaced by the banner
    _assert_locked(page, False)
    assert _no_horizontal_overflow(page)
    if failure == "timeout":
        _shot(page, "error-timeout")
    if failure == "agent_error":
        _shot(page, "error-ai-unreachable")


def test_copy_details_is_redacted_and_complete(page) -> None:
    _fail_at_batch_two(page)
    _reject(
        page,
        {
            "code": "unknown_error",
            "message": "Connection error. Authorization: Bearer abcdefghijklmnop1234",
        },
    )
    page.wait_for_selector("#op-banner")
    _click(page, "#op-copy")
    page.wait_for_function("window.__copied !== null")
    copied = page.evaluate("window.__copied")
    assert "abcdefghijklmnop1234" not in copied
    for needle in ("AI service unreachable", "ai_unreachable", "Request id:", "Time:", "Splitting"):
        assert needle in copied


def test_retry_restarts_and_relocks(page) -> None:
    _fail_at_batch_two(page)
    _reject(page, {"code": "unknown_error", "message": "Failed to fetch"})
    page.wait_for_selector("#op-banner")
    _click(page, "#op-retry")
    _pending(page)
    assert page.locator("#op-banner").count() == 0
    assert page.locator("#op-line").inner_text().startswith(f'Splitting "{CATEGORY}"')
    _assert_locked(page, True)


def test_invalid_reply_retry_uses_json_only_prompt(page) -> None:
    _open_pane(page)
    _click(page, "#pane-split")
    _pending(page)
    _resolve(page, _speech("I think maybe three groups?"))
    page.wait_for_selector("#op-banner")
    _click(page, "#op-retry")
    _pending(page)
    sent = page.evaluate("window.__pending[0].msg.service_data.text")
    assert "STRICT FORMAT RULES" in sent


def test_cancel_stops_waiting_and_unlocks(page) -> None:
    _open_pane(page)
    _click(page, "#pane-split")
    _pending(page)
    _click(page, "#op-cancel")
    banner = page.locator("#op-banner")
    banner.wait_for()
    assert "Cancelled" in banner.inner_text()
    assert banner.get_attribute("data-op-code") == "cancelled"
    assert page.locator("#op-copy").count() == 0
    _assert_locked(page, False)
    _shot(page, "cancelled")
    _click(page, "#op-dismiss")
    assert page.locator(".op").count() == 0


# --- atomicity: a failed / cancelled / partial split must not reclassify tracks ---


def _genres_state(page) -> str:
    return page.evaluate("JSON.stringify(document.getElementById('lib')._genres)")


def _assert_nothing_changed(page, before: str) -> None:
    assert _genres_state(page) == before  # taxonomy and assignments untouched
    assert page.evaluate("window.__saves.length") == 0  # nothing was written back
    assert page.locator("text=Split preview").count() == 0  # nothing left to apply
    assert page.locator("#apply-split").count() == 0


@pytest.mark.parametrize("how", ["error", "invalid", "cancel"])
def test_failed_or_cancelled_split_leaves_assignments_unchanged(page, how) -> None:
    _open_pane(page)
    before = _genres_state(page)
    assert json.loads(before)["assignments"]  # the library really is loaded
    _fail_at_batch_two(page)  # taxonomy ok, batch 1 assigned, batch 2 in flight
    if how == "error":
        _reject(page, {"code": "unknown_error", "message": "Failed to fetch"})
    elif how == "invalid":
        _resolve(page, _speech("not json at all"))
    else:
        _click(page, "#op-cancel")
    page.wait_for_selector("#op-banner")
    _assert_nothing_changed(page, before)


# --- #133: every top-level chip must be reachable on a narrow phone ---

MANY_GENRES = [
    "Cinematic Soul",
    "Neo Soul & House",
    "Cyber Industrial",
    "Ambient Drift",
    "Broken Beat",
    "Dream Pop",
    "Lo-fi Study",
    "Deep House",
    "Synthwave Nights",
    "Orchestral Drama",
    "Trap Soul",
    "Afro House",
    "Jazz Fusion",
    "Downtempo",
    "Garage & 2-Step",
    "Psychedelic Funk",
    "Cinematic Trailer Music",
    "Chiptune",
    "Gospel Choir",
    "Dub Techno",
]


def _write_wide_library(root: Path) -> Path:
    genres = root / "genres"
    genres.mkdir()
    clips = [
        {"id": f"cccccccc-{i:04d}", "title": f"Track {i}", "style": "neo soul", "model": "v4"}
        for i in range(len(MANY_GENRES))
    ]
    (genres / "genres.json").write_text(
        json.dumps(
            {
                "categories": [{"name": name, "count": 1} for name in MANY_GENRES],
                "assignments": {c["id"]: MANY_GENRES[i] for i, c in enumerate(clips)},
                "pinned": [],
            }
        ),
        encoding="utf-8",
    )
    (genres / "clips.json").write_text(json.dumps(clips), encoding="utf-8")
    return genres


@pytest.fixture(scope="module")
def wide_base(tmp_path_factory):
    root = tmp_path_factory.mktemp("ui133")
    genres = _write_wide_library(root)
    (root / ".env").write_text("", encoding="utf-8")
    manager = RunManager(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(root, manager, genres_dir=genres))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(
    params=[(320, 537), (360, 800), (390, 844)],
    ids=["320x537", "360x800", "390x844"],
)
def narrow(request, browser, wide_base):
    width, height = request.param
    ctx = browser.new_context(viewport={"width": width, "height": height})
    pg = ctx.new_page()
    pg.route(
        "**/harness*",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body=HARNESS.read_text(encoding="utf-8")
        ),
    )
    pg.goto(wide_base + "/harness?timeout=30")
    pg.wait_for_function("window.__ready === true")
    pg.wait_for_selector('button[data-cat="All"]')
    yield pg
    ctx.close()


def _chip_metrics(page) -> dict:
    return page.evaluate(
        """() => {
            const root = document.querySelector('suno-genre-card').shadowRoot;
            const chips = root.querySelector('.chips.top');
            const stick = root.querySelector('.stick');
            return {
                scrollWidth: chips.scrollWidth, clientWidth: chips.clientWidth,
                scrollHeight: chips.scrollHeight, clientHeight: chips.clientHeight,
                stickHeight: stick.getBoundingClientRect().height,
                pageWidth: document.documentElement.scrollWidth,
                viewportWidth: window.innerWidth, viewportHeight: window.innerHeight,
            };
        }"""
    )


def test_narrow_screen_reaches_every_top_level_chip_without_sideways_scroll(narrow) -> None:
    page = narrow
    chips = page.locator(".chips.top .chip")
    assert chips.count() == len(MANY_GENRES) + 1

    collapsed = _chip_metrics(page)
    # Nothing on the page or inside the chip row may need sideways swiping.
    assert collapsed["pageWidth"] <= collapsed["viewportWidth"] + 1
    assert collapsed["scrollWidth"] <= collapsed["clientWidth"] + 1
    # Some chips start collapsed away; the sticky header stays compact.
    assert collapsed["scrollHeight"] > collapsed["clientHeight"] + 1
    assert collapsed["stickHeight"] <= 0.4 * collapsed["viewportHeight"]
    more = page.locator("#chips-more")
    assert more.is_visible()
    assert more.get_attribute("aria-expanded") == "false"
    assert more.get_attribute("aria-label") == "Show all genres"
    assert more.inner_text() == "More ▾"
    # Even collapsed, no chip may spill out of the card horizontally.
    for i in range(chips.count()):
        box = chips.nth(i).bounding_box()
        assert box is not None
        assert box["x"] >= -0.5
        assert box["x"] + box["width"] <= collapsed["viewportWidth"] + 0.5
    if SHOTS:
        width = page.viewport_size["width"]
        page.screenshot(path=str(Path(SHOTS) / f"chips-collapsed-{width}.png"))

    more.click()
    page.wait_for_function("window.__ready === true")
    assert more.get_attribute("aria-expanded") == "true"
    assert more.get_attribute("aria-label") == "Show fewer genres"
    assert more.inner_text() == "Less ▴"
    expanded = _chip_metrics(page)
    assert expanded["scrollHeight"] <= expanded["clientHeight"] + 1  # every chip laid out
    assert expanded["scrollWidth"] <= expanded["clientWidth"] + 1
    assert expanded["pageWidth"] <= expanded["viewportWidth"] + 1
    for i in range(chips.count()):
        box = chips.nth(i).bounding_box()
        assert box is not None
        assert box["x"] >= -0.5
        assert box["x"] + box["width"] <= expanded["viewportWidth"] + 0.5
    if SHOTS:
        width = page.viewport_size["width"]
        page.screenshot(path=str(Path(SHOTS) / f"chips-expanded-{width}.png"))

    # The furthest chip is genuinely usable, not just painted.
    last = MANY_GENRES[-1]
    chip = page.locator(f'.chips.top .chip[data-cat="{last}"]')
    chip.scroll_into_view_if_needed()
    chip.click()
    page.wait_for_selector("#genre-pane")
    assert (
        page.evaluate(
            "document.querySelector('suno-genre-card').shadowRoot.querySelector('.pane-title').textContent"
        )
        == last
    )
    after = _chip_metrics(page)
    assert after["pageWidth"] <= after["viewportWidth"] + 1


def test_partial_ai_reply_does_not_guess_omitted_tracks(page) -> None:
    _open_pane(page)
    before = json.loads(_genres_state(page))
    _click(page, "#pane-split")
    _pending(page)
    _resolve(page, _taxonomy())
    omitted: list[str] = []
    for _ in range(3):  # three batches; the AI skips every third song in each
        _pending(page)
        reply = _assignment_reply(page)
        data = json.loads(reply["response"]["response"]["speech"]["plain"]["speech"])
        for n, tid in enumerate(list(data["assignments"])):
            if n % 3 == 2:
                omitted.append(tid)
                del data["assignments"][tid]
        _resolve(page, _speech(json.dumps(data)))
    page.wait_for_selector("text=Split preview", state="attached")
    note = page.locator("#split-unclassified").inner_text()
    assert note.startswith(f"{len(omitted)} song(s) were not classified")
    assert page.evaluate("window.__saves.length") == 0  # preview only, nothing saved yet
    _click(page, "#apply-split")
    page.wait_for_function("window.__saves.length === 1")
    saved = json.loads(page.evaluate("window.__saves[0].data.genres_json"))
    assert omitted
    for tid in omitted:  # NOT silently moved into the last subcategory
        assert saved["assignments"][tid] == before["assignments"][tid] == CATEGORY
    classified = [i for i in before["assignments"] if i.startswith("aaaaaaaa") and i not in omitted]
    assert all(saved["assignments"][i] in ("Neo Soul", "House") for i in classified)
    for i, cat in before["assignments"].items():  # unrelated tracks untouched
        if i.startswith("bbbbbbbb"):
            assert saved["assignments"][i] == cat
