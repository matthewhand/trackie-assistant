"""Public playlist discovery and resumable bulk imports (#160).

Why this exists
---------------
A profile feed lists clips *and* playlists, but the downloader only ever read
the clips. Public playlists therefore hid recordings that were not (or no
longer) on the profile: at the observed snapshot nine newer playlist pages held
283 song occurrences / 268 unique UUIDs while the profile itself offered far
fewer. This module expands a profile into its **explicitly public** playlists,
reads each playlist's ordered membership, and keeps a durable checkpoint so a
bulk import resumes instead of restarting.

Rules that are not negotiable
-----------------------------
* Only public data is read: the anonymous profile feed and public playlist
  pages. The sole API exception is an unauthenticated GET to the exact
  https://studio-api.prod.suno.com/api/playlist/{uuid}/?page=N endpoint
  for membership only. Each request uses a fresh client, no cookies or auth,
  exact-host HTTPS validation, no redirects, a byte cap and bounded paging.
  No other API path is permitted. Hidden, private, unlisted, trashed or deleted
  listings and clips fail closed.
* A listing that yields no song ids is **unresolved**, never "zero songs",
  unless the page explicitly reports zero or the exact API fallback verifies
  an empty public membership. Partial or ambiguous results never complete.
* Work is bounded: pages, playlist count and per-playlist tracks all have hard
  caps, and every request goes through the caller's rate limiter and response
  cap.
* Checkpoints are bookkeeping only. They live under ``.library/`` next to the
  membership index from #158, are written atomically, and are never rewritten
  when nothing changed.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .discovery import _canonical_handle, _creator_from_feed_item
from .models import TrackInfo
from .parser import parse_playlist
from .playlist_api import PUBLIC_PLAYLIST_API_MAX_PAGES, PublicPlaylistApiError
from .urls import UUID_RE, song_page_url

logger = logging.getLogger(__name__)

PLAYLIST_URL_TEMPLATE = "https://suno.com/playlist/{playlist_id}"

# Hard caps. A profile with more playlists than this keeps the first
# ``MAX_PLAYLISTS`` in feed order (newest first) and says so; the cap exists so
# a hostile or broken pagination cannot turn one sync into an unbounded crawl.
MAX_PLAYLISTS = 200
MAX_DISCOVERY_PAGES = 20
MAX_PLAYLIST_TRACKS = 5000

CHECKPOINT_SCHEMA = 1
CHECKPOINT_DIR = ".library"
CHECKPOINT_FILE = "playlist-checkpoint.json"

_PLAYLIST_TOTAL_RE = re.compile(r"(\d[\d,]*)\s+songs?\b", re.IGNORECASE)

# Visibility markers that mean "do not touch". Anything else (including a
# missing marker) is treated as the ordinary public listing the feed already
# promised, because the entry came from a public profile.
_NOT_PUBLIC_VALUES = frozenset(
    {"private", "unlisted", "hidden", "trashed", "deleted", "draft", "archived"}
)


@dataclass(frozen=True)
class PlaylistRef:
    """One public playlist as the feed describes it."""

    id: str
    name: str
    owner: str | None = None
    track_count: int | None = None

    @property
    def url(self) -> str:
        return PLAYLIST_URL_TEMPLATE.format(playlist_id=self.id)

    @property
    def signature(self) -> str:
        """What makes a checkpoint entry stale: a rename or a count change."""
        count = "?" if self.track_count is None else str(self.track_count)
        return f"{self.name}|{count}"


@dataclass(frozen=True)
class PlaylistDiscovery:
    """The public playlists a profile advertises, plus what was skipped."""

    playlists: tuple[PlaylistRef, ...]
    pages: int = 0
    reported_total: int | None = None
    skipped_not_public: int = 0
    skipped_invalid: int = 0
    truncated: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class PlaylistMembership:
    """One playlist's ordered membership, or why it could not be read."""

    playlist: PlaylistRef
    tracks: tuple[TrackInfo, ...] = ()
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.reason is None

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(track.id for track in self.tracks)


