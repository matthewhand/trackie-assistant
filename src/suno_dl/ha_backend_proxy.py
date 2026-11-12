"""Home Assistant ingress client for a separately managed Trackie backend.

No downloader, local archive or scheduling loop is started in this mode.
Credentials stay on the server; the backend certificate is pinned before
an authenticated HTTP request is sent.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import http.client
import json
import re
import socket
import ssl
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

_MAX_BODY = 65536
_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "etag",
        "last-modified",
        "cache-control",
        "content-encoding",
    }
)


@dataclass(frozen=True)
class Backend:
    host: str
    port: int
    origin: str
    frontend_origin: str
    certificate_sha256: str
    authorization: str = field(repr=False)

    @classmethod
    def from_options(cls, options: dict) -> Backend:
        if not isinstance(options, dict):
            raise ValueError("Invalid backend options")

        def origin(key: str, schemes: set[str]) -> str:
            value = options.get(key)
            if not isinstance(value, str) or any(ord(c) < 33 for c in value):
                raise ValueError("Configure a valid backend and Home Assistant origin")
            parsed = urlsplit(value)
            if (
                parsed.scheme not in schemes
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Backend and frontend origins must not contain paths or credentials"
                )
            _ = parsed.port
            return value.rstrip("/")

        backend_origin = origin("backend_url", {"https"})
        frontend = origin("frontend_origin", {"http", "https"})
        parsed = urlsplit(backend_origin)
        pin = options.get("backend_certificate_sha256", "")
        user = options.get("backend_username", "")
        password = options.get("backend_password", "")
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", pin):
            raise ValueError("Configure the backend certificate SHA256 fingerprint")
        if (
            not isinstance(user, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user)
            or not isinstance(password, str)
            or not 1 <= len(password) <= 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in password)
        ):
            raise ValueError("Configure backend credentials")
        authorization = "Basic " + base64.b64encode((user + ":" + password).encode()).decode()
        return cls(
            str(parsed.hostname),
            parsed.port or 443,
            backend_origin,
            frontend,
            pin.lower(),
            authorization,
        )


def safe_path(target: str) -> bool:
    if not target.startswith("/") or target.startswith("//") or len(target) > 8192:
        return False
    decoded = unquote(urlsplit(target).path)
    return not (
        "\\" in decoded
        or "//" in decoded
        or any(ord(c) < 32 or ord(c) == 127 for c in target + decoded)
        or any(part in (".", "..") for part in decoded.split("/"))
    )


def make_handler(
    backend: Backend, *, trusted_peer: str | None = None
) -> type[BaseHTTPRequestHandler]:
    peer = trusted_peer or socket.gethostbyname("supervisor")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def fail(self, status: int, message: str) -> None:
            body = json.dumps({"error": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def forward(self) -> None:
            if self.client_address[0] != peer:
                self.fail(403, "Home Assistant ingress proxy required")
                return
            if (
                not safe_path(self.path)
                or self.headers.get("Transfer-Encoding")
                or self.headers.get("Expect")
            ):
                self.fail(400, "Invalid request path or transfer framing")
                return
            if self.command == "POST" and (
                self.headers.get("Origin") != backend.frontend_origin
                or self.headers.get("Sec-Fetch-Site") == "cross-site"
            ):
                self.fail(403, "Home Assistant same-origin request required")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) > 1:
                self.fail(400, "Ambiguous request framing")
                return
            try:
                length = int(lengths[0]) if lengths else 0
            except ValueError:
                self.fail(400, "Invalid request length")
                return
            if not 0 <= length <= _MAX_BODY or (self.command != "POST" and length):
                self.fail(413, "Request body too large or unsupported")
                return
            if self.command == "POST" and not (
                urlsplit(self.path).path
                in {
                    "/api/schedule",
                    "/api/sync",
                    "/api/recommend",
                    "/api/client-error",
                    "/api/blocked",
                }
                or re.fullmatch(r"/api/runs/[0-9a-f]{8}/cancel", urlsplit(self.path).path)
            ):
                self.fail(403, "Unsupported backend operation")
                return
            self.connection.settimeout(15)
            try:
                body = self.rfile.read(length) if length else None
            except (TimeoutError, OSError):
                self.fail(408, "Request body timed out")
                return
            if body is not None and len(body) != length:
                self.fail(400, "Incomplete request body")
                return
            # The pin authenticates a private/self-signed TLS server. Never
            # send Authorization until its actual DER certificate matches.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            connection = http.client.HTTPSConnection(
                backend.host, backend.port, timeout=30, context=context
            )
            sent = False
            try:
                connection.connect()
                if connection.sock is None:
                    raise OSError("TLS connection unavailable")
                certificate = connection.sock.getpeercert(binary_form=True)
                if not certificate:
                    raise OSError("Backend certificate unavailable")
                fingerprint = hashlib.sha256(certificate).hexdigest()
                if not hmac.compare_digest(fingerprint, backend.certificate_sha256):
                    raise OSError("Backend certificate does not match its pin")
                headers = {
                    "Authorization": backend.authorization,
                    "Connection": "close",
                    "Host": urlsplit(backend.origin).netloc,
                }
                for name in ("Content-Type", "Range", "Accept"):
                    if value := self.headers.get(name):
                        headers[name] = value
                if self.command == "POST":
                    headers["Origin"] = backend.origin
                    headers["Content-Length"] = str(length)
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                if response.status == 401:
                    self.fail(502, "Backend authentication failed; check add-on settings")
                    return
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.lower() in _HEADERS:
                        self.send_header(name, value)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                sent = True
                if self.command != "HEAD":
                    while chunk := response.read(65536):
                        self.wfile.write(chunk)
                self.close_connection = True
            except (OSError, http.client.HTTPException):
                if not sent:
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        self.fail(
                            502,
                            "Trackie backend unavailable; check its address and certificate pin",
                        )
            finally:
                connection.close()

        def do_GET(self) -> None:  # noqa: N802
            self.forward()

        def do_HEAD(self) -> None:  # noqa: N802
            self.forward()

        def do_POST(self) -> None:  # noqa: N802
            self.forward()

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--options", type=Path, default=Path("/data/options.json"))
    args = parser.parse_args()
    try:
        if args.options.is_symlink() or args.options.stat().st_size > 65536:
            raise ValueError("Unsafe options file")
        options = json.loads(args.options.read_text())
        backend = Backend.from_options(options)
        handler = make_handler(backend)
    except (ValueError, OSError):
        raise SystemExit(
            "Cannot configure remote backend; check the add-on connection settings"
        ) from None
    server = ThreadingHTTPServer(("0.0.0.0", 8760), handler)  # noqa: S104 - ingress peer enforced
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
