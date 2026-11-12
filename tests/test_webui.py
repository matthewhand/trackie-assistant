"""Offline tests for the local web UI dashboard.

HTTP endpoints are exercised against a real server on an ephemeral
localhost port; subprocess runs use trivial ``python -c`` commands so no
test touches Suno (important: the process environment may carry
SUNO_* values — e.g. injected by ``uv run`` — so the real command
builders must never be invoked from tests). The real subprocess path is
verified by the live smoke run, not here.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

import suno_dl.webui as webui
from suno_dl.webui import (
    RunManager,
    _compute_status,
    _host_allowed,
    _library_payload,
    _list_files,
    _machine_addresses,
    _make_handler,
    _recommend_command,
    _safe_media_path,
    _sync_command,
)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real dashboard server on an ephemeral port; yields (base URL, manager)."""
    # Even a newly opened capability gate must never launch an ambient sync.
    monkeypatch.setattr(
        webui,
        "_sync_command",
        lambda *_args, **_kwargs: [sys.executable, "-c", "print('offline sync fixture')"],
    )
    manager = RunManager(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(tmp_path, manager))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", manager
    httpd.shutdown()


def _get(url: str, host: str | None = None) -> tuple[int, dict | str]:
    headers = {"Host": host} if host else {}
    response = httpx.get(url, headers=headers, timeout=10.0)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text


def _post(url: str, data: dict[str, str]) -> tuple[int, dict]:
    response = httpx.post(url, data=data, timeout=10.0)
    return response.status_code, response.json()


# ───────────────────────────── pure helpers ─────────────────────────────


def test_host_guard_accepts_only_localhost() -> None:
    assert _host_allowed("127.0.0.1:8760")
    assert _host_allowed("localhost:8760")
    assert _host_allowed("[::1]:8760")
    assert not _host_allowed("evil.example.com")
    assert not _host_allowed(None)


def test_host_guard_accepts_every_ipv6_loopback_spelling() -> None:
    """A browser on IPv6 loopback sends ``Host: [::1]`` with no port on :80.

    Brackets are three colons' worth of trap: stripping them after splitting on
    ``:`` left ``":"`` and 403'd a legitimate loopback request. Equivalent
    literals (fully expanded, zero-padded) must also pass.
    """
    for host in ("[::1]", "[::1]:8760", "::1", "[0:0:0:0:0:0:0:1]", "localhost."):
        assert _host_allowed(host), host
    for host in ("[::2]", "[::2]:8760", "[fe80::1]:8760", "evil.example.com.", ":8760"):
        assert not _host_allowed(host), host


def test_machine_addresses_includes_hostname_not_foreign_names() -> None:
    import socket

    addresses = _machine_addresses()
    assert socket.gethostname().lower() in addresses
    assert "evil.example.com" not in addresses


