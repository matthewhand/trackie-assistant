"""Track-level failure records shared by the downloader and Sync UI."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TypedDict

from .urls import UUID_RE, song_page_url

RETRY_PREFIX = "TRACKIE_RETRY "
ARCHIVED_PREFIX = "TRACKIE_ARCHIVED "
FAILURE_PREFIX = "TRACKIE_FAILED "
MAX_FAILURES = 40
_UUID = UUID_RE.pattern
_LEGACY_HTTP = re.compile(
    rf"^WARNING: Published media returned HTTP ([1-5][0-9]{{2}}) for clip ({_UUID})$"
)
_LEGACY_FAILED = re.compile(
    rf"^WARNING: Clip ({_UUID}) has no usable supported media source; left missing for retry$"
)


def report_failed_track(clip_id: str, title: str, reason: str) -> None:
    print(
        FAILURE_PREFIX + json.dumps({"id": clip_id, "title": title[:500], "reason": reason}),
        flush=True,
    )


def parse_failed_track(line: str) -> dict[str, str] | None:
    if not line.startswith(FAILURE_PREFIX) or len(line) > 8192:
        return None
    try:
        value = json.loads(line[len(FAILURE_PREFIX) :])
    except ValueError:
        return None
    return validate_failure(value)


def validate_failure(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    clip_id, title, reason = (value.get(key) for key in ("id", "title", "reason"))
    if (
        not isinstance(clip_id, str)
        or not UUID_RE.fullmatch(clip_id)
        or not isinstance(title, str)
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        return None
    clip_id = clip_id.lower()
    return {
        "id": clip_id,
        "url": song_page_url(clip_id),
        "title": title[:500],
        "reason": reason[:500],
    }


class RetryResult(TypedDict):
    finished: float
    recovered: int
    failed: int
    skipped: int
    unfinished: int
    stopped: bool


class FailureHistory:
    """Store unresolved WebUI download failures by clip UUID in the archive."""

    def __init__(self, archive: Path) -> None:
        self.path = archive / ".trackie-failures.sqlite3"

    def _open(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise sqlite3.OperationalError("Linked failure history")
        return sqlite3.connect(self.path)

    def seed(self, records: list[dict[str, str]]) -> None:
        if not records:
            return
        with closing(self._open()) as db, db:
            # The initial schema and imported rows commit together.
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                self._create(db)
                db.executemany(
                    "INSERT OR REPLACE INTO failures VALUES (:id, :title, :reason)", records
                )

    @staticmethod
    def _create(db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS failures "
            "(id TEXT PRIMARY KEY, title TEXT NOT NULL, reason TEXT NOT NULL)"
        )
        db.execute("PRAGMA user_version = 1")

    def record(self, failure: dict[str, str]) -> None:
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                self._create(db)
            db.execute("INSERT OR REPLACE INTO failures VALUES (:id, :title, :reason)", failure)

    def resolve(self, clip_id: str) -> None:
        if self.path.exists():
            with closing(self._open()) as db, db:
                if db.execute("PRAGMA user_version").fetchone()[0] == 1:
                    db.execute("DELETE FROM failures WHERE id = ?", (clip_id,))

    def save_retry_result(self, result: RetryResult) -> None:
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                self._create(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS last_retry_result "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), finished REAL NOT NULL, "
                "recovered INTEGER NOT NULL, failed INTEGER NOT NULL, skipped INTEGER NOT NULL, "
                "unfinished INTEGER NOT NULL, stopped INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT OR REPLACE INTO last_retry_result VALUES "
                "(1, :finished, :recovered, :failed, :skipped, :unfinished, :stopped)",
                dict(result),
            )

    def last_retry_result(self) -> RetryResult | None:
        if not self.path.exists():
            return None
        with closing(self._open()) as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'last_retry_result'"
            ).fetchone()
            if not exists:
                return None
            row = db.execute(
                "SELECT finished, recovered, failed, skipped, unfinished, stopped "
                "FROM last_retry_result WHERE id = 1"
            ).fetchone()
            if row is None:
                return None
            finished, recovered, failed, skipped, unfinished, stopped = row
            return RetryResult(
                finished=finished,
                recovered=recovered,
                failed=failed,
                skipped=skipped,
                unfinished=unfinished,
                stopped=bool(stopped),
            )

    def list(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with closing(self._open()) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                return []
            return [
                {"id": clip_id, "url": song_page_url(clip_id), "title": title, "reason": reason}
                for clip_id, title, reason in db.execute(
                    "SELECT id, title, reason FROM failures ORDER BY rowid"
                )
            ]


def legacy_failed_tracks(output: object) -> list[dict[str, str]]:
    """Use only final per-track failures, never a transient source warning."""
    if not isinstance(output, list):
        return []
    statuses: dict[str, set[str]] = {}
    failures = {}
    for line in output:
        if not isinstance(line, str):
            continue
        http = _LEGACY_HTTP.search(line)
        if http:
            code, clip_id = http.groups()
            statuses.setdefault(clip_id.lower(), set()).add(code)
        failed = _LEGACY_FAILED.search(line)
        if failed:
            clip_id = failed[1].lower()
            codes = sorted(statuses.get(clip_id, ()))
            reason = (
                "Published media returned " + ", ".join("HTTP " + code for code in codes) + "."
                if codes
                else "No supported media source could be downloaded."
            )
            failures[clip_id] = {
                "id": clip_id,
                "url": song_page_url(clip_id),
                "title": "",
                "reason": reason,
            }
    return list(failures.values())[-MAX_FAILURES:]
