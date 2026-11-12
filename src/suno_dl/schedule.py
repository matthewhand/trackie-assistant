"""Persistent, opt-in scheduling for one WebUI backend.

A disabled schedule never cancels an active run. The run manager and the
existing archive lock remain responsible for serialization and media safety.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .naming import normalize_watch_entry


def sources(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("Enter 1–20 public Suno profiles or playlist URLs")
    result = []
    for entry in value:
        if not isinstance(entry, str) or len(entry) > 256 or any(ord(c) < 32 for c in entry):
            raise ValueError("Invalid scheduled source")
        url, _ = normalize_watch_entry(entry)
        parts = urlsplit(url)
        if parts.query or parts.fragment:
            raise ValueError("Source URLs must not contain query strings or fragments")
        if url not in result:
            result.append(url)
    return result


def _read(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 131072:
            raise ValueError("Unsafe schedule state")
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("Invalid schedule state")
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".schedule-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class Scheduler:
    """One persistent schedule sharing the manual run manager's admission lock."""

    def __init__(
        self,
        archive: Path,
        manager: Any,
        command: Callable[[tuple[str, ...]], list[str]],
        defaults: list[str],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        import fcntl

        self.manager, self.command, self.clock = manager, command, clock
        self.root = archive / ".scheduler"
        if self.root.is_symlink():
            raise ValueError("Schedule directory must not be a symlink")
        self.root.mkdir(mode=0o700, exist_ok=True)
        parent_fd = os.open(archive, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        root_info = self.root.stat()
        if root_info.st_uid != os.geteuid() or root_info.st_mode & 0o022:
            raise ValueError("Schedule directory must be owned by this backend and private")
        self.path = self.root / "state.json"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.owner = os.open(
            self.root / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(self.owner)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Unsafe scheduler ownership lock")
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            notice = None
            try:
                self.data = _read(self.path)
                if self.data:
                    self._validate(self.data)
                    if self.data["enabled"] and self.data["next_run"] is None:
                        raise ValueError("Missing schedule deadline")
                    if self.data.get("last_run") is not None and not isinstance(
                        self.data["last_run"], dict
                    ):
                        raise ValueError("Invalid last-run state")
            except (OSError, ValueError):
                self.data = {}
                notice = (
                    "Schedule state could not be read; syncing is paused. Review and save settings."
                )
            if not self.data:
                self.data = {
                    "schema": 1,
                    "enabled": False,
                    "interval_minutes": 360,
                    "sources": sources(defaults) if defaults else [],
                    "next_run": None,
                    "last_run": None,
                    "last_finished_id": None,
                }
            if notice:
                self.data["notice"] = notice
            # A previous process cannot vouch for a still-running child. Preserve
            # its outcome as interrupted and retain the persisted retry deadline.
            last = self.data.get("last_run")
            if last and last.get("returncode") is None:
                last.update(
                    state="failed",
                    returncode=-1,
                    finished=self.clock(),
                    output=[
                        "Backend restarted during this run. The next check will "
                        "use the downloader's verified recovery; existing tracks are reused."
                    ],
                    error=None,
                    cancellable=False,
                )
                self._persist()
        except BaseException:
            os.close(self.owner)
            raise

    @staticmethod
    def _validate(data: dict[str, Any]) -> None:
        if (
            type(data.get("schema")) is not int
            or data["schema"] != 1
            or type(data.get("enabled")) is not bool
        ):
            raise ValueError("Invalid schedule state")
        minutes = data.get("interval_minutes")
        if type(minutes) is not int or not 15 <= minutes <= 1440:
            raise ValueError("Interval must be between 15 and 1440 minutes")
        if data.get("sources"):
            sources(data["sources"])
        elif data["enabled"]:
            raise ValueError("Configure scheduled sources before enabling sync")
        due = data.get("next_run")
        if due is not None and (type(due) not in (int, float) or not 0 <= due < 1e12):
            raise ValueError("Invalid next-run time")

    def _persist(self) -> None:
        _write(self.path, self.data)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(dict(self.data, available=True)))

    def save(self, value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"enabled", "interval_minutes", "sources"}:
            raise ValueError("Supply enabled, interval_minutes and sources")
        candidate = dict(value, schema=1, next_run=None)
        self._validate(candidate)
        candidate["sources"] = sources(value["sources"])
        with self.lock:
            previous = self.data
            candidate = dict(previous, **candidate)
            if candidate["enabled"]:
                if not previous["enabled"]:
                    candidate["next_run"] = self.clock()
                elif candidate["interval_minutes"] == previous["interval_minutes"]:
                    candidate["next_run"] = previous["next_run"]
                else:
                    last = previous.get("last_run") or {}
                    anchor = last.get("finished") or last.get("started") or self.clock()
                    candidate["next_run"] = anchor + candidate["interval_minutes"] * 60
            candidate.pop("notice", None)
            old = self.data
            self.data = candidate
            try:
                self._persist()
            except BaseException:
                self.data = dict(
                    old,
                    enabled=False,
                    next_run=None,
                    notice=(
                        "Schedule save could not be confirmed; "
                        "this backend is paused. Review settings."
                    ),
                )
                raise
            return self.snapshot()

    @staticmethod
    def _summary(run: dict[str, Any]) -> dict[str, Any]:
        def bounded(value: Any) -> Any:
            if isinstance(value, str):
                return value[:1024]
            if isinstance(value, list):
                return [bounded(item) for item in value[-40:]]
            if isinstance(value, dict):
                return {key: bounded(item) for key, item in list(value.items())[:32]}
            return value

        return bounded(run)

    def tick(self) -> None:
        with self.lock:
            if self.stop.is_set():
                return
            runs = self.manager.list()
            latest = next((self._summary(r) for r in runs if r["kind"] == "sync"), None)
            previous = self.data.get("last_run")
            if (
                latest
                and previous
                and latest["id"] == previous["id"]
                and previous.get("returncode") is not None
                and previous.get("finished") is not None
            ):
                latest = previous  # terminal error timestamps must not cause idle disk writes
            changed = False
            if latest and latest != self.data.get("last_run"):
                self.data["last_run"] = latest
                changed = True
            if (
                latest
                and latest.get("finished") is not None
                and latest["id"] != self.data.get("last_finished_id")
            ):
                self.data["last_finished_id"] = latest["id"]
                self.data["next_run"] = (
                    latest["finished"] + self.data["interval_minutes"] * 60
                    if self.data["enabled"]
                    else None
                )
                changed = True
            if changed:
                self._persist()
            if (
                not self.data["enabled"]
                or self.manager.busy
                or self.data["next_run"] is None
                or self.clock() < self.data["next_run"]
            ):
                return
            # Reserve a retry deadline before launching: a process crash must
            # not cause an immediate restart loop against the provider.
            due = self.data["next_run"]
            self.data["next_run"] = self.clock() + self.data["interval_minutes"] * 60
            self._persist()
            run, _ = self.manager.start("sync", self.command(tuple(self.data["sources"])))
            if run is None:
                self.data["next_run"] = due  # manual action won the admission race
            else:
                self.data["last_run"] = self._summary(run.snapshot())
            self._persist()

    def start(self) -> None:
        def loop() -> None:
            while not self.stop.wait(2):
                try:
                    self.tick()
                except Exception:  # scheduler errors pause safely; never spin/retry blindly
                    with self.lock:
                        self.data["enabled"] = False
                        self.data["next_run"] = None
                        self.data["notice"] = (
                            "Scheduling paused after a backend error; review Activity."
                        )
                        with contextlib.suppress(OSError):
                            self._persist()

        self.thread = threading.Thread(target=loop, name="trackie-schedule", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        with self.lock:
            os.close(self.owner)