def test_lan_mode_accepts_own_names_rejects_foreign(tmp_path: Path) -> None:
    """LAN posture: the machine's own names are legitimate Host values; an
    attacker-chosen DNS name resolving at the server is still refused."""
    manager = RunManager(tmp_path)
    allowed = frozenset({"library.example.lan", "192.0.2.10"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(tmp_path, manager, allowed))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, _ = _get(base + "/api/status", host="192.0.2.10:8760")
        assert code == 200
        code, _ = _get(base + "/api/status", host="library.example.lan:8760")
        assert code == 200
        code, body = _get(base + "/api/status", host="evil.example.com")
        assert code == 403 and "error" in body
    finally:
        httpd.shutdown()


def test_commands_are_subprocess_safe() -> None:
    cmd = _sync_command(dry_run=True)
    assert "--sync" in cmd and "--dry-run" in cmd and "--extract-audio" in cmd
    assert "--no-videos" in cmd and "--covers" in cmd
    assert "--keep-video" not in cmd
    with_videos = _sync_command(dry_run=False, videos=True, covers=False)
    assert "--videos" in with_videos and "--no-covers" in with_videos
    assert "--keep-video" in with_videos and "--extract-audio" in with_videos
    assert "--dry-run" not in with_videos
    assert "--keep-video" in _sync_command(dry_run=True, videos=True)
    assert "--keep-video" not in _sync_command(dry_run=False, videos=True, keep_video=False)
    rec = _recommend_command(["@a", "@b"], taste=True, archive=Path("/x"))
    assert "@a" in rec and "@b" in rec and "--taste-from" in rec


def test_ingress_sync_blocks_video_retention_even_with_videos_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant ingress must never request the MP4-retention flag."""
    from types import SimpleNamespace

    manager = RunManager(tmp_path)
    commands: list[list[str]] = []

    def fake_start(kind, command):
        commands.append(command)
        return SimpleNamespace(id="synthetic", request_id="synthetic"), None

    monkeypatch.setattr(manager, "start", fake_start)
    monkeypatch.setattr(webui, "_require_trusted_framing", lambda: None)
    monkeypatch.setattr(webui, "require_audio_sync", lambda **options: None)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _make_handler(tmp_path, manager, ingress_mode=True, trusted_ingress_peer="127.0.0.1"),
    )
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        response = httpx.post(
            base + "/api/sync",
            data={"videos": "1", "covers": "1", "dry_run": "1"},
            timeout=10.0,
        )
        assert response.status_code == 200, response.text
        assert len(commands) == 1
        assert "--videos" in commands[0] and "--extract-audio" in commands[0]
        assert "--keep-video" not in commands[0]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_home_assistant_addon_video_extraction_contract_remains_unchanged() -> None:
    """The add-on never opts into the WebUI-only keep-video behavior."""
    root = Path(__file__).resolve().parents[1]
    import json

    options = json.loads((root / "addon/suno_dl/config.json").read_text())
    script = (root / "addon/suno_dl/run.sh").read_text()
    assert options["options"]["download_videos"] is False
    assert options["options"]["sync_on_start"] is False
    assert 'SYNC_FLAGS=(--sync --output-dir "$MEDIA" --extract-audio)' in script
    assert "--keep-video" not in script


def test_compute_status_never_includes_secrets(tmp_path: Path) -> None:
    (tmp_path / "downloads" / "drinko").mkdir(parents=True)
    (tmp_path / "downloads" / "drinko" / "drinko_X.m4a").write_bytes(b"x" * 2048)
    (tmp_path / ".blocked").write_text("7c68eb8d-4bad-43d5-8d37-8be1fd48fb58\n", encoding="utf-8")
    env = {
        "SUNO_OUTPUT_DIR": str(tmp_path / "downloads"),
        "SUNO_WATCH": "drinko, @other",
        "SUNO_JEV_URL": "http://jev.example.lan:8903/v1/evaluate",
        "SUNO_SESSION_COOKIE": "session=SUPER-SECRET-VALUE",
    }
    status = _compute_status(tmp_path, env=env)
    dumped = json.dumps(status)
    assert "SUPER-SECRET-VALUE" not in dumped  # secrets never cross the UI
    assert status["watch"] == ["drinko", "@other"]
    assert status["blocked"]["count"] == 1
    assert status["profiles"][0]["name"] == "drinko"
    assert status["profiles"][0]["tracks"] == 1
    assert status["engine"] == "JEV configured"


def test_status_does_not_expose_jev_endpoint_credentials(tmp_path: Path) -> None:
    url = "https://user:synthetic-password@model.example/api?token=synthetic-token#private"
    status = _compute_status(tmp_path, env={"SUNO_JEV_URL": url})
    serialized = json.dumps(status)
    assert status["engine"] == "JEV configured"
    assert "synthetic-password" not in serialized
    assert "synthetic-token" not in serialized
    assert "model.example" not in serialized
    assert url not in serialized


def test_status_uses_heuristic_when_no_jev_url_is_configured(tmp_path: Path) -> None:
    status = _compute_status(tmp_path, env={})
    assert status["engine"] == "heuristic (no JEV endpoint configured)"


# ───────────────────────────── HTTP surface ─────────────────────────────


def test_status_and_pages_over_real_http(server) -> None:
    base, _manager = server
    code, page = _get(base + "/")
    assert code == 200
    assert "<title>Trackie Assistant</title>" in page
    assert "<h1>Trackie Assistant</h1>" in page
    assert "api/status" in page
    code, status = _get(base + "/api/status")
    assert code == 200 and "version" in status
    code, payload = _get(base + "/api/runs")
    assert code == 200 and payload == {"runs": [], "failed_tracks": [], "last_retry_result": None}
    code, _ = _get(base + "/nope")
    assert code == 404


def test_rejects_non_localhost_host_header(server) -> None:
    base, _manager = server
    code, body = _get(base + "/api/status", host="evil.example.com")
    assert code == 403 and "error" in body


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "null", "http://localhost.evil", "http://[invalid"]
)
def test_cross_site_lan_post_is_rejected_without_starting_a_run(server, origin: str) -> None:
    base, manager = server
    response = httpx.post(
        base + "/api/sync",
        data={"dry_run": "1"},
        headers={"Origin": origin},
        timeout=10.0,
    )
    assert response.status_code == 403
    assert manager.list() == []


def test_same_origin_post_still_reaches_api(server) -> None:
    base, _manager = server
    # Invalid request gives 400, not the 403 from the new Origin guard.
    response = httpx.post(
        base + "/api/recommend",
        data={"candidates": ""},
        headers={"Origin": base},
        timeout=10.0,
    )
    assert response.status_code == 400


def test_tls_proxy_same_host_origin_is_accepted(server) -> None:
    base, manager = server
    host = base.removeprefix("http://")
    # A trusted reverse proxy may terminate HTTPS before the stdlib listener.
    response = httpx.post(
        base + "/api/recommend",
        data={"candidates": ""},
        headers={"Origin": f"https://{host}"},
        timeout=10.0,
    )
    assert response.status_code == 400  # no candidates, not an Origin denial
    assert manager.list() == []


def test_explicitly_allowed_cross_origin_is_accepted(tmp_path: Path) -> None:
    manager = RunManager(tmp_path)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _make_handler(
            tmp_path,
            manager,
            cors_origins=frozenset({"https://ha.example"}),
        ),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        response = httpx.post(
            base + "/api/recommend",
            data={"candidates": ""},
            headers={"Origin": "https://ha.example"},
            timeout=10.0,
        )
        assert response.status_code == 400
        assert response.headers["Access-Control-Allow-Origin"] == "https://ha.example"
    finally:
        server.shutdown()


@pytest.mark.parametrize("raw_length", ["abc", "-1"])
def test_invalid_content_length_is_rejected_without_a_run(server, raw_length: str) -> None:
    base, manager = server
    port = int(base.rsplit(":", 1)[1])
    request = (
        "POST /api/sync HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Length: {raw_length}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as conn:
        conn.sendall(request)
        header = conn.makefile("rb").readline()
    assert b" 400 " in header
    assert manager.list() == []


def test_missing_content_length_returns_411_without_starting_a_run(server) -> None:
    base, manager = server
    port = int(base.rsplit(":", 1)[1])
    request = (
        f"POST /api/sync HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as conn:
        conn.sendall(request)
        status = conn.makefile("rb").readline()
    assert b" 411 " in status
    assert manager.list() == []


def test_empty_post_must_not_trigger_real_sync(server) -> None:
    base, manager = server
    response = httpx.post(base + "/api/sync", content=b"", timeout=10.0)
    assert response.status_code == 400
    assert manager.list() == []


def test_bodyless_cancel_post_is_still_accepted(server) -> None:
    """Run cancel carries no payload; the body-length guard must not break it."""
    import sys

    base, manager = server
    run, error = manager.start(
        "synthetic-cancel", [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    assert run is not None and error is None
    try:
        response = httpx.post(base + f"/api/runs/{run.id}/cancel", timeout=10.0)
        assert response.status_code == 200
        assert response.json()["state"] == "cancelling"
    finally:
        manager.cancel(run.id)


def test_oversized_post_returns_413_without_a_run(server) -> None:
    base, manager = server
    port = int(base.rsplit(":", 1)[1])
    # The guard must reject the declared size before waiting for a body.
    # Sending the body concurrently can reset the connection on Windows when
    # the server closes with unread bytes still in the socket.
    request = (
        f"POST /api/sync HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Content-Length: 65537\r\nConnection: close\r\n\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as conn:
        conn.sendall(request)
        status = conn.makefile("rb").readline()
    assert b" 413 " in status
    assert manager.list() == []


def test_invalid_utf8_post_returns_400_without_a_run(server) -> None:
    base, manager = server
    response = httpx.post(
        base + "/api/sync",
        content=b"\xff",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10.0,
    )
    assert response.status_code == 400
    assert manager.list() == []


def test_sync_rejects_json_and_ambiguous_options(server) -> None:
    base, manager = server
    for body in ({"dry_run": True}, {"dry_run": False}):
        response = httpx.post(base + "/api/sync", json=body, timeout=10.0)
        assert response.status_code == 415
    for body in ({"videos": "1"}, {"dry_run": "true"}, {"dry_run": "1", "covers": "maybe"}):
        code, _ = _post(base + "/api/sync", body)
        assert code == 400
    response = httpx.post(
        base + "/api/sync",
        content="dry_run=0&dry_run=",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10.0,
    )
    assert response.status_code == 400
    assert manager.list() == []


def test_supported_linux_sync_and_preview_can_start(server):
    base, manager = server
    code, body = _post(base + "/api/sync", {"dry_run": "1"})
    assert code == 200 and "id" in body
    deadline = time.time() + 5
    while manager.busy and time.time() < deadline:
        time.sleep(0.01)
    code, body = _post(base + "/api/sync", {"dry_run": "0"})
    assert code == 200 and "id" in body


def test_run_lifecycle_end_to_end(server, monkeypatch: pytest.MonkeyPatch) -> None:
    """Start → output capture → completion → busy refusal — with a fake command.

    The real command builders are never used here: the ambient environment
    may carry SUNO_WATCH etc. (uv injects the repo .env), which would launch
    a genuine Playwright sync from inside the test.
    """
    monkeypatch.setattr(webui, "_require_trusted_framing", lambda: None)
    monkeypatch.setattr(webui, "require_audio_sync", lambda **options: None)
    monkeypatch.setattr(
        webui,
        "_sync_command",
        lambda dry_run, **_kw: [sys.executable, "-c", "print('canary output line')"],
    )
    base, manager = server
    code, body = _post(base + "/api/sync", {"dry_run": "1"})
    assert code == 200 and "id" in body

    deadline = time.time() + 15
    while time.time() < deadline and manager.busy:
        time.sleep(0.05)
    assert not manager.busy
    code, payload = _get(base + "/api/runs")
    run = payload["runs"][0]
    assert run["kind"].startswith("sync")
    assert run["state"] == "done" and run["returncode"] == 0
    assert "canary output line" in run["output"]

    # A new command while busy is refused (one run at a time).
    manager.start("sleeper", [sys.executable, "-c", "import time; time.sleep(3)"])
    time.sleep(0.2)
    assert manager.busy
    code, body = _post(base + "/api/sync", {"dry_run": "1"})
    assert code == 409 and "error" in body


def test_run_snapshot_redacts_subprocess_secrets() -> None:
    """The API must not echo raw CLI stdout to LAN/dashboard clients."""
    run = webui.Run(
        id="synthetic",
        kind="test",
        cmd=["never-execute"],
        returncode=0,
        output=[
            "Authorization: Bearer synthetic-secret-token-abcdefghijkl",
            "api_key=another_synthetic_secret_value",
            "session=synthetic-session-value",
            "download completed normally",
        ],
    )
    snapshot = run.snapshot()
    output = "\n".join(snapshot["output"])
    assert "synthetic-secret-token-abcdefghijkl" not in output
    assert "another_synthetic_secret_value" not in output
    assert "synthetic-session-value" not in output
    assert "download completed normally" in output
    assert "[redacted]" in output


def test_cancel_before_subprocess_registration_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel during a blocked Popen must not leave the job running."""
    entered_popen = threading.Event()
    release_popen = threading.Event()
    terminated = threading.Event()

    class FakeProcess:
        stdout = iter(())
        terminate_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1
            terminated.set()

        def wait(self) -> int:
            assert terminated.wait(3), "cancel was lost while Popen was starting"
            return -15

    fake_process = FakeProcess()

    def blocked_popen(*_args: object, **_kwargs: object) -> FakeProcess:
        entered_popen.set()
        assert release_popen.wait(3), "test never released the blocked Popen"
        return fake_process

    monkeypatch.setattr(webui.subprocess, "Popen", blocked_popen)
    manager = RunManager(tmp_path)
    run, error = manager.start("synthetic-cancel", ["never-execute-a-real-command"])
    assert error is None and run is not None
    try:
        assert entered_popen.wait(3)
        assert run.process is None
        cancelled, error = manager.cancel(run.id)
        assert cancelled is run and error is None
        # A second Cancel is idempotent, not a second signal.
        cancelled, error = manager.cancel(run.id)
        assert cancelled is run and error is None
    finally:
        release_popen.set()

    assert terminated.wait(3)
    deadline = time.monotonic() + 3
    while manager.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not manager.busy
    assert fake_process.terminate_calls == 1
    assert manager.list()[0]["state"] == "cancelled"


# ───────────────────────── media pull (for HA) ─────────────────────────


def _media_env(tmp_path: Path) -> dict[str, str]:
    return {"SUNO_OUTPUT_DIR": str(tmp_path / "downloads")}


def test_list_files_lists_audio_only(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads" / "drinko"
    downloads.mkdir(parents=True)
    (downloads / "A Song.m4a").write_bytes(b"aa")
    (downloads / "B Song.mp3").write_bytes(b"bb")
    (downloads / "C Video.mp4").write_bytes(b"cc")  # video twin: excluded
    (downloads / "notes.txt").write_text("no", encoding="utf-8")
    payload = _list_files(tmp_path, env=_media_env(tmp_path))
    (profile,) = payload["profiles"]
    assert profile["name"] == "drinko"
    assert profile["files"] == ["A Song.m4a", "B Song.mp3"]
    assert profile["count"] == 2


def test_safe_media_path_containment(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads" / "drinko"
    downloads.mkdir(parents=True)
    real = downloads / "X.m4a"
    real.write_bytes(b"x")
    resolved = _safe_media_path(tmp_path, "drinko/X.m4a", env=_media_env(tmp_path))
    assert resolved == real.resolve()
    for bad in (
        "../.env",
        "drinko/../../etc/passwd",
        "drinko/sub/Y.m4a",  # deeper than profile/file
        ".blocked/x",  # hidden profile
        "drinko/.hidden.m4a",  # hidden file
        "drinko/X.mp4",  # video: never served
        "drinko/missing.m4a",  # nonexistent
    ):
        got = _safe_media_path(tmp_path, bad, env=_media_env(tmp_path))
        assert got is None, f"{bad} leaked as {got}"


def test_media_endpoints_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(tmp_path / "downloads"))
    downloads = tmp_path / "downloads" / "drinko"
    downloads.mkdir(parents=True)
    song = downloads / "Song One.m4a"
    song.write_bytes(b"fake-audio-bytes")

    monkeypatch.setattr(webui.mimetypes, "guess_type", lambda _: (None, None))
    manager = RunManager(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(tmp_path, manager))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, payload = _get(base + "/api/files")
        assert code == 200
        (profile,) = payload["profiles"]
        assert profile["files"] == ["Song One.m4a"]

        pulled = httpx.get(base + "/media/drinko/Song%20One.m4a", timeout=10.0)
        assert pulled.status_code == 200
        assert pulled.content == b"fake-audio-bytes"
        assert pulled.headers["content-type"].startswith("audio/")

        for path in (
            "/media/drinko/../../.env",  # traversal (encoded ..)
            "/media/.blocked/x",  # hidden profile
            "/media/drinko/missing.m4a",  # missing file
            "/media/drinko",  # wrong depth
        ):
            code, body = _get(base + path)
            assert code == 404, path
    finally:
        httpd.shutdown()


def test_library_endpoint_reads_genres_dir(tmp_path: Path) -> None:
    genres_dir = tmp_path / "suno-genres"
    genres_dir.mkdir()
    (genres_dir / "genres.json").write_text(
        '{"categories":[{"name":"Cyber Industrial","count":1}],'
        '"assignments":{"a":"Cyber Industrial"},"pinned":[]}',
        encoding="utf-8",
    )
    (genres_dir / "clips.json").write_text(
        '[{"id":"a","title":"Doomsday","style":"industrial","model":"v4"}]',
        encoding="utf-8",
    )
    payload = _library_payload(genres_dir)
    assert payload["ok"] is True
    assert payload["clips"][0]["title"] == "Doomsday"
    assert payload["genres"]["categories"][0]["name"] == "Cyber Industrial"

    manager = RunManager(tmp_path)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), _make_handler(tmp_path, manager, genres_dir=genres_dir)
    )
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, body = _get(base + "/api/library")
        assert code == 200 and body["ok"] is True
        assert body["clips"][0]["id"] == "a"
        code, page = _get(base + "/")
        assert code == 200 and ("genre-pane" in page or "chips" in page or "Suno archive" in page)
        assert "songs_url:'api/songs'" in page
    finally:
        httpd.shutdown()


