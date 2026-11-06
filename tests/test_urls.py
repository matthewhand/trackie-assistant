"""Tests for the URL allow-list and redaction (security-critical)."""

from __future__ import annotations

import pytest

from suno_dl.urls import (
    cdn_cover_url,
    cdn_guess_urls,
    is_allowed_media_url,
    is_uuid,
    redact_url,
    song_page_url,
)

CLIP = "a1b2c3d4-1111-4111-8111-111111111111"


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn1.suno.ai/x.m4a",
        "https://cdn2.suno.ai/image_x.jpeg",
        "https://studio-api.prod.suno.com/x",
        "https://d2lwuy8qc234o3.cloudfront.net/1/clip/x.m4a?sig=abc",
        "https://cdn-o.suno.com/sil-100.mp3",
    ],
)
def test_allowed_hosts(url: str) -> None:
    assert is_allowed_media_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata, http
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata, https
        "https://localhost/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://10.0.0.1/x",  # RFC1918 on purpose: private ranges must be rejected (SSRF)
        "https://evil-suno.ai.attacker.com/x",  # suffix-spoof attempt
        "https://user:pass@suno.ai/x",  # embedded credentials
        "http://cdn1.suno.ai/x.m4a",  # non-https
        "file:///etc/passwd",
        "ftp://suno.ai/x",
        "https://suno.ai.evil.com/x",
        "",
        "not a url",
    ],
)
def test_rejected_urls(url: str) -> None:
    assert not is_allowed_media_url(url)


def test_redact_url_strips_query() -> None:
    signed = "https://cdn.example/x.m4a?sig=SECRET&kid=1"
    out = redact_url(signed)
    assert "SECRET" not in out
    assert "sig" not in out
    assert out == "https://cdn.example/x.m4a?...redacted"


def test_redact_url_plain() -> None:
    assert redact_url("https://cdn.example/x.m4a") == "https://cdn.example/x.m4a"


def test_is_uuid() -> None:
    assert is_uuid(CLIP)
    assert is_uuid(CLIP.upper())
    assert not is_uuid("not-a-uuid")
    assert not is_uuid(CLIP + "x")


def test_url_builders() -> None:
    assert song_page_url(CLIP) == f"https://suno.com/song/{CLIP}"
    assert cdn_cover_url(CLIP).endswith(f"image_{CLIP}.jpeg")
    assert all(CLIP in u for u in cdn_guess_urls(CLIP))