@dataclass
class PlaylistExpansion:
    """Tracks contributed by playlists, with the counts a run summary reports."""

    tracks: list[TrackInfo] = field(default_factory=list)
    playlists_found: int = 0
    playlists_expanded: int = 0
    playlists_skipped: int = 0
    playlists_unresolved: int = 0
    unresolved: list[tuple[str, str]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "playlists_found": self.playlists_found,
            "playlists_expanded": self.playlists_expanded,
            "playlists_skipped": self.playlists_skipped,
            "playlists_unresolved": self.playlists_unresolved,
            "playlist_tracks": len(self.tracks),
        }


def _playlist_id(value: object) -> str | None:
    """A playlist id must be a real UUID; anything else is not selectable."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if UUID_RE.fullmatch(candidate) else None


def _playlist_item_public(item: dict[str, Any]) -> bool:
    """False only on an explicit non-public marker.

    ``is_public: false`` is honoured, and so is any visibility string naming a
    non-public state. A missing field is not treated as "private": the entry
    came from the anonymous public feed, and inventing an exclusion there would
    silently drop public playlists.
    """

    def non_public_markers(value: dict[str, Any]) -> bool:
        public = value.get("is_public")
        if public is False or (isinstance(public, str) and public.strip().lower() == "false"):
            return True
        for key in ("is_private", "is_hidden", "is_trashed", "hidden", "trashed", "deleted"):
            marker = value.get(key)
            if marker is True or (isinstance(marker, str) and marker.strip().lower() == "true"):
                return True
        for key in ("visibility", "privacy", "status", "state", "value"):
            marker = value.get(key)
            if isinstance(marker, str) and marker.strip().lower() in _NOT_PUBLIC_VALUES:
                return True
        return False

    if non_public_markers(item):
        return False
    for key in ("visibility", "privacy", "status", "state"):
        nested = item.get(key)
        if isinstance(nested, dict) and non_public_markers(nested):
            return False
    return True


def _owner(item: dict[str, Any]) -> str | None:
    """The producer's handle/display name, flattened or nested."""
    for source in (item, item.get("user"), item.get("author"), item.get("owner")):
        if not isinstance(source, dict):
            continue
        for key in ("handle", "display_name"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                # Same discipline as clip identity: never keep control,
                # separator or bidi characters that came from the server.
                if len(value) > 256 or any(
                    ord(ch) < 32 or 127 <= ord(ch) <= 159 or ch in "/\\?#%" for ch in value
                ):
                    continue
                return value.strip().lstrip("@")
    return None


def _name(item: dict[str, Any], playlist_id: str) -> str:
    for key in ("name", "title", "playlist_name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return f"playlist {playlist_id[:8]}"


def _count(item: dict[str, Any]) -> int | None:
    """Read recognized public-feed totals; reject contradictory count fields."""
    counts = []
    for key in (
        "num_total_clips",
        "clip_count",
        "track_count",
        "num_clips",
        "count",
        "song_count",
        "num_total_results",
    ):
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts.append(value)
    if len(set(counts)) > 1:
        raise ValueError("conflicting public playlist totals")
    return counts[0] if counts else None


def _collect_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Playlist entries from every payload shape observed or foreseeable."""
    candidates: list[Any] = []
    for key in ("playlists", "playlist_list", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    # A nested container (``{"data": {"playlists": [...]}}``) is common in
    # sibling endpoints; descend one level rather than inventing a schema.
    for key in ("data", "results", "profile"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            for inner in ("playlists", "playlist_list", "items"):
                value = nested.get(inner)
                if isinstance(value, list):
                    candidates.extend(value)
    return [item for item in candidates if isinstance(item, dict)]


def _reported_total(payload: dict[str, Any]) -> int | None:
    for key in ("num_total_playlists", "total_playlists", "playlist_count"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


async def discover_profile_playlists(
    fetch_page: Callable[[int], Awaitable[dict[str, Any]]],
    *,
    max_pages: int = MAX_DISCOVERY_PAGES,
    max_playlists: int = MAX_PLAYLISTS,
) -> PlaylistDiscovery:
    """Page the profile feed and return every *public* playlist.

    ``fetch_page(page)`` is the caller's bounded, paced request; this function
    only interprets the payload. Pagination stops on an empty page, when the
    reported total is reached, or at ``max_pages`` — whichever comes first.
    """
    seen: dict[str, PlaylistRef] = {}
    pages = 0
    reported_total: int | None = None
    skipped_not_public = 0
    skipped_invalid = 0
    truncated = False
    total_conflict = False
    reason = None

    for page in range(1, max_pages + 1):
        payload = await fetch_page(page)
        pages = page
        if not isinstance(payload, dict):
            payload = {}
        total = _reported_total(payload)
        if total is not None and not total_conflict:
            if reported_total is not None and reported_total != total:
                logger.warning("Profile feed reported inconsistent playlist totals")
                total_conflict = True
                reported_total = None
                truncated = True
            else:
                reported_total = total
        items = _collect_items(payload)
        if not items:
            break
        before = len(seen)
        for item in items:
            playlist_id = _playlist_id(item.get("id") or item.get("playlist_id"))
            if playlist_id is None:
                skipped_invalid += 1
                continue
            if not _playlist_item_public(item):
                skipped_not_public += 1
                continue
            if playlist_id in seen:
                continue
            try:
                track_count = _count(item)
            except ValueError:
                skipped_invalid += 1
                logger.warning("Skipping playlist %s with conflicting feed totals", playlist_id)
                continue
            seen[playlist_id] = PlaylistRef(
                id=playlist_id,
                name=_name(item, playlist_id),
                owner=_owner(item),
                track_count=track_count,
            )
            if len(seen) >= max_playlists:
                truncated = truncated or (total is not None and total > len(seen))
                break
        if len(seen) >= max_playlists:
            break
        if reported_total is not None and len(seen) >= reported_total:
            break
        if page > 1 and len(seen) == before and any(_playlist_item_public(x) for x in items):
            truncated = True
            reason = (
                "Suno repeats the same playlist list; add explicit playlist URLs to ensure coverage"
            )
            break
    else:
        truncated = True
        reason = "Playlist discovery reached its page limit; complete coverage is unverified"

    return PlaylistDiscovery(
        playlists=tuple(seen.values()),
        pages=pages,
        reported_total=reported_total,
        skipped_not_public=skipped_not_public,
        skipped_invalid=skipped_invalid,
        truncated=truncated,
        reason=reason,
    )


def _stated_song_count(html: str) -> int | None:
    """The page's own "N songs" statement, if it makes one."""
    match = _PLAYLIST_TOTAL_RE.search(html)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:  # pragma: no cover - regex only matches digits
        return None


async def _fetch_public_api_membership(
    fetch_api_page: Callable[[PlaylistRef, int], Awaitable[dict[str, Any]]],
    playlist: PlaylistRef,
    *,
    on_excluded: Callable[[str], None] | None = None,
) -> tuple[TrackInfo, ...]:
    """Fetch and verify every public song in a playlist, or reject the whole result."""
    tracks: list[TrackInfo] = []
    seen_ids: set[str] = set()
    expected_total: int | None = None

    for page in range(1, PUBLIC_PLAYLIST_API_MAX_PAGES + 1):
        payload = await fetch_api_page(playlist, page)
        if not isinstance(payload, dict):
            raise PublicPlaylistApiError("unexpected playlist response shape")
        if _playlist_id(payload.get("id")) != playlist.id:
            raise PublicPlaylistApiError(
                "playlist response identity did not match the requested playlist"
            )
        if (
            payload.get("is_public") is not True
            or payload.get("is_hidden") is not False
            or payload.get("is_trashed") is not False
            or not _playlist_item_public(payload)
        ):
            raise PublicPlaylistApiError("playlist is not explicitly public and visible")

        current_page = payload.get("current_page")
        if (
            isinstance(current_page, bool)
            or not isinstance(current_page, int)
            or current_page != page
        ):
            raise PublicPlaylistApiError("playlist response page did not match the requested page")
        total = payload.get("num_total_results")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise PublicPlaylistApiError("playlist response omitted a valid total count")
        if total > MAX_PLAYLIST_TRACKS:
            raise PublicPlaylistApiError("playlist exceeds the membership track cap")
        if expected_total is None:
            expected_total = total
            if playlist.track_count is not None and total != playlist.track_count:
                raise PublicPlaylistApiError(
                    "playlist total did not match the public profile listing"
                )
        elif total != expected_total:
            raise PublicPlaylistApiError("playlist total changed during pagination")

        entries = payload.get("playlist_clips")
        if not isinstance(entries, list):
            raise PublicPlaylistApiError("playlist response omitted its membership list")
        if total == 0:
            if page != 1 or entries:
                raise PublicPlaylistApiError(
                    "empty playlist response contained unexpected membership"
                )
            return ()

        if not entries:
            raise PublicPlaylistApiError("playlist membership ended before its reported total")

        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("clip"), dict):
                raise PublicPlaylistApiError("playlist membership contained a malformed clip")
            clip = entry["clip"]
            if clip.get("entity_type") != "song_schema":
                raise PublicPlaylistApiError("playlist contained an unsupported clip schema")
            clip_id = _playlist_id(clip.get("id"))
            if clip_id is None:
                raise PublicPlaylistApiError("playlist contained an invalid song UUID")
            if clip_id in seen_ids:
                raise PublicPlaylistApiError("playlist membership contained a duplicate song UUID")
            seen_ids.add(clip_id)
            if len(seen_ids) > total:
                raise PublicPlaylistApiError("playlist returned more clips than its reported total")
            if (
                clip.get("is_public") is not True
                or clip.get("is_hidden") is not False
                or clip.get("is_trashed") is not False
                or not _playlist_item_public(clip)
            ):
                if on_excluded is None:
                    raise PublicPlaylistApiError(
                        "playlist contained a non-public or unsupported clip"
                    )
                on_excluded(clip_id)
                continue

            title = clip.get("title")
            if not isinstance(title, str) or not title.strip():
                title = "Untitled"
            # Membership discovery must not import media URLs from this API.
            # Later, the ordinary song-page flow resolves media if the user
            # explicitly runs a download; this fallback itself is metadata-only.
            tracks.append(
                TrackInfo(
                    id=clip_id,
                    title=title.strip(),
                    artist=_creator_from_feed_item(clip, ""),
                    creator_handle=_canonical_handle(clip),
                    from_playlist=True,
                    page_url=song_page_url(clip_id),
                    is_public=True,
                )
            )
            if len(tracks) > total:
                raise PublicPlaylistApiError("playlist returned more clips than its reported total")

        if len(seen_ids) == total:
            return tuple(tracks)

    raise PublicPlaylistApiError("playlist membership exceeded the pagination page budget")


async def fetch_playlist_membership(
    fetch_html: Callable[[str], Awaitable[str]],
    playlist: PlaylistRef,
    fetch_api_page: Callable[[PlaylistRef, int], Awaitable[dict[str, Any]]] | None = None,
) -> PlaylistMembership:
    """Read public membership, using one tightly scoped anonymous API fallback.

    HTML links or unanchored page count text alone do not prove completeness.
    A non-empty HTML result requires a valid count from the public profile
    feed, with agreement against any page count. Otherwise the optional
    exact-endpoint API fallback must verify identity, visibility, every clip
    UUID, pagination and the full count. Any ambiguity remains unresolved
    rather than becoming a false completion or false zero.
    """
    try:
        html = await fetch_html(playlist.url)
    except Exception as exc:  # noqa: BLE001 - one playlist must not abort a run
        reason = f"fetch failed ({exc.__class__.__name__})"
        logger.warning("Playlist %r unreadable: %s", playlist.name, reason)
        return PlaylistMembership(playlist=playlist, reason=reason)

    tracks = parse_playlist(html)
    stated = _stated_song_count(html)
    if len(tracks) > MAX_PLAYLIST_TRACKS:
        reason = "public HTML membership exceeds the per-playlist track cap"
        logger.warning("Playlist %r unresolved: %s", playlist.name, reason)
        return PlaylistMembership(playlist=playlist, reason=reason)

    # Only a public profile-feed count is independent evidence that the
    # HTML links are complete. Unanchored "N songs" page text is not proof.
    html_matches = stated is None or stated == len(tracks)
    profile_matches = playlist.track_count == len(tracks)
    if tracks and profile_matches and html_matches:
        return PlaylistMembership(playlist=playlist, tracks=tuple(tracks))

    if not tracks and stated == 0 and playlist.track_count == 0 and fetch_api_page is None:
        return PlaylistMembership(playlist=playlist)

    if fetch_api_page is not None:
        try:
            # This API has independently verified playlist identity, visibility,
            # pagination and the feed total. An unanchored HTML "N songs"
            # phrase is not trusted evidence that can veto those results.
            api_tracks = await _fetch_public_api_membership(fetch_api_page, playlist)
        except Exception as exc:  # noqa: BLE001 - fail closed per playlist
            detail = str(exc) if isinstance(exc, PublicPlaylistApiError) else exc.__class__.__name__
            reason = f"public playlist API fallback unresolved ({detail})"
            logger.warning("Playlist %r unresolved: %s", playlist.name, reason)
            return PlaylistMembership(playlist=playlist, reason=reason)
        return PlaylistMembership(playlist=playlist, tracks=api_tracks)

    if tracks:
        reason = f"page reports {stated} songs but exposed {len(tracks)} ids"
    else:
        reason = (
            f"page reports {stated} songs but exposed no ids"
            if stated
            else "no public song ids in the page"
        )
    logger.warning("Playlist %r unresolved: %s", playlist.name, reason)
    return PlaylistMembership(playlist=playlist, reason=reason)


def checkpoint_path(root: Path) -> Path:
    return Path(root) / CHECKPOINT_DIR / CHECKPOINT_FILE


class PlaylistCheckpoint:
    """Durable, idempotent per-playlist progress for a bulk import.

    A complete entry records verified membership, not completed downloads.
    Writes are atomic and skipped when unchanged. A fresh sync must revalidate
    membership: the public feed offers no reliable revision marker, and an
    earlier run may have stopped before its recordings were downloaded.
    """

    def __init__(self, root: Path) -> None:
        self.path = checkpoint_path(root)
        self._data = self._load()
        self._dirty = False
        self.saved = False

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema": CHECKPOINT_SCHEMA, "playlists": {}}
        if not isinstance(data, dict) or not isinstance(data.get("playlists"), dict):
            return {"schema": CHECKPOINT_SCHEMA, "playlists": {}}
        return data

    @property
    def entries(self) -> dict[str, Any]:
        entries = self._data.get("playlists")
        return entries if isinstance(entries, dict) else {}

    def entry(self, playlist: PlaylistRef) -> dict[str, Any] | None:
        value = self.entries.get(playlist.id)
        return value if isinstance(value, dict) else None

    def is_complete(self, playlist: PlaylistRef) -> bool:
        """Whether matching membership was verified, not whether tracks downloaded."""
        entry = self.entry(playlist)
        return bool(
            entry
            and entry.get("status") in ("complete", "membership_verified")
            and entry.get("signature") == playlist.signature
        )

    def mark_complete(self, playlist: PlaylistRef, tracks: int) -> None:
        self._set(
            playlist,
            status="membership_verified",
            tracks=tracks,
            reason=None,
        )

    def mark_unresolved(self, playlist: PlaylistRef, reason: str) -> None:
        self._set(playlist, status="unresolved", tracks=0, reason=reason)

    def _set(self, playlist: PlaylistRef, *, status: str, tracks: int, reason: str | None) -> None:
        entry = {
            "name": playlist.name,
            "owner": playlist.owner,
            "signature": playlist.signature,
            "status": status,
            "tracks": tracks,
            "reason": reason,
        }
        current = self.entries.get(playlist.id)
        if (
            isinstance(current, dict)
            and {k: v for k, v in current.items() if k != "updated_at"} == entry
        ):
            return
        entry["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.entries[playlist.id] = entry
        self._data["schema"] = CHECKPOINT_SCHEMA
        self._dirty = True

    def save(self) -> bool:
        """Write when dirty; returns whether the file changed."""
        if not self._dirty:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".part")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            # The checkpoint is archive metadata, like a sidecar: readable by
            # other tooling on the same share.
            tmp.chmod(0o644)
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("Could not write playlist checkpoint: %s", exc)
            return False
        self._dirty = False
        self.saved = True
        return True


async def expand_public_playlists(
    fetch_page: Callable[[int], Awaitable[dict[str, Any]]],
    fetch_html: Callable[[str], Awaitable[str]],
    *,
    checkpoint: PlaylistCheckpoint,
    fetch_api_page: Callable[[PlaylistRef, int], Awaitable[dict[str, Any]]] | None = None,
    known_ids: Iterable[str] = (),
    max_pages: int = MAX_DISCOVERY_PAGES,
    max_playlists: int = MAX_PLAYLISTS,
    max_tracks: int = MAX_PLAYLIST_TRACKS,
    on_membership: Callable[[PlaylistRef, Sequence[str]], None] | None = None,
    persist: bool = True,
) -> PlaylistExpansion:
    """Expand a profile into public playlists, revalidating membership each run.

    ``known_ids`` are clips the caller already has (profile tracks), so the
    returned list only contains what the playlists add. ``on_membership`` is
    called with the ordered ids of every resolved playlist — that is where
    separate membership recording belongs. ``persist=False`` (dry-run) reads
    the checkpoint but never writes it, so a preview cannot mask a real run.
    """
    discovery = await discover_profile_playlists(
        fetch_page, max_pages=max_pages, max_playlists=max_playlists
    )
    expansion = PlaylistExpansion(playlists_found=len(discovery.playlists))
    if discovery.truncated:
        expansion.playlists_unresolved += 1
        expansion.unresolved.append(
            ("Profile playlist discovery", discovery.reason or "Playlist listing is incomplete")
        )
    seen = {clip.lower() for clip in known_ids}

    for playlist in discovery.playlists:
        # The checkpoint records discovery, not successful downloads. Always
        # recheck membership: an interrupted run may not have saved the tracks,
        # and an unchanged count does not prove an unchanged playlist.
        if len(expansion.tracks) >= max_tracks:
            expansion.playlists_unresolved += 1
            expansion.unresolved.append((playlist.name, "track cap reached"))
            break
        membership = await fetch_playlist_membership(fetch_html, playlist, fetch_api_page)
        if not membership.resolved:
            expansion.playlists_unresolved += 1
            expansion.unresolved.append((playlist.name, membership.reason or "unresolved listing"))
            if persist:
                checkpoint.mark_unresolved(playlist, membership.reason or "unresolved listing")
            continue

        additional = len({track.id for track in membership.tracks if track.id not in seen})
        if len(expansion.tracks) + additional > max_tracks:
            reason = "global playlist track cap would be exceeded"
            expansion.playlists_unresolved += 1
            expansion.unresolved.append((playlist.name, reason))
            if persist:
                checkpoint.mark_unresolved(playlist, reason)
            continue

        expansion.playlists_expanded += 1
        if on_membership is not None:
            # Resolved and empty is a real answer: record the empty membership
            # rather than leaving a stale one behind.
            on_membership(playlist, membership.ids)
        for track in membership.tracks:
            if track.id in seen:
                continue
            seen.add(track.id)
            expansion.tracks.append(track)
        if persist:
            checkpoint.mark_complete(playlist, len(membership.tracks))

    if persist:
        checkpoint.save()
    return expansion