def test_library_payload_empty_without_dir() -> None:
    payload = _library_payload(None)
    assert payload["ok"] is False
    assert payload["clips"] == []


def test_ingress_resolves_supervisor_dns_and_refuses_unresolved_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the supervisor DNS identity is trusted; unavailable DNS fails closed."""
    import socket

    lookups: list[str] = []

    def resolved(name: str) -> str:
        lookups.append(name)
        return "127.0.0.1"

    monkeypatch.setattr(socket, "gethostbyname", resolved)
    manager = RunManager(tmp_path)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _make_handler(tmp_path, manager, ingress_mode=True),
    )
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        response = httpx.get(f"http://127.0.0.1:{httpd.server_address[1]}/api/status")
        assert response.status_code == 200
        assert lookups == ["supervisor"]
    finally:
        httpd.shutdown()
        httpd.server_close()

    def unresolved(_name: str) -> str:
        raise socket.gaierror("synthetic supervisor DNS failure")

    monkeypatch.setattr(socket, "gethostbyname", unresolved)
    with pytest.raises(socket.gaierror, match="synthetic supervisor DNS failure"):
        _make_handler(tmp_path, manager, ingress_mode=True)


def test_ingress_denies_direct_addon_access_even_with_spoofed_proxy_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without Supervisor as the TCP peer, all UI and API operations are denied."""
    manager = RunManager(tmp_path)
    started: list[str] = []

    def fail_start(*_args: object, **_kwargs: object) -> None:
        started.append("unexpected")
        raise AssertionError("untrusted peer must not start any command")

    monkeypatch.setattr(manager, "start", fail_start)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _make_handler(tmp_path, manager, ingress_mode=True, trusted_ingress_peer="203.0.113.8"),
    )
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    headers = {
        "X-Forwarded-For": "203.0.113.8",
        "X-Ingress-Path": "/api/hassio_ingress/fictional/",
        "X-Remote-User-Id": "fictional-admin",
    }
    try:
        for path in ("/", "/api/status", "/api/tracks", "/api/files"):
            response = httpx.get(base + path, headers=headers, timeout=5.0)
            assert response.status_code == 403
        response = httpx.post(
            base + "/api/sync",
            headers=headers,
            data={"dry_run": "0", "covers": "1"},
            timeout=5.0,
        )
        assert response.status_code == 403
        assert started == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_ingress_mode_disables_host_guard(tmp_path: Path) -> None:
    """Behind a trusted proxy (HA ingress), Host is proxy-internal: no guard."""
    manager = RunManager(tmp_path)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _make_handler(
            tmp_path, manager, frozenset(), ingress_mode=True, trusted_ingress_peer="127.0.0.1"
        ),
    )
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, _ = _get(base + "/api/status", host="a9b2-suno-dl:8080")
        assert code == 200  # proxy-internal name would be refused without ingress
        code, _ = _get(base + "/api/status", host="evil.example.com")
        assert code == 200  # guard fully off — the proxy is the trust boundary
    finally:
        httpd.shutdown()


