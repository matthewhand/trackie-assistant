"""One approved, auditable selected-playlist job using the public download engine."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from suno_dl import __version__
from suno_dl.archive_lock import _exclusive_archive
from suno_dl.blocklist import load_blocked_ids
from suno_dl.downloader import download_tracks
from suno_dl.fetcher import Fetcher
from suno_dl.library import LibraryIndex, record_membership
from suno_dl.linux_downloads import _pending_state
from suno_dl.pacing import PlaybackPacer
from suno_dl.playlist_api import fetch_public_playlist_api_page
from suno_dl.playlists import PlaylistRef, _fetch_public_api_membership
from suno_dl.producer_lookup import ProducerLookupError
from suno_dl.sync_capacity import capacity_snapshot
from suno_dl.sync_state import publish_status
from suno_dl.urls import UUID_RE
from suno_dl.verified_sync import raise_if_migration_pending, require_owned_library


class ProviderStop(BaseException):
    """Refusal/backpressure stops the run, including through ordinary catch handlers."""


async def run(
    root: Path,
    audit: Path,
    run_id: str,
    playlists: list[str],
    blocked: Path,
    *,
    expected_missing: frozenset[str] | None = None,
) -> int:
    if not UUID_RE.fullmatch(run_id) or any(not UUID_RE.fullmatch(p) for p in playlists):
        raise ValueError("Expected explicit playlist and run UUIDs")
    owned = False
    status_ready = False
    status = {
        "run_id": run_id,
        "selected_playlists": [],
        "available": True,
        "source_mode": "Audio and artwork; temporary video discarded",
        "downloaded": 0,
        "failed": 0,
        "covers_added": 0,
    }

    def event(kind, **values):
        row = {
            "at": datetime.now(UTC).isoformat(),
            "event": kind,
            "id": run_id,
            "run_id": run_id,
            **values,
        }
        with audit.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + chr(10))
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps(row, ensure_ascii=False), flush=True)

    queued = []

    def update(phase, message, **values):
        status.update(values, phase=phase, message=message)
        status["capacity"] = capacity_snapshot(root, queued, scope="Selected playlists")
        publish_status(root, status)

    def pacing(wait):
        deadline = datetime.now(UTC) + timedelta(seconds=wait)
        update(
            "waiting" if wait else "downloading",
            "Downloads are spaced to match playback duration."
            if wait
            else "Saving verified audio and artwork; no video will be kept.",
            next_transfer_at=deadline.isoformat() if wait else None,
        )

    async def response_guard(response):
        if response.status_code in (401, 403, 429) or response.status_code >= 500:
            raise ProviderStop("Suno returned HTTP " + str(response.status_code))

    try:
        with audit.open("x"):
            pass
        owned = True
        raise_if_migration_pending(root)
        _pending_state(root)
        pending = root / ".library" / "verified-sync-intents"
        if pending.exists() and any(pending.iterdir()):
            raise RuntimeError("An earlier publication needs recovery before a fresh job")
        index = LibraryIndex(root).scan()
        if index.conflicts:
            raise RuntimeError("Conflicting clip ownership")
        require_owned_library(root, index)
        status_ready = True
        update(
            "reading_playlists",
            "Checking the selected public playlists; existing tracks are preserved.",
        )
        baseline = {
            p.relative_to(root).as_posix(): [
                p.stat().st_ino,
                p.stat().st_size,
                p.stat().st_mtime_ns,
            ]
            for p in root.rglob("*")
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
        }
        (audit.parent / "baseline.json").write_text(json.dumps(baseline))
        async with Fetcher(30, 0, rate_limit_sec=1.5) as fetcher:
            fetcher.client.event_hooks["response"].append(response_guard)
            tracks_by_id = {}
            members = {}
            excluded = set()
            for playlist_id in dict.fromkeys(playlists):
                pages = {}

                async def page(ref, number, cache=pages):
                    payload = await fetch_public_playlist_api_page(
                        ref.id, number, timeout_sec=30, rate_limiter=fetcher.rate_limiter
                    )
                    cache[number] = payload
                    return payload

                tracks = await _fetch_public_api_membership(
                    page, PlaylistRef(id=playlist_id, name=playlist_id), on_excluded=excluded.add
                )
                name = str(pages[1].get("name") or playlist_id)[:200]
                status["selected_playlists"].append(name)
                members["playlist:" + playlist_id] = [t.id for t in tracks]
                tracks_by_id.update({t.id: t for t in tracks})
            blocked_ids = load_blocked_ids(blocked)
            selected = [
                t for t in tracks_by_id.values() if t.id not in excluded and t.id not in blocked_ids
            ]
            missing = [t for t in selected if index.get(t.id) is None]
            if expected_missing is not None and {t.id for t in missing} != expected_missing:
                raise ValueError(
                    "Missing clips differ from the approved resume set; no transfers started"
                )
            queued = list(missing)
            status.update(
                total=len(selected),
                already_local=len(selected) - len(missing),
                missing=len(missing),
                remaining=len(missing),
            )
            event(
                "started",
                sources=len(members),
                total=len(selected),
                existing=len(selected) - len(missing),
                missing=len(missing),
                version=__version__,
                mode=status["source_mode"],
            )
            (audit.parent / "inventory.json").write_text(
                json.dumps(
                    {
                        "members": members,
                        "missing": [t.id for t in missing],
                        "selected": [t.id for t in selected],
                    },
                    ensure_ascii=False,
                )
            )
            async with _exclusive_archive(root):
                for source, ids in members.items():
                    record_membership(root, source, [cid for cid in ids if cid not in excluded])
            pacer = PlaybackPacer(on_wait=pacing)
            pacer.bind_checkpoint(root / ".library" / "media-pacing.json")
            for track in missing:
                update(
                    "checking",
                    "Verifying this track's own producer and published media.",
                    current_title=track.title,
                    next_transfer_at=None,
                )
                try:
                    stats = await download_tracks(
                        [track],
                        fetcher,
                        1,
                        root,
                        "{artist}_{title}",
                        "drinko",
                        audio_only=True,
                        covers=True,
                        extract_audio=True,
                        subdir="drinko",
                        min_free_mb=2048,
                        playback_paced=True,
                        pacer=pacer,
                        blocked_ids=blocked_ids,
                    )
                except ProducerLookupError:
                    status["failed"] += 1
                    event("unavailable", id=track.id, reason="Producer identity not verified")
                else:
                    if stats["downloaded"]:
                        status["downloaded"] += 1
                        entry = LibraryIndex(root).scan().get(track.id)
                        if entry is None:
                            raise RuntimeError("Published recording cannot be indexed")
                        data = json.loads(entry.sidecar.read_text())
                        has_cover = any(".cover." in n for n in data["saved_files"])
                        status["covers_added"] += int(has_cover)
                        event(
                            "downloaded",
                            id=track.id,
                            artist=data.get("artist"),
                            creator_handle=data.get("creator_handle"),
                            filename=entry.media[0].name,
                            cover=has_cover,
                        )
                    elif not stats["skipped_exists"]:
                        status["failed"] += 1
                        event(
                            "unavailable",
                            id=track.id,
                            reason="Published media unavailable or unreadable",
                        )
                queued = queued[1:]
                status["remaining"] -= 1
                update("checking", "Track checked. Existing recordings remain unchanged.")
            phase = "partial" if status["failed"] else "complete"
            update(
                phase,
                "Some tracks need attention."
                if status["failed"]
                else "All eligible tracks in the selected playlists are downloaded.",
                current_title=None,
                next_transfer_at=None,
            )
            event(
                "finished",
                total=len(selected),
                existing=len(selected) - len(missing),
                downloaded=status["downloaded"],
                failed=status["failed"],
                status=phase,
            )
            return int(bool(status["failed"]))
    except BaseException as exc:
        if owned:
            if isinstance(exc, ProviderStop):
                message = str(exc) + ". Sync stopped without automatic retry."
            else:
                message = "Sync stopped safely. Review its audit before retrying."
            if status_ready:
                update("stopped", message, next_transfer_at=None)
            event("stopped", exception=type(exc).__name__, message=message)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--playlist", action="append", required=True)
    parser.add_argument("--blocked", type=Path, required=True)
    parser.add_argument(
        "--expected-missing",
        action="append",
        help="Resume only if the missing clip UUIDs exactly match this repeatable list.",
    )
    args = parser.parse_args()
    if args.expected_missing is not None and any(
        not UUID_RE.fullmatch(cid) for cid in args.expected_missing
    ):
        parser.error("--expected-missing requires clip UUIDs")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    raise SystemExit(
        asyncio.run(
            run(
                args.library,
                args.audit,
                args.run_id,
                args.playlist,
                args.blocked,
                expected_missing=(
                    frozenset(args.expected_missing) if args.expected_missing is not None else None
                ),
            )
        )
    )


if __name__ == "__main__":
    main()
