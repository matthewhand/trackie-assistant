"""Structured errors + progress for the web UI's long actions (#95)."""

from __future__ import annotations

import logging
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from suno_dl.opstatus import (
    CODES,
    ApiError,
    classify_exception,
    classify_run_failure,
    error_body,
    new_request_id,
    parse_progress,
    redact,
)
from suno_dl.webui import RunManager, _make_handler

# ───────────────────────────── error mapping ─────────────────────────────


@pytest.mark.parametrize(
    ("exc", "code", "status"),
    [
        (TimeoutError("deadline"), "ai_timeout", 504),
        (RuntimeError("ReadTimeout: timed out after 30s"), "ai_timeout", 504),
        (PermissionError(13, "Permission denied", "/mnt/data/genres.json"), "file_permission", 403),
        (OSError(28, "No space left on device"), "file_permission", 403),
        (FileNotFoundError(2, "No such file", "genres.json"), "file_permission", 403),
        (ConnectionRefusedError("connection refused"), "ai_unreachable", 502),
        (RuntimeError("InternalServerError: Connection error."), "ai_unreachable", 502),
        (RuntimeError("No deployments available for selected model"), "ai_unreachable", 502),
        (RuntimeError("Bonsai returned a junk/empty reply"), "ai_empty_reply", 502),
        (ValueError("did not return JSON"), "ai_invalid_reply", 502),
        (KeyError("surprise"), "unexpected", 500),
    ],
)
def test_classify_exception_maps_cause_and_status(exc, code, status) -> None:
    err = classify_exception(exc)
    assert err.code == code
    assert err.status == status
    assert err.cause_class == CODES[code][1]
    assert err.message and err.message[0].isupper()  # plain words, not a repr


def test_every_code_has_a_status_and_a_label() -> None:
    for code, (status, label) in CODES.items():
        assert 400 <= status <= 599, code
        assert label, code
    assert ApiError("nonsense", "x").code == "unexpected"


def test_api_error_passes_through_unchanged() -> None:
    err = ApiError("busy", "Another run is active.", "run abc")
    assert classify_exception(err) is err
    assert err.status == 409


def test_error_body_envelope_is_complete_and_redacted() -> None:
    err = ApiError(
        "ai_unreachable",
        "Could not reach it with Bearer abcdef1234567890abcd",
        "GET https://x/v1?api_key="
        + "sk-live-"
        + "1234567890abcdef"
        + " token=hunter2hunter2 failed",
    )
    body = error_body(err, "deadbeef")
    assert set(body) >= {"code", "message", "detail", "request_id", "time", "error", "cause_class"}
    assert body["request_id"] == "deadbeef"
    assert body["error"] == body["message"]
    blob = str(body)
    for secret in ("abcdef1234567890abcd", "sk-live-1234567890abcdef", "hunter2hunter2"):
        assert secret not in blob
    assert "[redacted" in blob


def test_detail_is_capped() -> None:
    body = error_body(ApiError("unexpected", "x", "y" * 50_000))
    assert len(body["detail"]) <= 1200


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcdefghijklmnop1234",
        "cookie=__client=abcdef123456; other",
        "password: correct-horse-battery",
        "https://host/path?token=abc123secret&x=1",
        ".".join(("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "abcdefghijk")),
        "github_pat_11ABCDEFG0123456789abcdef",
    ],
)
def test_redact_masks_secrets(text: str) -> None:
    out = redact(text)
    assert "[redacted" in out
    for leaked in (
        "abcdefghijklmnop1234",
        "abcdef123456",
        "correct-horse",
        "abc123secret",
        "0123456789abcdef",
    ):
        assert leaked not in out


def test_request_ids_are_short_and_unique() -> None:
    ids = {new_request_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 8 and int(i, 16) >= 0 for i in ids)


# ───────────────────────────── progress parsing ─────────────────────────────


@pytest.mark.parametrize(
    ("lines", "done", "total"),
    [
        (["start", "[18/42] downloading x"], 18, 42),
        (["analysing 7 of 20 tracks"], 7, 20),
        (["12%|#####     | 5/42 [00:03<00:20]"], 5, 42),
        (["track 3/9 ok", "noise"], 3, 9),
    ],
)
def test_parse_progress_reads_latest_counts(lines, done, total) -> None:
    got = parse_progress(lines)
    assert got is not None
    assert (got["done"], got["total"]) == (done, total)


def test_parse_progress_ignores_nonsense() -> None:
    assert parse_progress([]) is None
    assert parse_progress(["no numbers here"]) is None
    assert parse_progress(["9/3 impossible"]) is None
    assert parse_progress(["0/0"]) is None


def test_classify_run_failure_uses_exit_and_tail() -> None:
    err = classify_run_failure(2, ["a", "OSError: [Errno 13] Permission denied: '/x'"])
    assert err.code == "file_permission"
    err = classify_run_failure(1, ["boom"])
    assert err.code == "unexpected" and "exit code 1" in err.message
    assert classify_run_failure(-15, [], cancelled=True).code == "cancelled"