# ───────────────────────── v6: tracks, lyrics, static card, range, CORS ─────────


def _archive_with_tracks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(tmp_path / "downloads"))
    prof = tmp_path / "downloads" / "drinko"
    prof.mkdir(parents=True)
    # clip.file says "feat_", disk says "feat." — must still match.
    (prof / "drinko_Doomsday (feat. Yawgmoth).m4a").write_bytes(b"0123456789" * 10)
    (prof / "drinko_No File Field.m4a").write_bytes(b"x" * 20)
    (prof / "drinko_Doomsday (feat. Yawgmoth).mp4").write_bytes(b"video")
    os_time = 1_759_000_000
    import os

    os.utime(prof / "drinko_No File Field.m4a", (os_time, os_time))
    (tmp_path / ".blocked").write_text(
        "# note\n7c68eb8d-4bad-43d5-8d37-8be1fd48fb58\n", encoding="utf-8"
    )
    genres_dir = tmp_path / "suno-genres"
    (genres_dir / "lyrics").mkdir(parents=True)
    clips = [
        {
            "id": "aaaaaaaa-0001",
            "title": "Doomsday (feat. Yawgmoth)",
            "file": "drinko_Doomsday (feat_ Yawgmoth).m4a",
            "style": "s",
            "lyrics": "from clips",
        },
        {
            "id": "aaaaaaaa-0002",
            "title": "No File Field",
            "style": "s",
            "created_at": "2026-09-01T10:00:00Z",
        },
        {"id": "aaaaaaaa-0003", "title": "Not Downloaded", "file": "drinko_Not Downloaded.m4a"},
    ]
    (genres_dir / "clips.json").write_text(json.dumps(clips), encoding="utf-8")
    (genres_dir / "genres.json").write_text(
        json.dumps({"categories": [], "assignments": {"aaaaaaaa-0001": "X", "orphan": "X"}}),
        encoding="utf-8",
    )
    (genres_dir / "lyrics" / "aaaaaaaa-0002.txt").write_text("from file", encoding="utf-8")
    return genres_dir


