"""Read-only queue estimates and destination filesystem capacity."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PREFIX = "TRACKIE_QUEUE "
ESTIMATE_NOTE = (
    "Estimated saved audio and artwork, not a transfer quota. "
    "Assumes 128–256 kbps audio and 0.25–5 MiB artwork per track. "
    "Tracks without a duration assume 4 minutes. Temporary video transfers "
    "and working space are additional and cannot be predicted from duration alone."
)


def destination_space(destination: Path) -> dict[str, Any]:
    """Use the destination's filesystem, or its existing ancestor before creation."""
    result: dict[str, Any] = {
        "destination": str(destination),
        "measured_at": datetime.now(UTC).isoformat(),
        "available": False,
    }
    try:
        probe = destination
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        usage = shutil.disk_usage(probe)
        result.update(available=True, free_bytes=usage.free, total_bytes=usage.total)
    except OSError:
        pass
    return result


def queue_estimate(tracks: Iterable[Any], *, covers: bool = True) -> dict[str, Any]:
    """An indicative range only; never used to authorize writes or bypass reserves."""
    durations = [getattr(t, "duration_sec", None) for t in tracks]
    known = [
        d
        for d in durations
        if isinstance(d, (int, float)) and not isinstance(d, bool) and 0 < d <= 86400
    ]
    unknown = len(durations) - len(known)
    seconds = sum(known) + unknown * 240
    count = len(durations)
    return {
        "count": count,
        "duration_unknown": unknown,
        "estimated_seconds": seconds,
        "estimated_bytes_min": int(seconds * 128000 / 8 + count * covers * 262144),
        "estimated_bytes_max": int(seconds * 256000 / 8 + count * covers * 5242880),
        "note": ESTIMATE_NOTE,
    }


def capacity_snapshot(
    destination: Path, tracks: Iterable[Any], *, covers: bool = True, scope: str = "Current batch"
) -> dict[str, Any]:
    return {
        "queue": queue_estimate(tracks, covers=covers),
        "storage": destination_space(destination),
        "scope": scope,
    }


def report_capacity(destination: Path, tracks: Iterable[Any], *, covers: bool) -> None:
    print(PREFIX + json.dumps(capacity_snapshot(destination, tracks, covers=covers)), flush=True)


def latest_capacity(lines: list[str]) -> dict[str, Any] | None:
    for line in reversed(lines):
        if not line.startswith(PREFIX) or len(line) > 8192:
            continue
        try:
            data = json.loads(line[len(PREFIX) :])
            queue = data["queue"]
            if not isinstance(data, dict) or not isinstance(queue, dict):
                continue
            fields = (
                "count",
                "duration_unknown",
                "estimated_seconds",
                "estimated_bytes_min",
                "estimated_bytes_max",
            )
            if any(
                type(queue.get(k)) not in (int, float) or not 0 <= queue[k] < 10**18 for k in fields
            ):
                continue
            return data
        except (ValueError, TypeError, KeyError):
            continue
    return None
