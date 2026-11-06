"""Shared fixtures: mock HTTP transports for offline provider tests.

Every test that previously monkeypatched ``httpx.Client.__init__`` and
``httpx.AsyncClient.__init__`` by hand uses the :func:`mock_http` fixture
instead — one place to fix if httpx's constructor surface ever changes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

Handler = Callable[[httpx.Request], httpx.Response]


def _install_mock_transport(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    """Route every httpx client (sync and async) through ``handler``."""

    real_async_init = httpx.AsyncClient.__init__
    real_sync_init = httpx.Client.__init__

    def patched_async_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        real_async_init(self, *args, **kwargs)

    def patched_sync_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        real_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_async_init)
    monkeypatch.setattr(httpx.Client, "__init__", patched_sync_init)


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], None]:
    """Install a MockTransport handler for both sync and async httpx clients."""

    return lambda handler: _install_mock_transport(monkeypatch, handler)


@pytest.fixture
def disable_producer_and_tagging_guards_for_legacy_transfer_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt in to both disabled guards solely for legacy transfer coverage."""
    import inspect

    from suno_dl import cli, downloader, producer_lookup, tagging

    # These tests deliberately cover the quarantined transfer implementation,
    # including its historical defaults. New public defaults are covered by
    # the unguarded production Linux tests, not by this explicit legacy fixture.
    monkeypatch.setattr(
        inspect.unwrap(downloader.download_tracks),
        "__defaults__",
        downloader._download_tracks_legacy.__defaults__,
    )

    monkeypatch.setattr(tagging, "_embedded_tag_write_guard", lambda: None)
    monkeypatch.setattr(downloader, "_require_trusted_framing", lambda: None)
    monkeypatch.setattr(cli, "_require_trusted_framing", lambda: None)
    monkeypatch.setattr(producer_lookup, "_require_trusted_framing", lambda: None)
    monkeypatch.setattr(producer_lookup, "refuse_filename_attribution", lambda: None)

    async def legacy_transfer_inputs(tracks, fetcher):
        # Transfer-only tests deliberately isolate provenance. Production
        # verification is exercised separately without this fixture.
        return tracks

    monkeypatch.setattr(downloader, "verify_track_producers", legacy_transfer_inputs)
    monkeypatch.setattr(downloader, "require_audio_sync", lambda **options: None)
    monkeypatch.setattr(cli, "require_audio_sync", lambda **options: None)
    monkeypatch.setattr(downloader, "_execute_downloads", downloader._download_tracks_legacy)

    async def legacy_playlist_source(fetcher, url):
        return cli.parse_playlist(await fetcher.fetch_html(url))

    monkeypatch.setattr(cli, "_source_playlist_tracks", legacy_playlist_source)


@pytest.fixture(autouse=True)
def offline_http_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests may use local HTTP servers and mocked providers, never Suno."""
    original_sync = httpx.Client.send
    original_async = httpx.AsyncClient.send

    def require_offline(client, request):
        transport = client._transport_for_url(request.url)
        if isinstance(
            transport, (httpx.HTTPTransport, httpx.AsyncHTTPTransport)
        ) and request.url.host not in {"localhost", "127.0.0.1", "::1"}:
            pytest.fail("Offline test attempted an unmocked external HTTP request")

    def sync_send(client, request, **kwargs):
        require_offline(client, request)
        return original_sync(client, request, **kwargs)

    async def async_send(client, request, **kwargs):
        require_offline(client, request)
        return await original_async(client, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", sync_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