def test_track_index_maps_audio_duration_and_created(tmp_path: Path, monkeypatch) -> None:
    genres_dir = _archive_with_tracks(tmp_path, monkeypatch)
    probed: list[str] = []

    def fake_probe(path: Path) -> float:
        probed.append(path.name)
        return 187.4

    index = webui.TrackIndex(tmp_path, genres_dir, prober=fake_probe)
    index.warm()
    payload = index.payload()
    t = payload["tracks"]
    assert payload["counts"] == {"archive": 3, "audio": 2, "blocked": 1}
    assert payload["warming"] is False
    one = t["aaaaaaaa-0001"]
    assert one["duration"] == 187.4
    assert one["audio"] == "/media/drinko/drinko_Doomsday%20%28feat.%20Yawgmoth%29.m4a"
    assert one["created_source"] == "file" and one["created"].endswith("+00:00")
    two = t["aaaaaaaa-0002"]  # matched by <profile>_<title>; Suno date wins
    assert two["audio"].endswith("drinko_No%20File%20Field.m4a")
    assert two["created"] == "2026-09-01T10:00:00Z" and two["created_source"] == "suno"
    three = t["aaaaaaaa-0003"]
    assert three == {"duration": None, "created": None, "created_source": None, "audio": None}
    assert all(name.endswith(".m4a") for name in probed)  # video twins never probed
    index.warm()
    assert len(probed) == 2  # cached by (path, mtime, size)


