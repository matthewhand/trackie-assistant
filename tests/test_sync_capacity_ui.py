"""Real browser rendering of capacity details; no network or library writes."""

import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from suno_dl.sync_capacity import capacity_snapshot
from suno_dl.webui import _PAGE

sync_api = pytest.importorskip("playwright.sync_api")


def test_queue_capacity_in_standalone_and_ha_views(tmp_path):
    data = capacity_snapshot(tmp_path, [NS(duration_sec=240), NS()])
    data["storage"].update(free_bytes=1, total_bytes=1024**4, destination="<img src=x>")
    script = _PAGE.split("function capacityBytes(", 1)[1].split("async function refresh(", 1)[0]
    bridge = (
        (Path(__file__).parents[1] / "deploy/haos-dashboard/trackie-ha-bridge.js")
        .read_text()
        .replace("export function", "function", 1)
    )
    with sync_api.sync_playwright() as p:
        options = {"headless": True}
        chrome = shutil.which("google-chrome")
        if chrome:
            options["executable_path"] = chrome
        browser = p.chromium.launch(**options)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.route("**/*", lambda route: route.abort())
            page.set_content('<div id="sync-capacity"></div>')
            page.add_script_tag(content="function capacityBytes(" + script)
            page.evaluate(
                "d => renderCapacity(d.storage, {kind:'sync',state:'running',capacity:d})", data
            )
            panel = page.locator("#sync-capacity")
            assert "2 track(s)" in panel.inner_text()
            assert "assuming 4 minutes" in panel.inner_text()
            assert "Low space" in panel.inner_text()
            assert "network transfer size is unknown" in panel.inner_text()
            assert page.locator("img").count() == 0
            page.evaluate("renderCapacity(null,null)")
            assert "free space unavailable" in panel.inner_text()

            page.add_script_tag(
                content=bridge
                + """
class FixtureCard extends HTMLElement {
  constructor(){super();this.attachShadow({mode:'open'});
    this.shadowRoot.innerHTML='<div class="hd"></div>';this._config={};}
  get hass(){return this._hass;}
  set hass(value){this._hass=value;}
  _render(){}
  _togglePlay(){}
  disconnectedCallback(){}
}
installHaLibraryBridge(FixtureCard);
customElements.define('capacity-fixture',FixtureCard);
document.body.append(document.createElement('capacity-fixture'));
"""
            )
            attrs = {
                "capacity": data,
                "missing": 2,
                "remaining": 2,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            page.evaluate(
                "a=>document.querySelector('capacity-fixture').hass="
                "{states:{'sensor.trackie_sync_status':{state:'waiting',attributes:a}}}",
                attrs,
            )
            ha = page.locator("capacity-fixture").locator("#ct107-sync-status")
            assert "Queued: 2 tracks" in ha.inner_text()
            assert "Estimated saved size" in ha.inner_text()
            assert "Low space" in ha.inner_text()
            for width in (390, 1280):
                page.set_viewport_size({"width": width, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.evaluate(
                "document.querySelector('capacity-fixture').hass="
                "{states:{'sensor.trackie_sync_status':{state:'complete',attributes:{}}}}"
            )
            assert "Size estimate unavailable" in ha.inner_text()
        finally:
            browser.close()