# ───────────────────────────── HTTP behaviour ─────────────────────────────


@pytest.fixture
def server(tmp_path: Path):
    manager = RunManager(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(tmp_path, manager))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", manager
    httpd.shutdown()


def _assert_envelope(body: dict, code: str) -> None:
    assert body["code"] == code
    assert body["message"] and body["request_id"] and body["time"]
    assert body["error"] == body["message"]


def test_errors_use_envelope_and_correct_status(server) -> None:
    base, _manager = server
    r = httpx.post(base + "/api/recommend", data={"candidates": "  "}, timeout=10)
    assert r.status_code == 400
    _assert_envelope(r.json(), "bad_request")
    r = httpx.get(base + "/api/nope", timeout=10)
    assert r.status_code == 404
    _assert_envelope(r.json(), "not_found")
    r = httpx.get(base + "/api/status", headers={"Host": "evil.example"}, timeout=10)
    assert r.status_code == 403
    _assert_envelope(r.json(), "forbidden_host")


def test_busy_returns_409_envelope(server, monkeypatch) -> None:
    base, manager = server
    run, err = manager.start("hold", [sys.executable, "-c", "import time; time.sleep(30)"])
    assert run and not err
    try:
        r = httpx.post(base + "/api/recommend", data={"candidates": "@a"}, timeout=10)
        assert r.status_code == 409
        _assert_envelope(r.json(), "busy")
    finally:
        manager.cancel(run.id)


def test_unhandled_exception_becomes_500_envelope_and_is_logged(
    server, monkeypatch, caplog
) -> None:
    base, _manager = server
    import suno_dl.webui as webui

    def boom(*_a, **_k):
        raise RuntimeError("secret token=abc123supersecret exploded")

    monkeypatch.setattr(webui, "_compute_status", boom)
    with caplog.at_level(logging.ERROR, logger="suno_dl.webui"):
        r = httpx.get(base + "/api/status", timeout=10)
    assert r.status_code == 500
    body = r.json()
    _assert_envelope(body, "unexpected")
    assert "abc123supersecret" not in r.text
    log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert body["request_id"] in log
    assert "exploded" in log  # the full cause is logged...
    assert "abc123supersecret" not in log  # ...but never the secret


def test_run_snapshot_exposes_progress_and_failure_cause(server) -> None:
    base, manager = server
    script = (
        "import sys,time\n"
        "print('[3/10] fetching', flush=True)\n"
        "time.sleep(0.4)\n"
        "print(\"OSError: [Errno 13] Permission denied: '/mnt/x'\", flush=True)\n"
        "sys.exit(2)\n"
    )
    run, _ = manager.start("probe", [sys.executable, "-c", script])
    assert run
    deadline = time.time() + 10
    seen_progress = None
    while time.time() < deadline:
        snap = httpx.get(base + "/api/runs", timeout=10).json()["runs"][0]
        if snap["progress"] and not seen_progress:
            seen_progress = snap["progress"]
        if snap["state"] != "running":
            break
        time.sleep(0.05)
    assert seen_progress and (seen_progress["done"], seen_progress["total"]) == (3, 10)
    assert snap["state"] == "failed"
    _assert_envelope(snap["error"], "file_permission")
    assert snap["error"]["request_id"] == snap["request_id"]


def test_cancel_endpoint_stops_a_run(server) -> None:
    base, manager = server
    run, _ = manager.start("hold", [sys.executable, "-c", "import time; time.sleep(60)"])
    assert run
    r = httpx.post(base + f"/api/runs/{run.id}/cancel", timeout=10)
    assert r.status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline and run.returncode is None:
        time.sleep(0.05)
    assert run.returncode is not None
    snap = httpx.get(base + "/api/runs", timeout=10).json()["runs"][0]
    assert snap["state"] == "cancelled"
    assert snap["error"]["code"] == "cancelled"
    # Finished runs cannot be cancelled again; unknown ids are 404.
    assert httpx.post(base + f"/api/runs/{run.id}/cancel", timeout=10).status_code == 400
    assert httpx.post(base + "/api/runs/00000000/cancel", timeout=10).status_code == 404


def test_client_error_report_is_logged_without_secrets(server, caplog) -> None:
    base, _manager = server
    report = {
        "request_id": "abcd1234",
        "kind": "split",
        "code": "ai_timeout",
        "cause": "AI service timeout",
        "detail": "OpTimeoutError: no answer; Authorization: Bearer abcdefghijklmnop1234",
    }
    with caplog.at_level(logging.ERROR, logger="suno_dl.webui"):
        r = httpx.post(base + "/api/client-error", json=report, timeout=10)
    assert r.status_code == 200
    log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "abcd1234" in log and "ai_timeout" in log and "OpTimeoutError" in log
    assert "abcdefghijklmnop1234" not in log
    big = httpx.post(base + "/api/client-error", content=b"x" * 20000, timeout=10)
    assert big.status_code == 400