def test_track_index_reports_warming_until_probed(tmp_path: Path, monkeypatch) -> None:
    genres_dir = _archive_with_tracks(tmp_path, monkeypatch)
    gate = threading.Event()

    def slow_probe(path: Path) -> float:
        gate.wait(5)
        return 1.0

    index = webui.TrackIndex(tmp_path, genres_dir, prober=slow_probe)
    first = index.payload()
    assert first["warming"] is True
    assert first["tracks"]["aaaaaaaa-0001"]["duration"] is None
    gate.set()
    index._warming.join(5)
    assert index.payload()["tracks"]["aaaaaaaa-0001"]["duration"] == 1.0


def test_lyrics_payload_file_then_clips_and_validates_ids(tmp_path: Path, monkeypatch) -> None:
    genres_dir = _archive_with_tracks(tmp_path, monkeypatch)
    out = webui._lyrics_payload(genres_dir, "aaaaaaaa-0001,aaaaaaaa-0002,../../etc/passwd,zzz")
    assert out == {"lyrics": {"aaaaaaaa-0002": "from file", "aaaaaaaa-0001": "from clips"}}
    assert webui._lyrics_payload(None, "aaaaaaaa-0001") == {"lyrics": {}}


def test_parse_range() -> None:
    assert webui._parse_range(None, 100) is None
    assert webui._parse_range("bytes=0-9", 100) == (0, 9)
    assert webui._parse_range("bytes=90-", 100) == (90, 99)
    assert webui._parse_range("bytes=-10", 100) == (90, 99)
    assert webui._parse_range("bytes=50-500", 100) == (50, 99)
    assert webui._parse_range("bytes=200-300", 100) == (-1, -1)
    assert webui._parse_range("bytes=0-1,5-6", 100) is None


