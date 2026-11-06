"""Local health probes never follow redirects or consult proxy destinations."""

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


def load_probe():
    path = Path(__file__).parents[1] / "deploy/ct107/healthcheck.py"
    spec = importlib.util.spec_from_file_location("health_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("status", "expected"), [(403, 0), (302, 0), (503, 1)])
def test_health_probe_stays_on_loopback(status, expected, monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(status)
            self.send_header("Location", "https://example.invalid/must-not-follow")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TRACKIE_GATEWAY_PORT", str(server.server_address[1]))
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid:9")
    monkeypatch.setenv("http_proxy", "http://example.invalid:9")
    try:
        assert load_probe().probe_gateway() == expected
        assert seen == ["/api/status"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("port", ["0", "-1", "65536", "8760@evil.invalid", "file:///tmp/x"])
def test_invalid_gateway_port_fails_before_connect(port, monkeypatch):
    monkeypatch.setenv("TRACKIE_GATEWAY_PORT", port)
    assert load_probe().probe_gateway() == 1
