"""Offline browser regression checks; pass --installed-assets to check the HA copy."""

# ruff: noqa: S101 - standalone regression assertions
import argparse
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--installed-assets", type=Path)
args = parser.parse_args()
here = Path(__file__).resolve().parent
assets = here.parents[1] / "src/suno_dl/webui_static"
snapshot = {
    "schema": 1,
    "generated_at": "2026-11-05T00:00:00Z",
    "clips": [{"id": "one", "title": "First"}, {"id": "two", "title": "Second"}],
    "tracks": {
        k: {"audio": "media-source://media_source/local/trackie_ct107/drinko/" + k + ".m4a"}
        for k in ("one", "two")
    },
    "genres": {"categories": [], "assignments": {}, "pinned": []},
    "lyrics": {},
}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


with TemporaryDirectory() as tmp:
    root = Path(tmp)
    dest = root / "local/suno-genres"
    dest.mkdir(parents=True)
    card = (
        (args.installed_assets / "before-suno-genre-card.js").read_text()
        if args.installed_assets
        else (assets / "suno-genre-card.js").read_text()
    )
    core = (
        (args.installed_assets / "before-suno-genre-core.js").read_text()
        if args.installed_assets
        else (assets / "suno-genre-core.js").read_text()
    )
    (dest / "suno-genre-card.js").write_text(
        card
        + """
import { installHaLibraryBridge } from "./trackie-ha-bridge.js";
installHaLibraryBridge(SunoGenreCard);
"""
    )
    (dest / "suno-genre-core.js").write_text(core)
    (dest / "trackie-ha-bridge.js").write_text((here / "trackie-ha-bridge.js").read_text())
    (dest / "ct107-library.json").write_text(json.dumps(snapshot))
    (root / "index.html").write_text("""<script type="module">
import '/local/suno-genres/suno-genre-card.js';
window.card = document.createElement('suno-genre-card');
card.setConfig({title:'CT107'});
document.body.append(card);
</script>""")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=tmp))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path="/usr/bin/google-chrome")
        page = browser.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: requests.append((r.method, r.url)))
        page.goto("http://127.0.0.1:" + str(server.server_port) + "/index.html")
        page.wait_for_function("window.card && card._tracksInfo.state === 'ok'")
        assert page.evaluate("card._clips.length") == 2
        assert page.evaluate("card._readOnly")
        assert page.locator("#keep-open, #reclassify").count() == 0
        assert page.evaluate("card.shadowRoot.textContent.includes('CT107 library snapshot')")
        assert all(
            url.startswith("http://127.0.0.1:" + str(server.server_port) + "/")
            for _, url in requests
        )
        assert not any(method != "GET" for method, _ in requests)
        page.evaluate("""() => {
          card.hass = {states: {sensor: {}, "sensor.trackie_sync_status": {
            state:"waiting", attributes:{updated_at:new Date().toISOString(),
            selected_playlists:["Electronic", "For You"],missing:85,already_local:8,
            downloaded:1,failed:0,remaining:84,current_title:"<img src=x onerror=alert(1)>",
            next_transfer_at:new Date(Date.now()+240000).toISOString(),
            message:"Audio and artwork only; temporary video discarded"}
          }}};
        }""")
        assert page.locator("#ct107-sync-status progress").get_attribute("max") == "85"
        assert page.locator("#ct107-sync-status progress").get_attribute("value") == "1"
        assert page.locator("#ct107-sync-status img").count() == 0
        assert "Next transfer after" in page.locator("#ct107-sync-status").inner_text()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.evaluate("""() => {
          card.hass = {states: {"sensor.trackie_sync_status": {
            state:"downloading",attributes:{updated_at:"2000-01-01T00:00:00Z"}
          }}};
        }""")
        assert "out of date" in page.locator("#ct107-sync-status").inner_text()
        # Fake only the local Audio object: never play actual sound or call a player service.
        page.evaluate("""() => {
          window.ws = [];
          window.audio = {paused:true,play(){this.paused=false;return Promise.resolve()},
                          pause(){this.paused=true},src:''};
          card._ensureAudio = () => {card._audio=audio;return audio};
          card.hass = {callWS:async msg => {
            ws.push(msg);return {url:'/media/local/trackie_ct107/drinko/test.m4a?authSig=fake'};
          }};
        }""")
        page.evaluate("card._togglePlay('one')")
        assert page.evaluate("card._playing") == "one"
        assert page.evaluate("ws[0].type") == "media_source/resolve_media"
        assert page.evaluate("audio.src.includes('/media/local/trackie_ct107/')")
        page.evaluate("card._togglePlay('one')")
        assert page.evaluate("audio.paused")
        assert page.evaluate("ws.length") == 1
        # Last click wins even if two resolve requests finish out of order.
        page.evaluate("""() => {
          card._playing=null;window.pending={};
          card.hass={callWS:msg=>new Promise(resolve=>pending[msg.media_content_id]=resolve)};
          card._togglePlay('one');card._togglePlay('two');
          pending[card._tracks.two.audio]({url:'/media/local/trackie_ct107/two.m4a'});
        }""")
        page.wait_for_function("card._playing === 'two'")
        page.evaluate("pending[card._tracks.one.audio]({url:'/media/local/trackie_ct107/one.m4a'})")
        assert page.evaluate("card._playing") == "two"
        # Refuse traversal before HA is asked to resolve anything.
        page.evaluate("""() => {
          card._tracks.bad={audio:'media-source://media_source/local/trackie_ct107/%2e%2e/secret.m4a'};
          card.hass={callWS:()=>{throw Error('resolver must not be called')}};
        }""")
        page.evaluate("card._togglePlay('bad')")
        assert "Invalid CT107" in page.evaluate("card._err")
        # Cross-origin results cannot become audio URLs.
        page.evaluate("card.hass={callWS:async()=>({url:'https://example.invalid/media/a.m4a'})}")
        page.evaluate("card._togglePlay('one')")
        assert "unexpected media URL" in page.evaluate("card._err")
        # Removed cards cannot start playback after a slow resolve.
        page.evaluate("""() => {
          card._playing=null;
          card.hass={callWS:()=>new Promise(resolve=>window.late=resolve)};
          card._togglePlay('one');card.remove();
          late({url:'/media/local/trackie_ct107/one.m4a'});
        }""")
        page.wait_for_timeout(100)
        assert page.evaluate("card._playing") is None
        assert errors == [], errors
        browser.close()
    server.shutdown()
print(
    "PASS: read-only load, no retired endpoints, HA-local playback, pause, "
    "click ordering, path/origin safety, disconnected recovery"
)