def test_v6_endpoints_over_http(tmp_path: Path, monkeypatch) -> None:
    genres_dir = _archive_with_tracks(tmp_path, monkeypatch)
    index = webui.TrackIndex(tmp_path, genres_dir, prober=lambda p: 42.0)
    index.warm()
    manager = RunManager(tmp_path)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _make_handler(
            tmp_path,
            manager,
            genres_dir=genres_dir,
            cors_origins=frozenset({"http://ha.example.lan:8123"}),
            tracks=index,
        ),
    )
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, body = _get(base + "/api/tracks")
        assert code == 200 and body["tracks"]["aaaaaaaa-0001"]["duration"] == 42.0
        code, body = _get(base + "/api/library")
        assert body["counts"] == {"archive": 3, "classified": 2, "blocked": 1}
        code, body = _get(base + "/api/lyrics?ids=aaaaaaaa-0002")
        assert code == 200 and body["lyrics"]["aaaaaaaa-0002"] == "from file"

        # The shared genre card is served (query string ignored) and mounted read-only.
        r = httpx.get(base + "/static/suno-genre-card.js?v=9", timeout=10.0)
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
        assert "customElements" in r.text
        svg = httpx.get(base + "/static/trackie-dacks.svg?v=1", timeout=10.0)
        assert svg.status_code == 200
        assert svg.headers["content-type"] == "image/svg+xml"
        assert svg.headers["x-content-type-options"] == "nosniff"
        assert b"<svg " in svg.content and b"prefers-reduced-motion" in svg.content
        hero = httpx.get(base + "/static/trackie-assistant-logo.webp?v=1", timeout=10.0)
        assert hero.status_code == 200
        assert hero.headers["content-type"] == "image/webp"
        assert hero.headers["x-content-type-options"] == "nosniff"
        assert (
            hero.content
            == (
                Path(__file__).resolve().parents[1] / "docs/trackie-assistant-logo.webp"
            ).read_bytes()
        )
        assert b'<title id="trackie-title">Trackie Dacks</title>' in svg.content
        r = httpx.get(base + "/static/suno-genre-core.js", timeout=10.0)
        assert r.status_code == 200 and "honestCounts" in r.text
        assert httpx.get(base + "/static/../webui.py", timeout=10.0).status_code == 404
        assert httpx.get(base + "/static/other.js", timeout=10.0).status_code == 404
        code, page = _get(base + "/")
        assert "<suno-genre-card" in page and "standalone:true" in page
        assert 'src="static/trackie-assistant-logo.webp?v=1"' in page
        assert "<h1>Trackie Assistant</h1>" in page
        assert "Could not reach the Trackie Assistant server." in page
        assert "Could not reach the suno-dl server." not in page

        # Range requests for seeking.
        body_audio = _get(base + "/api/tracks")[1]["tracks"]["aaaaaaaa-0001"]["audio"]
        assert body_audio
        audio = body_audio
        r = httpx.get(base + audio, headers={"Range": "bytes=10-19"}, timeout=10.0)
        assert r.status_code == 206 and r.content == b"0123456789"
        assert r.headers["content-range"] == "bytes 10-19/100"
        assert r.headers["accept-ranges"] == "bytes"
        r = httpx.get(base + audio, headers={"Range": "bytes=500-"}, timeout=10.0)
        assert r.status_code == 416
        r = httpx.get(base + audio, timeout=10.0)
        assert r.status_code == 200 and len(r.content) == 100

        # CORS: only the allow-listed origin is echoed, never '*'.
        r = httpx.get(
            base + "/api/tracks", headers={"Origin": "http://ha.example.lan:8123"}, timeout=10.0
        )
        assert r.headers.get("access-control-allow-origin") == "http://ha.example.lan:8123"
        r = httpx.get(base + "/api/tracks", headers={"Origin": "http://evil.test"}, timeout=10.0)
        assert "access-control-allow-origin" not in r.headers
        r = httpx.get(base + "/api/tracks", timeout=10.0)
        assert "access-control-allow-origin" not in r.headers
    finally:
        httpd.shutdown()


