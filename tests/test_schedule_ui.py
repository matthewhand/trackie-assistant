"""Real HTTP and browser acceptance for editable backend scheduling."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import httpx
from playwright.sync_api import expect, sync_playwright

from suno_dl.blocklist import load_blocked_ids
from suno_dl.schedule import Scheduler
from suno_dl.webui import RunManager, _make_handler
from tests.test_producer_lookup import page as song_page


@contextlib.contextmanager
def backend(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(music))
    manager = RunManager(tmp_path)
    controller = Scheduler(
        tmp_path, manager, lambda src: ["deliberately-not-a-real-sync"], ["@example"]
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_handler(tmp_path, manager, scheduler=controller)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, controller, manager
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        controller.close()


def test_schedule_api_rejects_cross_site_changes_and_invalid_options(tmp_path, monkeypatch):
    with backend(tmp_path, monkeypatch) as (port, controller, manager):
        for origin, options, expected in [
            (
                "https://evil.example",
                {"enabled": True, "interval_minutes": 360, "sources": ["@example"]},
                403,
            ),
            (None, {"enabled": True, "interval_minutes": 0, "sources": ["@example"]}, 400),
            (None, {"enabled": True, "interval_minutes": 360, "sources": ["@example"]}, 200),
        ]:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            connection.request("POST", "/api/schedule", json.dumps(options), headers)
            response = connection.getresponse()
            assert response.status == expected
            response.read()
            connection.close()
        assert controller.snapshot()["enabled"] is True
        assert manager.list() == []  # saving does not bypass the run manager


def test_schedule_ui_persists_hours_sources_and_pause(tmp_path, monkeypatch):
    with backend(tmp_path, monkeypatch) as (port, controller, manager), sync_playwright() as p:
        options = {"headless": True}
        if executable := os.environ.get("SUNO_BROWSER_EXECUTABLE"):
            options["executable_path"] = executable
        browser = p.chromium.launch(**options)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 900})
            page.goto("http://127.0.0.1:" + str(port) + "/?section=sync")
            panel = page.locator("#automatic-sync")
            panel.wait_for(state="visible")
            page.get_by_label("Check for new music automatically").check()
            page.get_by_label("Check every (hours)").fill("3")
            page.get_by_label("Scheduled profiles or playlists").fill("@another")
            page.get_by_role("button", name="Save schedule", exact=True).click()
            page.wait_for_function(
                'document.getElementById("schedule-status").textContent.includes("Saved.")'
            )
            value = controller.snapshot()
            assert value["enabled"] is True and value["interval_minutes"] == 180
            assert value["sources"] == ["https://suno.com/@another"]
            page.get_by_label("Check for new music automatically").uncheck()
            page.get_by_role("button", name="Save schedule", exact=True).click()
            page.wait_for_function(
                'document.getElementById("schedule-status").textContent.includes("paused")'
            )
            assert controller.snapshot()["enabled"] is False
            assert controller.snapshot()["next_run"] is None
            assert manager.list() == []
            panel.screenshot(path=str(tmp_path / "schedule-mobile.png"))
        finally:
            browser.close()


def test_skip_list_shows_legacy_notes_and_saves_from_sync_panel(tmp_path, monkeypatch, mock_http):
    clip = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"

    def suno(request):
        assert request.url.host == "suno.com"
        if request.url.path == "/s/TestShareCode123":
            return httpx.Response(307, headers={"Location": f"/song/{second}?sh=TestShareCode123"})
        assert request.url.path == f"/song/{second}"
        assert request.url.params["sh"] == "TestShareCode123"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=song_page(second, title="Autofilled track"),
        )

    mock_http(suno)
    blocked = tmp_path / ".blocked"
    blocked.write_text(
        f"# Keep this archive note\n# Song <b>title</b> | Video returned HTTP 403\n{clip}\n"
    )
    with backend(tmp_path, monkeypatch) as (port, controller, manager), sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("SUNO_BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page(viewport={"width": 390, "height": 900})
            page.goto(f"http://127.0.0.1:{port}/?section=sync")
            entries = page.locator("#skip-entries")
            entries.get_by_role("link", name="Song <b>title</b>").wait_for()
            assert "HTTP 403" in entries.inner_text()
            page.get_by_role("button", name="Edit skip list").click()
            assert page.get_by_label("Suno link").input_value() == f"https://suno.com/song/{clip}"
            page.get_by_label("Title", exact=True).fill("Song | title")
            page.get_by_label("Reason").fill("New reason")
            page.get_by_role("button", name="Add track").click()
            page.get_by_label("Suno link").last.fill("https://suno.com/s/TestShareCode123")
            page.wait_for_function(
                "document.querySelector('#skip-rows fieldset:last-child [name=title]').value"
                " === 'Autofilled track'"
            )
            assert (
                page.get_by_label("Suno link").last.input_value()
                == f"https://suno.com/song/{second}"
            )
            page.get_by_label("Title", exact=True).last.fill("Another track")
            page.get_by_label("Reason").last.fill("No public audio")
            page.get_by_role("button", name="Save skip list").click()
            entries.get_by_role("link", name="Song | title").wait_for()
            entries.get_by_role("link", name="Another track").wait_for()
            assert "# Keep this archive note" in blocked.read_text()
            page.get_by_role("button", name="Edit skip list").click()
            page.get_by_role("button", name="Remove track").last.click()
            page.get_by_role("button", name="Save skip list").click()
            page.locator("#skip-editor").wait_for(state="hidden")
            assert entries.get_by_role("link", name="Another track").count() == 0
            assert second not in blocked.read_text()
            page.get_by_role("button", name="Edit skip list").click()
            blocked.write_text(f"{clip} # Song title | External change")
            page.get_by_role("button", name="Remove track").click()
            page.get_by_role("button", name="Save skip list").click()
            page.get_by_text("Skip list changed. Reload before saving.").wait_for()
            assert "External change" in blocked.read_text()
            assert manager.list() == []
        finally:
            browser.close()


def test_failed_download_opens_prefilled_skip_list_draft(tmp_path, monkeypatch):
    clip = "11111111-1111-4111-8111-111111111111"
    blocked = tmp_path / ".blocked"
    with backend(tmp_path, monkeypatch) as (port, controller, manager), sync_playwright() as p:
        cmd = [
            sys.executable,
            "-c",
            "from suno_dl.download_failures import report_failed_track; "
            f"report_failed_track({clip!r}, 'A failed song', 'Published media returned HTTP 403.')",
        ]
        assert manager.start("sync", cmd)[0] is not None
        browser = p.chromium.launch(executable_path=os.environ.get("SUNO_BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page(viewport={"width": 390, "height": 900})
            page.goto(f"http://127.0.0.1:{port}/?section=sync")
            failure = page.locator("#failed-downloads")
            failure.get_by_role("link", name="A failed song").wait_for()
            failure.get_by_role("button", name="Skip track").click()
            assert page.get_by_label("Suno link").input_value() == f"https://suno.com/song/{clip}"
            assert page.get_by_label("Title", exact=True).input_value() == "A failed song"
            assert page.get_by_label("Reason").input_value() == "Published media returned HTTP 403."
            assert not blocked.exists()
            failure.get_by_role("button", name="Skip track").click()
            assert page.get_by_label("Suno link").count() == 1
            page.get_by_role("button", name="Save skip list").click()
            page.get_by_text("Skip list saved.", exact=False).wait_for()
            assert load_blocked_ids(blocked) == {clip}
            failure.get_by_text("On skip list").wait_for()
        finally:
            browser.close()


def test_failed_download_can_retry_only_that_song(tmp_path, monkeypatch):
    clip = "11111111-1111-4111-8111-111111111111"
    url = f"https://suno.com/song/{clip}"
    with backend(tmp_path, monkeypatch) as (port, controller, manager), sync_playwright() as p:
        base = f"http://127.0.0.1:{port}"
        assert (
            httpx.post(
                base + "/api/schedule",
                json={"enabled": True, "interval_minutes": 1440, "sources": ["@example"]},
            ).status_code
            == 200
        )
        assert (
            httpx.post(
                base + "/api/sync",
                data={"dry_run": "0", "retry_track": clip, "sources": "@example"},
            ).status_code
            == 400
        )
        finish = threading.Event()
        commands = []
        sync_done = threading.Event()
        execute = manager._execute

        def stub_retry(run):
            if run.retry_ids:
                commands.append(run.cmd)
                if finish.wait(timeout=15):
                    run.cmd = [
                        sys.executable,
                        "-c",
                        f"print('TRACKIE_ARCHIVED {clip}')",
                    ]
                else:
                    run.cmd = [sys.executable, "-c", "raise SystemExit(1)"]
            execute(run)
            if run.kind == "sync":
                sync_done.set()

        monkeypatch.setattr(manager, "_execute", stub_retry)
        assert (
            manager.start(
                "sync",
                [
                    sys.executable,
                    "-c",
                    "from suno_dl.download_failures import report_failed_track; "
                    f"report_failed_track({clip!r}, 'A failed song', 'HTTP 403')",
                ],
            )[0]
            is not None
        )
        assert sync_done.wait(timeout=10)
        controller.tick()
        deadline = controller.snapshot()["next_run"]
        browser = p.chromium.launch(executable_path=os.environ.get("SUNO_BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page()
            page.goto(base + "/?section=sync")
            failure = page.locator("#failed-downloads")
            retry = failure.get_by_role("button", name="Retry track")
            with page.expect_response(
                lambda response: response.url.endswith("/api/sync")
            ) as result:
                retry.click()
            assert result.value.ok
            page.get_by_text("retry track — running", exact=True).wait_for()
            assert retry.is_disabled()
            assert len(commands) == 1
            cmd = commands[0]
            assert cmd.count("--watch") == 1
            assert cmd[cmd.index("--watch") + 1] == url
            assert "@example" not in cmd
            assert {"--no-rotate", "--playback-paced", "--no-videos"} <= set(cmd)
            finish.set()
            failure.wait_for(state="hidden")
            assert manager.list()[0]["archived_tracks"] == [clip]
            controller.tick()
            assert controller.snapshot()["next_run"] == deadline
        finally:
            finish.set()
            browser.close()


def test_retry_all_uses_only_unblocked_failures_and_keeps_unsuccessful_rows(tmp_path, monkeypatch):
    first, second, skipped, blocked = (
        f"00000000-0000-4000-8000-{number:012}" for number in range(1, 5)
    )
    finish = tmp_path / "finish-retry"
    (tmp_path / ".blocked").write_text(blocked + "\n")
    with backend(tmp_path, monkeypatch) as (port, _controller, manager), sync_playwright() as p:
        base = f"http://127.0.0.1:{port}"
        opts = {"dry_run": "0", "retry_failed": "1"}
        assert httpx.post(base + "/api/sync", data=opts).status_code == 409
        assert (
            httpx.post(base + "/api/sync", data={**opts, "sources": "@example"}).status_code == 400
        )
        assert manager.list() == []
        commands = []
        execute = manager._execute

        def stub_retry(run):
            if run.retry_ids:
                commands.append(run.cmd)
                script = f"""
