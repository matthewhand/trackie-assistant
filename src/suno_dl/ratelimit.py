"""Per-host rate limiting for outbound requests.

A popular public release could otherwise hammer Suno's CDN or page endpoints and
get users blocked. This enforces a minimum interval between requests to the
same host, shared across all concurrent tasks.
"""

from __future__ import annotations

import asyncio
import random
import time
from urllib.parse import urlsplit

# Crypto-strength source for timing jitter (also satisfies S311 without
# suppression); cost is negligible at rate-limiter call rates.
_SYS_RANDOM = random.SystemRandom()


class HostRateLimiter:
    """Enforce a minimum interval (seconds) between requests to each host.

    Intervals are multiplied by a random factor in ``[0.75, 1.5]`` (jitter) so
    request spacing does not look metronomic; pass ``jitter=False`` for exact
    intervals (used by tests that assert precise timing bounds).

    ``min_interval <= 0`` disables limiting.
    """

    def __init__(self, min_interval: float, jitter: bool = True) -> None:
        self.min_interval = max(0.0, min_interval)
        self.jitter = jitter
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}

    def _lock_for(self, host: str) -> asyncio.Lock:
        lock = self._locks.get(host)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[host] = lock
        return lock

    async def acquire(self, url: str) -> None:
        """Block until this host may be requested again."""
        if self.min_interval <= 0:
            return
        host = urlsplit(url).hostname or ""
        async with self._lock_for(host):
            now = time.monotonic()
            earliest = self._next_allowed.get(host, 0.0)
            wait = earliest - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed[host] = max(earliest, now) + self._effective_interval()

    def _effective_interval(self) -> float:
        if not self.jitter or self.min_interval <= 0:
            return self.min_interval
        return self.min_interval * _SYS_RANDOM.uniform(0.75, 1.5)