def test_static_card_matches_bundled_files() -> None:
    for name in ("suno-genre-card.js", "suno-genre-core.js"):
        assert webui._static_file(name), name


def test_genres_dir_has_no_machine_specific_default(monkeypatch, tmp_path: Path) -> None:
    from suno_dl.webui import _genres_dir

    monkeypatch.delenv("SUNO_GENRES_DIR", raising=False)
    assert _genres_dir(None) is None
    monkeypatch.setenv("SUNO_GENRES_DIR", str(tmp_path))
    assert _genres_dir(None) == tmp_path.resolve()


# ───────────────────────────── startup & client-error ──────────────────────


def test_webui_cli_loads_selected_archive_not_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / ".env").write_text("SUNO_WATCH=@archive\n")
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("SUNO_WATCH=@wrong\n")
    monkeypatch.chdir(other)
    monkeypatch.setenv("SUNO_WATCH", "")
    monkeypatch.delenv("SUNO_WATCH", raising=False)
    monkeypatch.setattr(webui.TrackIndex, "warm_async", lambda self: None)

    class FakeServer:
        server_address = ("127.0.0.1", 8760)

        def __init__(self, *args, **kwargs):
            pass

        def serve_forever(self):
            return None

        def server_close(self):
            pass

    monkeypatch.setattr(webui, "ThreadingHTTPServer", FakeServer)
    result = CliRunner().invoke(webui.webui_cli, ["--archive", str(archive)])
    assert result.exit_code == 0, result.output
    assert webui.os.environ["SUNO_WATCH"] == "@archive"


def test_webui_cli_refuses_an_archive_without_env(tmp_path: Path, monkeypatch) -> None:
    """A wrong --archive is a one-line fix, not a traceback or a blank dashboard."""
    from click.testing import CliRunner

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(webui.webui_cli, ["--archive", str(tmp_path), "--port", "0"])

    assert result.exit_code == 1
    assert "no .env" in result.output


def test_webui_cli_reports_a_port_already_in_use(tmp_path: Path) -> None:
    from click.testing import CliRunner

    (tmp_path / ".env").write_text("", encoding="utf-8")
    occupant = socket.socket()
    occupant.bind(("127.0.0.1", 0))
    occupant.listen(1)
    port = occupant.getsockname()[1]
    try:
        result = CliRunner().invoke(
            webui.webui_cli, ["--archive", str(tmp_path), "--port", str(port)]
        )
    finally:
        occupant.close()

    assert result.exit_code == 1
    assert "cannot bind" in result.output


def test_client_error_report_over_the_size_cap_is_rejected(server) -> None:
    base, _ = server
    response = httpx.post(base + "/api/client-error", json={"x": "y" * 9000}, timeout=10.0)
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_client_error_report_is_clamped_and_never_echoed(server) -> None:
    base, _ = server
    response = httpx.post(
        base + "/api/client-error",
        json={"kind": "split", "cause": "boom", "detail": "z" * 5000},
        timeout=10.0,
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_unreviewed_video_retention_is_refused_before_starting_a_run(server):
    base, manager = server
    code, body = _post(base + "/api/sync", {"dry_run": "0", "videos": "1"})
    assert code == 503 and body["code"] == "tagging_unavailable"
    assert not manager.busy and manager.list() == []


def test_real_windows_sync_is_definitively_blocked_before_other_gates(server, monkeypatch):
    from suno_dl.download_intent import WindowsArchiveWriteUnavailable

    def windows_block():
        raise WindowsArchiveWriteUnavailable("fixture")

    monkeypatch.setattr(webui, "raise_if_unsupported_archive_write", windows_block)
    base, manager = server
    code, body = _post(base + "/api/sync", {"dry_run": "0"})
    assert code == 503
    assert body["code"] == "windows_archive_unavailable"
    assert "Windows archive writes are unavailable" in body["error"]
    # A read-only preview can still be started through the offline test command.
    code, body = _post(base + "/api/sync", {"dry_run": "1"})
    assert code == 200 and "id" in body


def test_sync_command_keeps_explicit_source_scope_and_disables_video_fallback():
    from suno_dl.webui import _sync_command

    sources = ("https://suno.com/playlist/11111111-1111-4111-8111-111111111111",)
    command = _sync_command(False, sources=sources, extract_audio=False)
    assert "--no-extract-audio" in command and "--extract-audio" not in command
    assert command[command.index("--watch") + 1] == sources[0]
    assert "--no-videos" in command and "--keep-video" not in command
