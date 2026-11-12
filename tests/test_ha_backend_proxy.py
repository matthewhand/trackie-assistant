"""Ingress trust, pinned TLS ordering and absence of local downloading."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from suno_dl import ha_backend_proxy as proxy

CERT = b"synthetic backend certificate"


def options():
    return {
        "backend_url": "https://backend.example:8786",
        "frontend_origin": "https://ha.example",
        "backend_username": "trackie",
        "backend_password": "p" * 40,
        "backend_certificate_sha256": hashlib.sha256(CERT).hexdigest(),
    }


@contextlib.contextmanager
def serving(config, peer="127.0.0.1"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.make_handler(config, trusted_peer=peer))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def upstream(monkeypatch):
    calls = []

    class Response:
        status = 200

        def getheaders(self):
            return [("Content-Type", "application/json"), ("Set-Cookie", "must-not-forward")]

        def read(self, size):
            if hasattr(self, "read_once"):
                return b""
            self.read_once = True
            return b'{"available":true}'

    class Socket:
        def getpeercert(self, binary_form):
            assert binary_form
            calls.append("certificate")
            return CERT

    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = Socket()

        def connect(self):
            calls.append("connect")

        def request(self, method, path, body, headers):
            calls.append((method, path, body, headers))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(proxy.http.client, "HTTPSConnection", Connection)
    return calls


def request(port, method, target, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def test_valid_ingress_pins_before_authentication_and_forwards_same_backend(upstream):
    config = proxy.Backend.from_options(options())
    with serving(config) as port:
        status, _, headers = request(
            port,
            "POST",
            "/api/schedule",
            b"{}",
            {
                "Origin": "https://ha.example",
                "Content-Type": "application/json",
                "Authorization": "Bearer browser-value",
                "Cookie": "browser-session",
            },
        )
    assert status == 200 and "Set-Cookie" not in headers
    assert upstream[:2] == ["connect", "certificate"]
    forwarded = upstream[2][3]
    assert forwarded["Authorization"] == config.authorization
    assert forwarded["Origin"] == config.origin
    assert "Cookie" not in forwarded
    assert options()["backend_password"] not in repr(config)


def test_bad_pin_sends_no_authenticated_http_request(upstream):
    data = options()
    data["backend_certificate_sha256"] = "0" * 64
    with serving(proxy.Backend.from_options(data)) as port:
        status, body, _ = request(port, "GET", "/api/status")
    assert status == 502 and upstream == ["connect", "certificate"]
    assert options()["backend_password"].encode() not in body


@pytest.mark.parametrize(
    "peer,origin,path",
    [
        ("127.0.0.2", "https://ha.example", "/api/schedule"),
        ("127.0.0.1", "https://evil.example", "/api/schedule"),
        ("127.0.0.1", None, "/api/schedule"),
        ("127.0.0.1", "https://ha.example", "/../api/schedule"),
    ],
)
def test_untrusted_peer_origin_or_path_never_reaches_backend(upstream, peer, origin, path):
    headers = {"X-Forwarded-For": peer}
    if origin:
        headers["Origin"] = origin
    with serving(proxy.Backend.from_options(options()), peer) as port:
        status, _, _ = request(port, "POST", path, b"{}", headers)
    assert status in (400, 403) and upstream == []


def test_ambiguous_framing_is_rejected_before_reading_body(upstream):
    with serving(proxy.Backend.from_options(options())) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.putrequest("POST", "/api/schedule")
            connection.putheader("Origin", "https://ha.example")
            connection.putheader("Content-Length", "2")
            connection.putheader("Content-Length", "999")
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 400
            response.read()
        finally:
            connection.close()
    assert upstream == []


@pytest.mark.parametrize(
    "changes",
    [
        {"backend_url": "http://backend.example"},
        {"backend_url": "https://name:value@backend.example"},
        {"backend_url": "https://backend.example/path"},
        {"backend_certificate_sha256": ""},
    ],
)
def test_remote_settings_fail_closed(changes):
    with pytest.raises(ValueError):
        proxy.Backend.from_options(dict(options(), **changes))


def test_remote_entrypoint_does_not_create_local_archive_or_sync_loop(tmp_path):
    root = Path(__file__).resolve().parents[1]
    stub = tmp_path / "stub/suno_dl"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("")
    (stub / "ha_backend_proxy.py").write_text('print("remote proxy only")')
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps(dict(options(), backend_mode="remote", profiles=[])))
    source = (root / "addon/suno_dl/run.sh").read_text()
    archive, media = tmp_path / "archive", tmp_path / "music"
    source = source.replace("ARCHIVE=/data/archive", "ARCHIVE=" + str(archive))
    source = source.replace("MEDIA=/media/suno", "MEDIA=" + str(media))
    source = source.replace("OPTIONS=/data/options.json", "OPTIONS=" + str(opts))
    source = source.replace('open("/data/options.json")', 'open("' + str(opts) + '")')
    result = subprocess.run(
        ["bash"],
        input=source,
        capture_output=True,
        text=True,
        timeout=10,
        env=dict(
            os.environ,
            PYTHONPATH=str(stub.parent),
            PATH=str(Path(sys.executable).parent) + ":" + os.environ["PATH"],
        ),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "remote proxy only"
    assert not archive.exists() and not media.exists()
