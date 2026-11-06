"""Gateway access policy (#162): direct-peer CIDRs, strict auth, write denial.

Every case binds a throwaway server on 127.0.0.1. "Untrusted client" is
simulated with a trusted CIDR that cannot contain the loopback peer, so the
decision under test is the real socket peer, not a mocked address.
"""

from __future__ import annotations

import base64
import ipaddress
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from suno_dl.webui import GatewayAuth, RunManager, _make_handler, webui_cli


def _auth(header: tuple[str, str] | None) -> dict[str, str]:
    if header is None:
        return {}
    raw = f"{header[0]}:{header[1]}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture
def serve(tmp_path: Path):
    (tmp_path / ".env").write_text("", encoding="utf-8")
    servers: list[ThreadingHTTPServer] = []

    def start(auth: GatewayAuth) -> str:
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _make_handler(tmp_path, RunManager(tmp_path), auth=auth),
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in servers:
        httpd.shutdown()


def _get(base: str, path: str = "/api/status", headers: dict[str, str] | None = None):
    assert base.startswith("http://127.0.0.1:")
    request = urllib.request.Request(base + path, headers=headers or {})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - local server
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _post(base: str, path: str = "/api/nope", headers: dict[str, str] | None = None):
    assert base.startswith("http://127.0.0.1:")
    request = urllib.request.Request(  # noqa: S310
        base + path, data=b"x=1", method="POST", headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - local server
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_default_policy_is_unchanged(serve) -> None:
    base = serve(GatewayAuth())
    assert _get(base)[0] == 200


def test_require_auth_challenges_reads_and_writes(serve) -> None:
    base = serve(GatewayAuth(require_auth=True, credentials=("u", "p")))
    status, headers, _ = _get(base)
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Basic ")
    assert _get(base, headers=_auth(("u", "wrong")))[0] == 401
    assert _get(base, headers=_auth(("u", "p")))[0] == 200
    assert _post(base, headers=_auth(("u", "p")))[0] != 401
    assert _post(base)[0] == 401


def test_require_auth_wins_over_trusted_cidr(serve) -> None:
    base = serve(
        GatewayAuth(
            require_auth=True,
            lan_readonly=True,
            trusted_cidrs=(ipaddress.ip_network("127.0.0.0/8"),),
            credentials=("u", "p"),
        )
    )
    assert _get(base)[0] == 401


def test_lan_readonly_allows_trusted_peer_to_read_but_never_write(serve) -> None:
    base = serve(
        GatewayAuth(
            lan_readonly=True,
            trusted_cidrs=(ipaddress.ip_network("127.0.0.1/32"),),
        )
    )
    assert _get(base)[0] == 200
    status, _, _ = _post(base)
    assert status == 403


def test_untrusted_peer_is_denied_and_forwarded_headers_are_ignored(serve) -> None:
    base = serve(
        GatewayAuth(
            lan_readonly=True,
            # Documentation range (RFC 5737), never a real deployment subnet.
            trusted_cidrs=(ipaddress.ip_network("203.0.113.0/24"),),
        )
    )
    spoof = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Forwarded": "for=127.0.0.1"}
    assert _get(base, headers=spoof)[0] == 403


def test_writes_require_credentials_when_configured_even_from_loopback(serve) -> None:
    base = serve(GatewayAuth(credentials=("u", "p")))
    assert _get(base)[0] == 200  # reads stay open unless a flag says otherwise
    assert _post(base)[0] == 401
    assert _post(base, headers=_auth(("u", "p")))[0] != 401


def test_malformed_basic_header_never_matches() -> None:
    auth = GatewayAuth(require_auth=True, credentials=("u", "p"))
    assert not auth.credentials_match("Basic !!!not-base64!!!")
    assert not auth.credentials_match("Basic " + base64.b64encode(b"up").decode())
    assert not auth.credentials_match("Bearer dTpw")
    assert auth.credentials_match("basic " + base64.b64encode(b"u:p").decode())


def test_require_auth_without_credentials_fails_to_start(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("TRACKIE_GATEWAY_REQUIRE_AUTH", "1")
    monkeypatch.delenv("TRACKIE_GATEWAY_USER", raising=False)
    monkeypatch.delenv("TRACKIE_GATEWAY_PASSWORD", raising=False)
    result = CliRunner().invoke(webui_cli, ["--archive", str(tmp_path)])
    assert result.exit_code != 0
    assert "refusing to start an open gateway" in result.output


def test_invalid_cidr_is_a_startup_error(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("TRACKIE_GATEWAY_TRUSTED_CIDRS", "not-a-cidr")
    result = CliRunner().invoke(webui_cli, ["--archive", str(tmp_path)])
    assert result.exit_code != 0
    assert "TRACKIE_GATEWAY_TRUSTED_CIDRS" in result.output


def test_ipv6_loopback_peer_network_matching() -> None:
    auth = GatewayAuth(trusted_cidrs=(ipaddress.ip_network("::1/128"),))
    assert auth.peer_networks("::1")
    assert auth.peer_networks("::ffff:127.0.0.1") is False
