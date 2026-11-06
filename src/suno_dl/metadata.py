"""Metadata reconciliation for recordings that are already archived (#159).

The default UUID skip is right about the audio and wrong about the metadata: a
mixed-creator playlist could have written the playlist owner as the artist, and
a re-sync would keep that wrong tag forever. ``--metadata-only`` and
``--update-existing`` repair the tags and the JSON sidecar **without** touching
the audio stream, the filename, the playlist folder or Music Assistant's
references. ``--dry-run`` reports the exact proposed differences and writes
nothing.

Guarantees
----------
* Matching is by validated clip UUID (:class:`suno_dl.library.LibraryIndex`),
  never by filename or title.
* An unresolved producer never overwrites existing attribution: only a
  non-empty, already validated ``track.artist`` can replace the stored artist.
* The sidecar is written atomically after tagging; if every tag attempt fails
  the sidecar is left alone so it cannot claim metadata the file does not have.
* No media request is ever made here — this module only reads local bytes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import tagging as _tagging
from .library import LibraryEntry, LibraryIndex
from .models import TrackInfo
from .sidecar import build_sidecar, read_sidecar, sidecar_path, write_sidecar
from .tagging import (
    AUDIO_SUFFIXES,
    read_tags,
    tag_file,
    write_lyrics_sidecar,
)

logger = logging.getLogger(__name__)

# Fields reconciled from freshly discovered public metadata. Deliberately
# narrow: identity and description only, so a repair cannot rewrite provenance
# (``handle``, ``source``, ``downloaded_at``, ``saved_files``).
_COMPARED = (
    "title",
    "artist",
    "creator_handle",
    "duration_sec",
    "model_name",
    "model_version",
    "tags",
    "is_public",
)


@dataclass(frozen=True)
class MetadataPlan:
    """A proposed metadata repair for one archived recording."""

    clip_id: str
    entry: LibraryEntry
    changes: dict[str, tuple[Any, Any]]
    proposed: dict[str, Any] = field(repr=False)
    media: tuple[Path, ...] = ()
    unresolved_producer: bool = False

    @property
    def retag_required(self) -> bool:
        return "title" in self.changes or "artist" in self.changes

    def describe(self) -> str:
        return "; ".join(f"{name}: {old!r} -> {new!r}" for name, (old, new) in self.changes.items())


def plan_metadata(track: TrackInfo, entry: LibraryEntry, handle: str) -> MetadataPlan | None:
    """Diff ``track``'s public metadata against its archived sidecar.

    Returns ``None`` when the recording has no readable sidecar (nothing safe
    to compare, and inventing one would be a guess) or when nothing differs.
    """
    record = read_sidecar(entry.sidecar)
    if record is None:
        return None
    stored_files = [name for name in record["saved_files"] if isinstance(name, str)]
    downloaded_at = record.get("downloaded_at")
    source = record.get("source")
    proposed = build_sidecar(
        track,
        handle=handle,
        saved_files=stored_files,
        downloaded_at=downloaded_at if isinstance(downloaded_at, str) else None,
        source=source if isinstance(source, str) else "existing",
    )
    # A field the fresh payload does not carry is *unknown*, not empty: keep
    # whatever the archive already recorded instead of erasing it.
    for name in _COMPARED:
        if proposed.get(name) is None and record.get(name) is not None:
            proposed[name] = record[name]

    changes: dict[str, tuple[Any, Any]] = {}
    known_producer = isinstance(track.artist, str) and bool(track.artist.strip())
    unresolved = bool(record.get("artist")) and not known_producer
    for name in _COMPARED:
        old = record.get(name)
        new = proposed.get(name)
        # Unknown or empty producer: preserve existing attribution.
        if name == "artist" and not known_producer:
            continue
        if old == new:
            continue
        if name == "title" and (not isinstance(new, str) or not new.strip()):
            continue
        changes[name] = (old, new)
    if not changes:
        return None
    media = tuple(
        path for path in entry.media if path.suffix.lower() in AUDIO_SUFFIXES and path.is_file()
    )
    return MetadataPlan(track.id.lower(), entry, changes, proposed, media, unresolved)


def _stored_lyrics(media: tuple[Path, ...]) -> str | None:
    """Existing lyrics tag, so a repair never strips lyrics it did not fetch."""
    for path in media:
        tags = read_tags(path)
        value = tags.get("lyrics") or tags.get("\xa9lyr")
        if value:
            return value
    return None


def apply_metadata(
    plan: MetadataPlan,
    *,
    dry_run: bool,
    sidecar: bool,
    lyrics_sidecar: str | None,
    stats: dict[str, int],
    echo: Callable[[str], None] = print,
) -> str:
    """Tag and rewrite one recording's metadata. Returns ``updated``/``failed``.

    Direct callers are responsible for independently verifying any producer
    in the supplied plan before allowing this helper to write.

    Mutagen edits a private copy before replacing the media. An ordinary tag
    failure is counted as ``metadata_failed`` and does not update the sidecar.
    A replacement with uncertain durability raises TagPublicationUncertain and
    aborts this workflow before the sidecar write; the media must be inspected.
    """
    record = read_sidecar(plan.entry.sidecar)
    if record is None:
        stats["metadata_failed"] = stats.get("metadata_failed", 0) + 1
        echo(f"[metadata] {plan.clip_id}: unreadable sidecar, nothing changed")
        return "failed"
    if dry_run:
        reason = (
            _tagging._embedded_tag_write_guard() if plan.retag_required and plan.media else None
        )
        if reason:
            echo(f"[dry-run] {plan.entry.sidecar.name}: {reason}")
            stats["metadata_failed"] = stats.get("metadata_failed", 0) + 1
            return "failed"
        echo(f"[dry-run] {plan.entry.sidecar.name}: {plan.describe()}")
        stats["metadata_updated"] = stats.get("metadata_updated", 0) + 1
        return "updated"

    if plan.retag_required:
        artist = str(plan.proposed.get("artist") or record.get("artist") or "")
        title = str(plan.proposed.get("title") or record.get("title") or "")
        lyrics = _stored_lyrics(plan.media)
        tagged_ok = 0
        for path in plan.media:
            result = tag_file(path, artist=artist, title=title, lyrics=lyrics)
            if result.tagged:
                tagged_ok += 1
                stats["tagged"] = stats.get("tagged", 0) + 1
            else:
                logger.warning("Could not retag %s: %s", path.name, result.reason)
        if plan.media and tagged_ok == 0:
            stats["metadata_failed"] = stats.get("metadata_failed", 0) + 1
            echo(f"[metadata] {plan.clip_id}: tagging failed, audio and sidecar unchanged")
            return "failed"

    if sidecar and not write_sidecar(sidecar_path(plan.entry.base), plan.proposed, overwrite=True):
        stats["metadata_failed"] = stats.get("metadata_failed", 0) + 1
        return "failed"
    if sidecar:
        stats["sidecars"] = stats.get("sidecars", 0) + 1
    if lyrics_sidecar:
        lyrics = _stored_lyrics(plan.media)
        for path in plan.media:
            if lyrics:
                write_lyrics_sidecar(path, lyrics, lyrics_sidecar)
    stats["metadata_updated"] = stats.get("metadata_updated", 0) + 1
    echo(f"[metadata] {plan.entry.sidecar.name}: {plan.describe()}")
    return "updated"


def reconcile(
    tracks: list[TrackInfo],
    index: LibraryIndex,
    *,
    handle: str,
    dry_run: bool,
    sidecar: bool,
    lyrics_sidecar: str | None,
    stats: dict[str, int],
    echo: Callable[[str], None] = print,
) -> dict[str, int]:
    """Repair metadata for every track already archived; report the rest.

    Used by ``--metadata-only`` (never downloads) and by ``--update-existing``
    for the already-owned subset of an otherwise ordinary sync. A missing clip
    is counted as ``metadata_missing`` and left alone.
    """
    for track in tracks:
        entry = index.get(track.id)
        if entry is None:
            stats["metadata_missing"] = stats.get("metadata_missing", 0) + 1
            if dry_run:
                echo(f"[dry-run] {track.id}: not archived; metadata mode downloads nothing")
            continue
        plan = plan_metadata(track, entry, handle)
        if plan is None:
            stats["metadata_unchanged"] = stats.get("metadata_unchanged", 0) + 1
            continue
        if plan.unresolved_producer:
            stats["metadata_unresolved"] = stats.get("metadata_unresolved", 0) + 1
        apply_metadata(
            plan,
            dry_run=dry_run,
            sidecar=sidecar,
            lyrics_sidecar=lyrics_sidecar,
            stats=stats,
            echo=echo,
        )
    return stats
