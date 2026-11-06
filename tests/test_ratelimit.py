"""Tests for the per-host rate limiter (#17)."""

from __future__ import annotations

import asyncio
import time

import pytest

from suno_dl.ratelimit import HostRateLimiter


async def test_disabled_never_sleeps() -> None:
    rl = HostRateLimiter(0.0)
    start = time.monotonic()
    for _ in range(50):
        await rl.acquire("https://cdn1.suno.ai/x")
    assert time.monotonic() - start < 0.05


async def test_enforces_min_interval_same_host() -> None:
    rl = HostRateLimiter(0.05, jitter=False)  # exact bounds for this test
    start = time.monotonic()
    await rl.acquire("https://cdn1.suno.ai/a")
    await rl.acquire("https://cdn1.suno.ai/b")
    await rl.acquire("https://cdn1.suno.ai/c")
    elapsed = time.monotonic() - start
    # Three requests, two gaps of >=50ms => at least ~100ms.
    assert elapsed >= 0.09


async def test_different_hosts_independent() -> None:
    rl = HostRateLimiter(0.2)
    start = time.monotonic()
    await rl.acquire("https://cdn1.suno.ai/a")
    await rl.acquire("https://cdn2.suno.ai/a")  # different host: no wait
    assert time.monotonic() - start < 0.1


async def test_concurrent_requests_serialised() -> None:
    rl = HostRateLimiter(0.03, jitter=False)  # exact bounds for this test
    start = time.monotonic()
    await asyncio.gather(*[rl.acquire("https://cdn1.suno.ai/x") for _ in range(4)])
    elapsed = time.monotonic() - start
    # 4 requests => at least 3 gaps of 30ms.
    assert elapsed >= 0.08


async def test_negative_interval_clamped() -> None:
    rl = HostRateLimiter(-5.0)
    assert rl.min_interval == 0.0
    await rl.acquire("https://cdn1.suno.ai/x")


def test_config_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    from suno_dl.config import load_config

    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_RATE_LIMIT_SEC", "1.5")
    monkeypatch.setenv("SUNO_USER_AGENT", "my-agent/1.0")
    cfg = load_config()
    assert cfg.rate_limit_sec == 1.5
    assert cfg.user_agent == "my-agent/1.0"


def test_config_rejects_bad_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from suno_dl.config import load_config

    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_RATE_LIMIT_SEC", "-1")
    with pytest.raises(ValueError):
        load_config()


def test_fetcher_uses_configurable_ua() -> None:
    from suno_dl.fetcher import Fetcher

    f = Fetcher(1.0, 0, user_agent="custom/9")
    assert f.user_agent == "custom/9"
    assert f.rate_limiter.min_interval == 0.0


def test_default_ua_is_honest() -> None:
    """The default must identify the tool, not spoof a browser."""
    from suno_dl.fetcher import USER_AGENT

    assert "suno-download-cli" in USER_AGENT
    assert "Mozilla" not in USER_AGENT


async def test_jitter_keeps_spacing_within_bounds() -> None:
    """Jitter multiplies the interval by a factor in [0.75, 1.5] (never 0)."""
    rl = HostRateLimiter(0.1, jitter=True)
    start = time.monotonic()
    for _ in range(6):
        await rl.acquire("https://cdn1.suno.ai/a")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.40  # 5 gaps x [0.075, 0.15] >= 0.375; floor with slack
    assert elapsed <= 1.2  # ceiling well above 0.75s max


def test_config_defaults_are_human_paced() -> None:
    """Politeness is on by default: sequential downloads, jittered spacing."""
    from suno_dl.config import Config

    cfg = Config(default_handle="drinko")
    assert cfg.concurrency == 1
    assert cfg.rate_limit_sec == 1.5
