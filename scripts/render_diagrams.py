"""Render diagram HTML files to PNG for visual verification.

Usage: python scripts/render_diagrams.py [glob-dir]
Writes <name>.png next to each .html (in docs/diagrams/renders/).
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/diagrams").resolve()
    htmls = sorted(root.glob("*.html"))
    if not htmls:
        print(f"no .html files under {root}")
        return 1
    outdir = root / "renders"
    outdir.mkdir(exist_ok=True)
    failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        for html in htmls:
            page.goto(html.as_uri())
            page.wait_for_timeout(700)  # let webfonts settle
            svg = page.query_selector("svg")
            if svg is None:
                print(f"FAIL {html.name}: no <svg>")
                failures += 1
                continue
            png = outdir / (html.stem + ".png")
            svg.screenshot(path=str(png))
            box = svg.bounding_box()
            print(f"OK   {html.name} -> {png.name}  {box['width']:.0f}x{box['height']:.0f}")
        browser.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
