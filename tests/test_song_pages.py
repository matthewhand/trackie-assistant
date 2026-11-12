"""Whole-catalogue filters with 50 rows in either Trackie view."""

from __future__ import annotations

import json
import os
import threading
from http.server import ThreadingHTTPServer

import httpx
from playwright.sync_api import expect, sync_playwright

from suno_dl.webui import RunManager, _make_handler
from tests.test_linux_downloads import PAYLOAD


def test_live_song_pages_and_home_assistant_card(tmp_path, monkeypatch):
    audio_root = tmp_path / "music"
    audio_root.mkdir()
    genres = tmp_path / "genres"
    genres.mkdir()
    clips = []
    assignments = {}
    for n in range(112):
        cid = f"10000000-0000-4000-8000-{n:012}"
        title = f"Song {n:03d}"
        style = "Rare finale" if n == 111 else "General style"
        audio = audio_root / f"{n}.m4a"
        audio.write_bytes(PAYLOAD.read_bytes())
        audio.with_suffix(".json").write_text(
            json.dumps({"id": cid, "title": title, "tags": style, "saved_files": [audio.name]})
        )
        clips.append({"id": cid, "title": title, "style": style})
        assignments[cid] = "Collection" if n >= 55 else "Opening"
    (genres / "clips.json").write_text(json.dumps(clips))
    (genres / "genres.json").write_text(
        json.dumps(
            {
                "categories": [
                    {"name": "Opening", "count": 55},
                    {"name": "Collection", "count": 57},
                ],
                "assignments": assignments,
                "pinned": [clips[-1]["id"]],
            }
        )
    )
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(audio_root))
    monkeypatch.delenv("SUNO_GENRES_URL", raising=False)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_handler(tmp_path, RunManager(tmp_path), genres_dir=genres)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = httpx.get(base + "/api/songs?sort=az&page=3").json()
        assert body["total"] == 112 and body["page"] == 3 and len(body["clips"]) == 12
        assert body["clips"][-1]["id"] == clips[-1]["id"]
        assert httpx.get(base + "/api/songs?page=-1").status_code == 400
        endpoint = body["tracks"][clips[-1]["id"]]["audio"]
        response = httpx.get(base + endpoint, headers={"Range": "bytes=0-31"})
        assert response.status_code == 206 and response.content == PAYLOAD.read_bytes()[:32]
        for params in (
            {"search": "rare finale", "category": "Collection"},
            {"pinned": "1", "audio": "1"},
        ):
            result = httpx.get(base + "/api/songs", params=params).json()
            assert result["total"] == 1 and result["clips"][0]["id"] == clips[-1]["id"]

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("SUNO_BROWSER_EXECUTABLE"))
            try:
                for mode in ("standalone", "static", "ha-ingress"):
                    view = browser.new_page()
                    requests = []
                    view.on("request", lambda req, seen=requests: seen.append(req.url))
                    try:
                        if mode != "standalone":
                            config = (
                                '{"library_url":"/api/library"}'
                                if mode == "static"
                                else '{"ingress_app":"local_suno_dl"}'
                            )
                            harness = (
                                '<script type="module" src="/static/suno-genre-card.js"></script>'
                                '<suno-genre-card></suno-genre-card><script type="module">'
                                'await customElements.whenDefined("suno-genre-card");'
                                'const card = document.querySelector("suno-genre-card");'
                                f"card.setConfig({config});"
                                "card.hass={callWS:async ({type,slug})=>{"
                                'if(slug!=="local_suno_dl")throw Error("unexpected app");'
                                'return type==="supervisor/ingress/info"'
                                '? {ingress_url:"/api/hassio_ingress/fixture-token/?section=sync"}'
                                ': {session:"fixture-session"};}};</script>'
                            )
                            view.route(
                                base + "/harness",
                                lambda route, _request, document=harness: route.fulfill(
                                    body=document, content_type="text/html"
                                ),
                            )

                        def forward(route):
                            target = route.request.url.replace(
                                "/api/hassio_ingress/fixture-token", ""
                            )
                            assert (
                                "ingress_session=fixture-session"
                                in route.request.all_headers().get("cookie", "")
                            )
                            route.fulfill(response=route.fetch(url=target))

                        if mode == "ha-ingress":
                            view.route("**/api/hassio_ingress/fixture-token/**", forward)
                        view.goto(
                            base + ("/?section=library" if mode == "standalone" else "/harness")
                        )
                        card = view.locator("suno-genre-card")
                        expect(card.locator(".song")).to_have_count(50)
                        card.get_by_role("button", name="A–Z").click()
                        expect(card.locator("#song-page-status")).to_have_text("1–50 of 112 songs")
                        card.get_by_role("button", name="Next", exact=True).click()
                        expect(card.locator("#song-page-status")).to_have_text(
                            "51–100 of 112 songs"
                        )
                        card.get_by_role("searchbox", name="Search").fill("rare finale")
                        expect(card.locator(".song")).to_have_count(1)
                        expect(card.get_by_role("link", name="Song 111")).to_be_visible()
                        card.get_by_role("searchbox", name="Search").fill("")
                        expect(card.locator(".song")).to_have_count(50)
                        card.get_by_role("button", name="Collection", exact=False).first.click()
                        expect(card.locator("#song-page-status")).to_have_text("1–50 of 57 songs")
                        card.get_by_role("button", name="Next", exact=True).click()
                        expect(card.locator(".song")).to_have_count(7)
                        if mode != "static":
                            assert any("/api/songs?" in url and "page=2" in url for url in requests)
                            assert not any(
                                "/api/library" in url or "/api/tracks" in url for url in requests
                            )
                    finally:
                        view.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
