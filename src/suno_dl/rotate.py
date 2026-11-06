"""Rolling-library caps: keep an archive bounded and prune the oldest tracks.

Why this exists
---------------
A plain sync mirrors everything a profile ever published, so an archive only
grows. A Home Assistant box wants a *rolling* library instead — the newest 20
tracks, or at most 1 GB — and that needs two things a downloader alone cannot
do (#93):

* **stop downloading at a cap** (``rotate`` off): a bounded mirror, never a
  deletion;
* **delete the oldest tracks until under the cap** (``rotate`` on): the
  archive is capped by track count and/or bytes, oldest-first.

Because this is the one destructive path in the tool it is guarded hard:

* only files **inside the archive root** are ever removed, re-checked at
  deletion time, and symlinks are neither followed nor managed;
* a track's ``<stem>.json`` id sidecar (see :mod:`suno_dl.sidecar`) goes with the
  track, but only a genuine suno-dl sidecar that records that track's own media
  and whose stem matches a pruned track; any other ``.json`` is left alone;
* every removal is logged individually;
* ``--dry-run`` reports the plan and deletes nothing;
* a plan that would delete **every** track in the archive is refused outright.

Age is the Suno ``created_at`` recorded for the track when it was downloaded
(``.suno-rotation.json`` next to the archive), falling back to the file's
mtime — an archive seeded before rotation existed has no state yet and simply
sorts by mtime. The same state remembers which clip ids were pruned so a
rotation never re-downloads what it deliberately deleted.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .sidecar import SIDECAR_SUFFIX, read_sidecar

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIO_SUFFIXES",
    "COVER_SUFFIXES",
    "MEGABYTE",
    "STATE_FILENAME",
    "RotationPlan",
    "RotationState",
    "TrackGroup",
    "apply_rotation",
    "archive_usage",
    "plan_rotation",
    "rotate_archive",
]

MEGABYTE = 1024 * 1024

# Rotation bookkeeping, written next to the archive (dotfile: no media server
# indexes it, and it is inert for anyone who never sets a cap).
STATE_FILENAME = ".suno-rotation.json"
# Crash journal (#147). Rotation deletes several files per group and only
# persists state at the end, so a crash in between used to leave the archive
# and its bookkeeping disagreeing. The journal is JSONL: one ``planned``
# header (written atomically before the first unlink), one ``removed`` event
# per successful unlink (appended and fsynced), and a final ``completed``.
# Recovery is reconciliation only: it never deletes anything, because a later
# sync may already have re-created a file.
JOURNAL_FILENAME = ".suno-rotation.journal"

# Playable containers. A group is every file in one directory that shares a
# stem with one of these — ``Song.m4a``, ``Song.mp4``, ``Song.cover.jpg``.
AUDIO_SUFFIXES = frozenset({".m4a", ".mp4", ".m4b", ".aac", ".mp3", ".wav", ".flac", ".ogg"})
COVER_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
_COVER_MARK = ".cover."


@dataclass
class RotationState:
    """What rotation remembers between runs: ages and pruned clip ids."""

    # archive-relative stem (``drinko/Song``) -> {"id", "created_at"}
    tracks: dict[str, dict[str, str]] = field(default_factory=dict)
    # clip ids that were pruned away and must not be re-downloaded.
    pruned: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, root: Path) -> RotationState:
        """Read the state file; a missing or corrupt file is an empty state."""
        path = root / STATE_FILENAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            return cls()
        except ValueError:
            logger.warning("Ignoring unreadable rotation state %s", path.name)
            return cls()
        if not isinstance(data, dict):
            return cls()
        tracks = data.get("tracks")
        pruned = data.get("pruned")
        return cls(
            tracks={
                str(k): dict(v)
                for k, v in (tracks if isinstance(tracks, dict) else {}).items()
                if isinstance(v, dict)
            },
            pruned=[str(x) for x in (pruned if isinstance(pruned, list) else [])],
        )

    def save(self, root: Path) -> None:
        """Atomically write the state file; failure is logged, never fatal."""
        path = root / STATE_FILENAME
        tmp = path.with_name(path.name + ".tmp")
        payload = {"version": 1, "tracks": self.tracks, "pruned": sorted(set(self.pruned))}
        try:
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("Could not write rotation state %s: %s", path.name, exc)
            tmp.unlink(missing_ok=True)

    def record(self, root: Path, base: Path, *, clip_id: str, created_at: str | None) -> None:
        """Remember the age of a freshly downloaded track."""
        entry = {"id": clip_id}
        if created_at:
            entry["created_at"] = created_at
        try:
            key = _stem_key(base.relative_to(root))
        except ValueError:
            key = _stem_key(Path(base.name))
        self.tracks[key] = entry

    def forget(self, key: str) -> None:
        """Drop a pruned track's age entry (its id moves to ``pruned``)."""
        self.tracks.pop(key, None)

    def mark_pruned(self, clip_id: str | None) -> None:
        if clip_id and clip_id not in self.pruned:
            self.pruned.append(clip_id)

    def clip_id_for(self, key: str) -> str | None:
        entry = self.tracks.get(key)
        return entry.get("id") if entry else None

    def created_at_for(self, key: str) -> str | None:
        entry = self.tracks.get(key)
        return entry.get("created_at") if entry else None