from pathlib import Path
from time import sleep
from suno_dl.download_failures import report_failed_track
print('TRACKIE_RETRY {first} working', flush=True)
print('TRACKIE_ARCHIVED {first}', flush=True)
print('TRACKIE_RETRY {second} working', flush=True)
for _ in range(160):
    if Path({str(finish)!r}).exists():
        break
    sleep(0.1)
report_failed_track({second!r}, 'Song two', 'Still unavailable')
print('TRACKIE_RETRY {second} failed')
print('TRACKIE_RETRY {skipped} skipped')
"""
                run.cmd = [sys.executable, "-c", script]
            execute(run)

        monkeypatch.setattr(manager, "_execute", stub_retry)
        reports = "; ".join(
            f"report_failed_track({clip!r}, {title!r}, 'HTTP 403')"
            for clip, title in [
                (first, "Song one"),
                (second, "Song two"),
                (skipped, "Song skipped during retry"),
                (blocked, "Skipped song"),
            ]
        )
        assert (
            manager.start(
                "sync",
                [
                    sys.executable,
                    "-c",
                    "from suno_dl.download_failures import report_failed_track; " + reports,
                ],
            )[0]
            is not None
        )
        browser = p.chromium.launch(executable_path=os.environ.get("SUNO_BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page()
            page.goto(base + "/?section=sync")
            panel = page.locator("#failed-downloads")
            with page.expect_response(
                lambda response: response.url.endswith("/api/sync")
            ) as result:
                panel.get_by_role("button", name="Retry all (3 failed tracks)").click()
            assert result.value.ok
            expect(page.locator("#op-line")).to_have_text("Retry all · 2 of 3 · Song two")
            finish.touch()
            summary = page.locator("#retry-summary")
            expect(summary).to_contain_text("1 recovered · 1 still failed · 1 skipped")
            original = httpx.get(base + "/api/runs").json()["last_retry_result"]
            assert original == {
                "finished": manager.list()[0]["finished"],
                "recovered": 1,
                "failed": 1,
                "skipped": 1,
                "unfinished": 0,
                "stopped": False,
            }
            timestamp = page.evaluate(
                "(epoch) => new Date(epoch * 1000).toLocaleString()", original["finished"]
            )
            expected = (
                f"Last Retry all finished {timestamp} · 1 recovered · 1 still failed · 1 skipped"
            )
            expect(summary).to_have_text(expected)
            panel.get_by_text("Still unavailable", exact=False).wait_for()
            assert panel.get_by_role("link", name="Song one").count() == 0
            assert panel.get_by_text("On skip list").count() == 1
            assert len(commands) == 1
            cmd = commands[0]
            assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--watch"] == [
                f"https://suno.com/song/{clip}" for clip in (first, second, skipped)
            ]
            assert cmd[cmd.index("--concurrency") + 1] == "1"
            assert "--playback-paced" in cmd
        finally:
            browser.close()

    with backend(tmp_path, monkeypatch) as (port, _controller, manager), sync_playwright() as p:
        base = f"http://127.0.0.1:{port}"
        assert manager.list() == []
        assert httpx.get(base + "/api/runs").json()["last_retry_result"] == original
        browser = p.chromium.launch(executable_path=os.environ.get("SUNO_BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page()
            page.goto(base + "/?section=sync")
            expect(page.locator("#retry-summary")).to_have_text(expected)
        finally:
            browser.close()


def test_failures_survive_restart_and_later_sync_with_counted_retry(tmp_path, monkeypatch):
    clips = [f"10000000-0000-4000-8000-{n:012}" for n in range(42)]
    recovered, skipped = clips[:2]
    (tmp_path / ".blocked").write_text(skipped + "\n")

    def run_sync(manager, script):
        assert manager.start("sync", [sys.executable, "-c", script])[0] is not None
        deadline = time.monotonic() + 10
        while manager.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager.list()[0]["state"] == "done"

    with backend(tmp_path, monkeypatch) as (port, controller, manager):
        run_sync(
            manager,
            "from suno_dl.download_failures import report_failed_track; "
            f"for_id = {clips!r}; "
            "[report_failed_track(clip, 'Disposable song', 'HTTP 403') for clip in for_id]",
        )
        controller.tick()  # An older release could have stored this truncated snapshot.
        run_sync(manager, f"print('TRACKIE_ARCHIVED {recovered}')")
        base = f"http://127.0.0.1:{port}"
        rows = httpx.get(base + "/api/runs").json()["failed_tracks"]
        assert [row["id"] for row in rows] == clips[1:]

    with backend(tmp_path, monkeypatch) as (port, _controller, manager), sync_playwright() as p:
        base = f"http://127.0.0.1:{port}"
        assert manager.list() == []
        browser = p.chromium.launch(executable_path=os.environ.get("SUNO_BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page()
            page.goto(base + "/?section=sync")
            retry = page.locator("#retry-all")
            expect(retry).to_have_text("Retry all (40 failed tracks)")
            (tmp_path / ".blocked").write_text("")
            response = httpx.post(
                base + "/api/sync",
                data={"dry_run": "0", "retry_failed": "1", "retry_count": "40"},
            )
            assert response.status_code == 409
            assert manager.list() == []
            expect(retry).to_have_text("Retry all (41 failed tracks)")
            run_sync(manager, f"print('TRACKIE_ARCHIVED {skipped}')")
            expect(retry).to_have_text("Retry all (40 failed tracks)")
        finally:
            browser.close()
