#!/usr/bin/env python3
"""Container health probes for the CT107 deployment (#136, #173).

Docker's healthcheck only needs a non-zero exit for "unhealthy"; the message on
stdout is what `docker inspect` shows, so each probe explains itself.

Two distinct semantics:

* ``gateway`` — the HTTP listener answers. A 401/403 counts as healthy: the
  gateway policy refusing an unauthenticated read is the intended behaviour
  (#162), not a fault.
* ``worker`` — the loop is turning. The worker touches ``/tmp/worker-heartbeat``
  once per interval; an old heartbeat means the loop stopped, which is a real
  failure even though the process may still exist.
"""

from __future__ import annotations

import http.client
import os
import sys
import time

# Container-local tmpfs written by the entrypoint; not a shared temp path.
HEARTBEAT = "/tmp/worker-heartbeat"  # noqa: S108
# A heartbeat is refreshed at the start of every interval, so the newest one can
# legitimately be one full interval old. The grace covers a long download.
GRACE_SECONDS = 300


def probe_gateway() -> int:
    try:
        port = int(os.environ.get("TRACKIE_GATEWAY_PORT", "8760"))
        if not 1 <= port <= 65535:
            raise ValueError("Invalid port")
    except ValueError:
        print("gateway port must be an integer from 1 to 65535")
        return 1
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        connection.request("GET", "/api/status")
        status = connection.getresponse().status
    except (OSError, http.client.HTTPException) as exc:
        print(f"gateway unreachable: {exc.__class__.__name__}")
        return 1
    finally:
        connection.close()
    if status >= 500:
        print(f"gateway answered {status}: server error")
        return 1
    print(f"gateway answered {status}")
    return 0


def probe_worker() -> int:
    interval = int(os.environ.get("TRACKIE_SYNC_INTERVAL_MINUTES", "360"))
    try:
        age = time.time() - os.stat(HEARTBEAT).st_mtime
    except OSError:
        print(f"no worker heartbeat at {HEARTBEAT}")
        return 1
    limit = interval * 60 + GRACE_SECONDS
    if age > limit:
        # A disabled-sync standby still keeps the heartbeat fresh, so this is
        # always a stuck loop rather than the intentional disable (#136).
        print(f"worker heartbeat is {int(age)}s old (limit {limit}s)")
        return 1
    print(f"worker heartbeat {int(age)}s old")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"gateway", "worker"}:
        print("usage: ct107-healthcheck {gateway|worker}", file=sys.stderr)
        return 2
    return probe_gateway() if argv[1] == "gateway" else probe_worker()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