@dataclass(frozen=True)
class TrackGroup:
    """One track on disk: its media file plus every sidecar file (cover, id ``.json``)."""

    key: str  # archive-relative stem, e.g. "drinko/Song"
    files: tuple[Path, ...]
    size: int  # bytes across every file in the group
    created_at: str | None
    clip_id: str | None

    @property
    def age(self) -> float:
        """Sort key: Suno ``created_at`` when known, else file mtime."""
        stamp = _parse_ts(self.created_at)
        if stamp is not None:
            return stamp
        for path in self.files:
            if path.suffix.lower() == SIDECAR_SUFFIX:
                # A sidecar can be (re)written long after the track was saved;
                # its mtime says nothing about the track's age.
                continue
            try:
                return path.stat().st_mtime
            except OSError:
                continue
        return 0.0


@dataclass(frozen=True)
class RotationPlan:
    """The oldest-first groups a cap demands, plus why it might be refused."""

    remove: tuple[TrackGroup, ...]
    tracks_before: int
    bytes_before: int
    tracks_after: int
    bytes_after: int
    refused: bool = False
    reason: str = ""


def _stem_key(rel_path: Path) -> str:
    return rel_path.with_suffix("").as_posix()


def _parse_ts(value: str | None) -> float | None:
    """ISO 8601 (``Z`` suffix included) to epoch seconds; ``None`` if unusable."""
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    try:
        return stamp.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    """True if ``path`` resolves to a child of ``root`` (never a parent/escape)."""
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _classify(path: Path, root: Path) -> tuple[str, str] | None:
    """Map a file to ``(stem key, kind)``; ``None`` when rotation ignores it."""
    suffix = path.suffix.lower()
    lowered = path.name.lower()
    if suffix in COVER_SUFFIXES:
        idx = lowered.rfind(_COVER_MARK)
        if idx > 0:
            base = path.parent / path.name[:idx]
            return _stem_key(base.relative_to(root)), "cover"
        return None
    if suffix in AUDIO_SUFFIXES:
        return _stem_key(path.relative_to(root).with_suffix("")), "media"
    if suffix == SIDECAR_SUFFIX and not path.name.startswith("."):
        return _stem_key(path.relative_to(root)), "sidecar"
    return None


def _owned_sidecar(path: Path, media: list[Path], known_id: str | None) -> str | None:
    """The clip id of ``path`` if it is the id sidecar of the track whose files are ``media``.

    Owned means: a valid suno-dl sidecar (clip UUID + ``saved_files``), at least
    one of its ``saved_files`` is a file of this very track, and, when rotation
    state already knows this track's id, the two ids agree. Anything else (a
    user's ``Song.json``, another tool's metadata) is not ours to delete.
    """
    data = read_sidecar(path)
    if data is None:
        return None
    clip_id = str(data["id"]).lower()
    if known_id is not None and known_id.lower() != clip_id:
        return None
    names = {m.name for m in media}
    listed = {Path(n).name for n in data["saved_files"] if isinstance(n, str)}
    if not names & listed:
        return None
    return clip_id


