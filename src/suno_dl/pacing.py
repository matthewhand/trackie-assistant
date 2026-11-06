"""Playback-duration pacing for automated bulk sync (#161).

A bulk import should not issue media transfers back-to-back. This module
spaces the *start* of one transfer from the next by at least the previous
track's playback duration, using a monotonic clock so wall-clock changes and
NTP steps cannot shorten (or stall) the gap.

The pacer is deliberately conservative about untrusted input: a missing,
non-finite, non-positive or absurdly large duration falls back to a bounded
default instead of raising or scheduling a multi-hour wait.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

logger = logging.getLogger(__name__)

#: Used when the provider did not declare a usable duration (4 minutes).
FALLBACK_DURATION_SEC = 240.0
#: Upper bound on any single interval, so hostile metadata cannot park a sync.
MAX_DURATION_SEC = 3600.0


def effective_duration_sec(duration: float | None) -> float:
    """Return a bounded, finite playback duration for pacing.

    ``None``, non-numeric, ``NaN``, ``inf`` and values <= 0 all fall back to
    ``FALLBACK_DURATION_SEC``; anything above ``MAX_DURATION_SEC`` is clamped.
    """
    if duration is None:
        return FALLBACK_DURATION_SEC
    try:
        value = float(duration)
    except (TypeError, ValueError):
        return FALLBACK_DURATION_SEC
    if not math.isfinite(value) or value <= 0:
        return FALLBACK_DURATION_SEC
    return min(value, MAX_DURATION_SEC)


class PlaybackPacer:
    """Serialize transfer starts at least one playback duration apart.

    One instance spans a whole bulk run (every playlist/profile in a sync),
    so switching sources never resets the pacing. Clock and sleep are
    injectable so tests can run deterministically without real waits.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        fallback_sec: float = FALLBACK_DURATION_SEC,
        max_sec: float = MAX_DURATION_SEC,
        on_wait: Callable[[float], None] | None = None,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._fallback = fallback_sec
        self._max = max_sec
        self._lock = asyncio.Lock()
        self._next_start: float | None = None
        self.last_duration: float | None = None
        self.total_wait: float = 0.0
        self.on_wait = on_wait
        self._checkpoint: Path | None = None

    def bind_checkpoint(self, path: Path) -> None:
        """Caller holds the archive lock. Preserve pacing across process restarts."""
        if self._checkpoint is not None:
            if self._checkpoint != path:
                raise ValueError("Pacer cannot span different archive checkpoints")
            return
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Linked pacing checkpoint")
        self._checkpoint = path
        if not path.exists():
            return
        if path.stat().st_size > 4096:
            raise ValueError("Oversized pacing checkpoint")
        data = json.loads(path.read_text())
        duration = data.get("duration")
        deadline = data.get("deadline")
        if (
            data.get("schema") != 1
            or type(duration) not in (int, float)
            or type(deadline) not in (int, float)
            or not math.isfinite(duration)
            or not math.isfinite(deadline)
            or not 0 < duration <= self._max
        ):
            raise ValueError("Invalid pacing checkpoint")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        # Same-boot monotonic time is immune to wall-clock changes. On a new
        # boot, conservatively wait one complete previous track duration.
        restored = deadline if data.get("boot") == boot else self._clock() + duration
        if restored > self._clock() + self._max:
            raise ValueError("Pacing deadline exceeds the permitted interval")
        self._next_start = max(self._next_start or 0, restored)

    def _save_checkpoint(self) -> None:
        if self._checkpoint is None:
            return
        if self._checkpoint.is_symlink():
            raise ValueError("Linked pacing checkpoint")
        data = {
            "schema": 1,
            "deadline": self._next_start,
            "duration": self.last_duration,
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
        temp = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self._checkpoint.parent, prefix=".media-pacing-", delete=False
            ) as stream:
                temp = Path(stream.name)
                stream.write(json.dumps(data, allow_nan=False).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self._checkpoint)
            fd = os.open(self._checkpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    @property
    def next_start(self) -> float | None:
        """Monotonic instant the next transfer may start, if one was gated."""
        return self._next_start

    def _duration(self, duration: float | None) -> float:
        if duration is None:
            return self._fallback
        try:
            value = float(duration)
        except (TypeError, ValueError):
            return self._fallback
        if not math.isfinite(value) or value <= 0:
            return self._fallback
        return min(value, self._max)

    async def wait(self, duration: float | None) -> float:
        """Block until the next transfer may start; return seconds waited.

        The wait is evaluated inside a lock so concurrent tasks cannot all
        observe the same free slot and start together.
        """
        async with self._lock:
            now = self._clock()
            waited = 0.0
            if self._next_start is not None and self._next_start > now:
                waited = self._next_start - now
                if self.on_wait is not None:
                    self.on_wait(waited)
                await self._sleep(waited)
                if self.on_wait is not None:
                    self.on_wait(0)
                now = self._clock()
            self.last_duration = self._duration(duration)
            self._next_start = now + self.last_duration
            self._save_checkpoint()
            self.total_wait += waited
            if waited:
                logger.info(
                    "Playback pacing: waited %.0fs before the next transfer (previous track %.0fs)",
                    waited,
                    self.last_duration,
                )
            return waited
