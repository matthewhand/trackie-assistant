"""Opt-in synthetic Firefox dashboard sweep; zero server, real downloads or outbound calls.

Run from repository root with PYTHONPATH=src python tests/synthetic_browser_check.py.
Requires existing compatible Playwright Firefox; never installs a browser.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from suno_dl.webui import _PAGE

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/suno_dl/webui_static"
FIXTURE = {
    "/api/status": {
        "version": "fixture",
        "archive": "synthetic://no-disk",
        "engine": "mock",
        "blocked": {"count": 0},
        "watch": [],
        "profiles": [],
    },
    "/api/runs": {"runs": []},
    "/api/library": {"genres": {}, "assignments": {}, "pinned": [], "tracks": []},
    "/api/tracks": {"tracks": [], "total": 0},
    "/api/lyrics": {"lyrics": {}},
    "/api/files": {"files": []},
}


# Block every request unless the URL is our own fictional host and its path
# is one of the explicit fixture resources. No live endpoints are reachable.
def run() -> None:
    seen: list[str] = []
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.firefox.launch(headless=True, timeout=15000)
        try:
            ctx = browser.new_context(
                viewport={"width": 390, "height": 850},
                reduced_motion="reduce",
                service_workers="block",
            )

            def intercept(route):
                request = route.request
                parsed = urlparse(request.url)
                if parsed.hostname != "suno.invalid" or request.method != "GET":
                    seen.append("BLOCKED " + request.method + " " + request.url)
                    route.abort()
                    return
                path = parsed.path
                if path == "/":
                    route.fulfill(status=200, content_type="text/html", body=_PAGE)
                elif path.startswith("/static/") and path[len("/static/") :] in {
                    "suno-genre-card.js",
                    "suno-view-modes.js",
                    "suno-genre-core.js",
                }:
                    route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=(STATIC / path.split("/")[-1]).read_text(),
                    )
                elif path == "/static/trackie-assistant-logo.webp":
                    route.fulfill(
                        status=200,
                        content_type="image/webp",
                        body=(STATIC / "trackie-assistant-logo.webp").read_bytes(),
                    )
                elif path == "/static/trackie-dacks.svg":
                    route.fulfill(
                        status=200,
                        content_type="image/svg+xml",
                        body=(STATIC / "trackie-dacks.svg").read_text(),
                    )
                elif path in FIXTURE:
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(FIXTURE[path]),
                    )
                else:
                    seen.append("BLOCKED " + request.url)
                    route.abort()

            ctx.route("**/*", intercept)
            page = ctx.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)[:300]))
            page.goto("http://suno.invalid/", wait_until="domcontentloaded", timeout=16000)
            page.wait_for_function(
                "() => { const img = document.querySelector('.brand-mark img');"
                " return !!img && img.complete && img.naturalWidth > 0; }",
                timeout=10000,
            )
            # Crop the same approved hero art in CSS: ensure the mascot gets
            # substantially more space than the old 44x33 thumbnail.
            framing = page.evaluate("""() => {
              const m=document.querySelector('.brand-mark').getBoundingClientRect();
              const i=document.querySelector('.brand-mark img').getBoundingClientRect();
              return {markWidth:m.width, markHeight:m.height, imageWidth:i.width,
                sourceWidth:document.querySelector('.brand-mark img').naturalWidth};
            }""")
            if (
                framing["markWidth"] < 60
                or framing["markHeight"] < 100
                or framing["imageWidth"] <= framing["markWidth"] * 2
                or framing["sourceWidth"] != 420
            ):
                errors.append(f"mascot framing too small or artwork replaced: {framing}")
            hero_motion = page.locator(".brand-mark img").evaluate(
                "(node) => getComputedStyle(node).animationName"
            )
            if hero_motion != "none":
                errors.append("approved banner ignores reduced-motion")
            page.emulate_media(reduced_motion="no-preference")
            hero_active = page.locator(".brand-mark img").evaluate(
                "(node) => getComputedStyle(node).animationName"
            )
            if hero_active != "brand-sway":
                errors.append("approved banner animation missing")
            svg_page = ctx.new_page()
            svg_page.goto(
                "http://suno.invalid/static/trackie-dacks.svg",
                wait_until="domcontentloaded",
                timeout=10000,
            )
            svg_motion = svg_page.locator("#trackie-body").evaluate(
                "(node) => getComputedStyle(node).animationName"
            )
            if svg_motion != "none":
                errors.append("mascot ignores reduced-motion setting")
            svg_page.emulate_media(reduced_motion="no-preference")
            svg_active = svg_page.locator("#trackie-body").evaluate(
                "(node) => getComputedStyle(node).animationName"
            )
            if svg_active != "sway":
                errors.append("mascot animation missing when motion is allowed")
            svg_page.close()
            page.locator("#lib").wait_for(state="attached", timeout=10000)
            page.wait_for_function(
                "document.querySelector('#lib').hasAttribute('data-suno-theme')",
                timeout=12000,
            )
            page.wait_for_function(
                "document.querySelector('#lib').shadowRoot !== null",
                timeout=12000,
            )
            for width in (320, 390, 700, 768, 1024, 1199, 1200, 1920):
                page.set_viewport_size({"width": width, "height": 860})
                for view in ("classic", "studio", "minimal"):
                    page.locator("#view-mode").select_option(view)
                    for theme in ("system", "dark", "light", "contrast"):
                        page.locator("#appearance-mode").select_option(theme)
                        data = page.evaluate("""() => ({
                          scroll: document.documentElement.scrollWidth,
                          width: document.documentElement.clientWidth,
                          visible: [...document.querySelectorAll('[data-panel]')]
                            .filter(p => !p.hidden && getComputedStyle(p).display !== 'none')
                            .length,
                          mode: document.body.dataset.view,
                          theme: document.body.dataset.theme,
                          connection: document.querySelector('#connection-state').dataset.state,
                        })""")
                        if data["scroll"] > width + 2:
                            errors.append(f"horizontal overflow at {width} {view} {theme}: {data}")
                        expected = (
                            1
                            if view == "minimal"
                            or (view == "studio" and width <= 1199)
                            or (view == "classic" and width <= 700)
                            else 4
                        )
                        if data["visible"] != expected:
                            errors.append(f"wrong panel count at {width} {view}: {data}")
                        if data["mode"] != view or data["theme"] != theme:
                            errors.append(f"mode/theme mismatch: {data}")
                if width <= 700:
                    page.locator("#view-mode").select_option("classic")
                    for label in ("Library", "Sync", "Discover", "Activity"):
                        control = page.locator("#feature-nav button", has_text=label)
                        bounds = control.bounding_box()
                        if not bounds or bounds["height"] < 44:
                            errors.append(f"navigation target smaller than 44px: {width} {label}")
                        control.click()
                        selected = control.get_attribute("aria-pressed")
                        if selected != "true":
                            errors.append(f"mobile navigation not activated: {width} {label}")
                    sticky = page.locator("#feature-nav").evaluate(
                        "(element) => getComputedStyle(element).position"
                    )
                    if sticky != "sticky":
                        errors.append(f"mobile navigation not sticky at {width}")
            page.locator("#view-mode").select_option("studio")
            page.set_viewport_size({"width": 1920, "height": 900})
            page.locator("#cands").focus()
            page.set_viewport_size({"width": 390, "height": 850})
            if page.locator("#panel-recommend").is_hidden():
                errors.append("Studio narrow resize hides the currently focused panel")
            if page.locator("#connection-state").inner_text() != "Online":
                errors.append("connection indicator failed to surface online state")
            # Never trigger Sync or Recommend; those routes are forbidden regardless.
            print("browser matrix: 8 widths x 3 modes x 4 themes = 96 combinations")
            print("mobile controls: four tabs at 320, 390, 700px; Studio resize")
            print("interception blocks:", len(seen), "javascript errors:", len(errors))
            for entry in (seen + errors)[:12]:
                print("ISSUE", entry)
            assert not seen and not errors, "synthetic browser validation failed"
            ctx.close()
        finally:
            browser.close()


if __name__ == "__main__":
    os.environ["MOZ_DISABLE_TELEMETRY"] = "1"
    run()