def scan_groups(root: Path, state: RotationState | None = None) -> list[TrackGroup]:
    """Group every managed file under ``root`` per track, oldest data included.

    Symlinks are never followed or managed — a link that escapes the archive
    cannot make rotation touch (or size) anything outside it. Unmanaged files
    (dotfiles, ``.part`` temporaries, unrelated documents) are ignored.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []
    grouped: dict[str, list[Path]] = {}
    sidecars: dict[str, Path] = {}
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        logger.debug("Rotation ignoring symlink: %s", entry.path)
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        stack.append(Path(entry.path))
                        continue
                    classified = _classify(Path(entry.path), root)
                    if classified is not None:
                        key, kind = classified
                        if kind == "sidecar":
                            sidecars[key] = Path(entry.path)
                        else:
                            grouped.setdefault(key, []).append(Path(entry.path))
        except OSError as exc:
            logger.debug("Rotation could not scan %s: %s", current, exc)
            continue

    groups: list[TrackGroup] = []
    for key, paths in grouped.items():
        clip_id = state.clip_id_for(key) if state else None
        sidecar = sidecars.get(key)
        if sidecar is not None:
            owned_id = _owned_sidecar(sidecar, paths, clip_id)
            if owned_id is not None:
                paths = [*paths, sidecar]
                clip_id = clip_id or owned_id
        size = 0
        for path in paths:
            try:
                size += path.stat().st_size
            except OSError:
                continue
        groups.append(
            TrackGroup(
                key=key,
                files=tuple(sorted(paths)),
                size=size,
                created_at=state.created_at_for(key) if state else None,
                clip_id=clip_id,
            )
        )
    return groups


def archive_usage(root: Path) -> tuple[int, int]:
    """``(track count, managed bytes)`` for the whole archive."""
    groups = scan_groups(root)
    return len(groups), sum(group.size for group in groups)


def plan_rotation(
    root: Path,
    *,
    max_tracks: int = 0,
    max_disk_mb: int = 0,
    state: RotationState | None = None,
) -> RotationPlan:
    """Oldest-first groups that must go for the archive to fit both caps."""
    groups = scan_groups(root, state)
    ordered = sorted(groups, key=lambda g: (g.age, g.key))
    count = len(groups)
    total = sum(group.size for group in groups)
    before = (count, total)

    bytes_cap = max_disk_mb * MEGABYTE if max_disk_mb > 0 else None
    doomed: list[TrackGroup] = []
    for group in ordered:
        over_count = max_tracks > 0 and count > max_tracks
        over_bytes = bytes_cap is not None and total > bytes_cap
        if not (over_count or over_bytes):
            break
        doomed.append(group)
        count -= 1
        total -= group.size

    refused = bool(groups) and len(doomed) == len(groups)
    reason = ""
    if refused:
        reason = (
            "deleting would empty the whole archive "
            f"({len(groups)} track(s), caps: max_tracks={max_tracks or 'off'} "
            f"max_disk_mb={max_disk_mb or 'off'}); refusing to delete anything"
        )
    return RotationPlan(
        remove=tuple(doomed),
        tracks_before=before[0],
        bytes_before=before[1],
        tracks_after=count,
        bytes_after=total,
        refused=refused,
        reason=reason,
    )


def _journal_path(root: Path) -> Path:
    return root / JOURNAL_FILENAME


def begin_journal(root: Path, plan: RotationPlan) -> Path | None:
    """Durably record ``plan`` before the first unlink.

    The header is written to a temporary file and replaced atomically, so a
    crash either leaves no journal (nothing was deleted) or the complete plan.
    """
    path = _journal_path(root)
    header = {
        "version": 1,
        "phase": "planned",
        "groups": [
            {
                "key": group.key,
                "clip_id": group.clip_id,
                "files": [file.name for file in group.files],
            }
            for group in plan.remove
        ],
    }
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(header, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not write rotation journal: %s", exc)
        tmp.unlink(missing_ok=True)
        return None
    return path


def _append_journal(path: Path, event: dict[str, object]) -> None:
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:  # pragma: no cover - disk failure, still best-effort
        logger.warning("Could not append to rotation journal: %s", exc)


def read_journal(path: Path) -> tuple[dict | None, list[dict]]:
    """Return ``(header, events)``; a corrupt journal yields ``(None, [])``.

    Only complete lines count, so a torn final append after a crash is
    ignored rather than interpreted as a removal.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, []
    header: dict | None = None
    events: list[dict] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if header is None and isinstance(entry.get("groups"), list):
            header = entry
        elif isinstance(entry.get("event"), str):
            events.append(entry)
    return header, events


