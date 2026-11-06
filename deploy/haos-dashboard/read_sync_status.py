"""Read only CT107 status for HA's command-line sensor; no network or writes."""

import json
from pathlib import Path

path = Path("/media/trackie_ct107/.library/sync-status.json")
try:
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Status exceeds limit")
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or data.get("schema") != 1
        or not isinstance(data.get("phase"), str)
    ):
        raise ValueError("Unknown status")
    print(json.dumps(data, ensure_ascii=False, allow_nan=False))
except (OSError, ValueError, TypeError):
    print(
        json.dumps(
            {
                "phase": "offline",
                "message": "Music share or sync status unavailable",
                "available": False,
            }
        )
    )