def recover_rotation(root: Path, state: RotationState | None = None) -> dict[str, int]:
    """Reconcile bookkeeping with what is actually on disk after a crash.

    Never deletes: a group whose media is entirely gone is marked pruned and
    forgotten; a group with media still present keeps its state entry so a
    later sync can repair or prune it normally. Already-deleted files cannot
    be restored — this only makes the next run's counts truthful. Idempotent:
    a completed journal changes nothing and is removed.
    """
    stats = {"recovered_groups": 0, "partial_groups": 0}
    path = _journal_path(root)
    if not path.is_file():
        return stats
    header, events = read_journal(path)
    if header is None:
        logger.warning("Ignoring unreadable rotation journal %s", path.name)
        path.unlink(missing_ok=True)
        return stats
    # Even a completed journal is reconciled: the state write that follows the
    # last unlink may itself have been the operation that was interrupted.
    state = state if state is not None else RotationState.load(root)
    for group in header["groups"]:
        if not isinstance(group, dict):
            continue
        key = group.get("key")
        if not isinstance(key, str):
            continue
        base = root / key
        media_left = [Path(str(base) + suffix) for suffix in AUDIO_SUFFIXES]
        remaining = [file for file in media_left if file.is_file() and not file.is_symlink()]
        if remaining:
            stats["partial_groups"] += 1
            logger.warning(
                "Rotation interrupted with %d media file(s) left for %s; keeping its state",
                len(remaining),
                key,
            )
            continue
        clip_id = group.get("clip_id")
        state.forget(key)
        if isinstance(clip_id, str):
            state.mark_pruned(clip_id)
        stats["recovered_groups"] += 1
    state.save(root)
    path.unlink(missing_ok=True)
    return stats


def apply_rotation(
    root: Path,
    plan: RotationPlan,
    *,
    dry_run: bool = False,
    state: RotationState | None = None,
) -> dict[str, int]:
    """Delete every file of every doomed group, re-checking each path.

    Containment and the symlink guard are re-verified per file (the plan may
    be older than the disk), each removal is logged on its own, and a failing
    unlink is counted and skipped rather than aborting the rest. The journal
    records the plan and every successful unlink, so a crash cannot leave
    counts or pruned ids ahead of (or behind) the filesystem.
    """
    stats = {"rotated_tracks": 0, "rotated_files": 0, "rotated_bytes": 0}
    root = root.expanduser().resolve()
    journal = None if dry_run else begin_journal(root, plan)
    for group in plan.remove:
        for path in group.files:
            if path.is_symlink() or not _is_within(path, root):
                logger.warning(
                    "Rotation refused to remove %s (outside the archive or a symlink)",
                    path,
                )
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if dry_run:
                logger.info("[dry-run] Rotation would remove %s (%d bytes)", path, size)
            else:
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("Rotation could not remove %s: %s", path, exc)
                    continue
                logger.info("Rotation removed %s (%d bytes)", path, size)
                if journal is not None:
                    _append_journal(
                        journal, {"event": "removed", "key": group.key, "file": path.name}
                    )
            stats["rotated_files"] += 1
            stats["rotated_bytes"] += size
        stats["rotated_tracks"] += 1
        if state is not None:
            state.forget(group.key)
            state.mark_pruned(group.clip_id)
    if journal is not None:
        _append_journal(journal, {"event": "completed"})
    return stats


def rotate_archive(
    root: Path,
    *,
    max_tracks: int = 0,
    max_disk_mb: int = 0,
    dry_run: bool = False,
    state: RotationState | None = None,
) -> dict[str, int]:
    """Prune the oldest tracks until the archive fits both caps.

    Returns ``{rotated_tracks, rotated_files, rotated_bytes}``. Refuses the
    whole plan when it would delete everything, and never writes anything in
    ``dry_run`` mode.
    """
    empty = {"rotated_tracks": 0, "rotated_files": 0, "rotated_bytes": 0}
    if max_tracks <= 0 and max_disk_mb <= 0:
        return empty
    root = root.expanduser().resolve()
    if state is not None and not dry_run:
        recover_rotation(root, state)
    plan = plan_rotation(root, max_tracks=max_tracks, max_disk_mb=max_disk_mb, state=state)
    if plan.refused:
        logger.error("Rotation refused: %s", plan.reason)
        return empty
    if not plan.remove:
        logger.debug(
            "Rotation: archive already within caps (%d track(s), %d bytes)",
            plan.tracks_before,
            plan.bytes_before,
        )
        return empty
    logger.info(
        "Rotation: archive over cap (max_tracks=%s max_disk_mb=%s): %d track(s), "
        "%d bytes; pruning the oldest %d",
        max_tracks or "off",
        max_disk_mb or "off",
        plan.tracks_before,
        plan.bytes_before,
        len(plan.remove),
    )
    stats = apply_rotation(root, plan, dry_run=dry_run, state=state)
    if state is not None and not dry_run:
        state.save(root)
    if not dry_run:
        # The state file is durable before the journal is retired; recovery is
        # then a no-op even if this process dies right here.
        _journal_path(root).unlink(missing_ok=True)
    return stats
