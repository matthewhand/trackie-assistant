"""Click entry point for suno-dl."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import re
import signal
import sys
import threading
import unicodedata
from collections.abc import Coroutine, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click

from .archive_lock import archive_locked_if_capped, raise_if_unsupported_archive_write
from .blocklist import load_blocked_ids
from .config import Config, ensure_dotenv_loaded, load_config
from .discovery import (
    BrowserDependencyMissing,
    ChromiumExecutableMissing,
    discover_profile_tracks,
    fetch_profile_feed_page,
)
from .download_failures import RETRY_PREFIX
from .download_intent import UnsafeDownloadIntent, WindowsArchiveWriteUnavailable
from .downloader import download_tracks, resolve_output_dir
from .errors import (
    BlockedError,
    ConfigError,
    DiskSpaceError,
    DownloadError,
    FetchError,
    ResolutionError,
    SunoError,
)
from .fetcher import Fetcher
from .library import record_membership
from .linux_downloads import require_audio_sync
from .models import TrackInfo
from .naming import normalize_watch_entry, source_slug
from .network_styles import (
    NetworkStyleAnalysis,
    analyze_network_styles,
    dump_csv,
    dump_json,
    fetch_network_profiles,
    load_catalogue,
    print_summary,
)
from .pacing import PlaybackPacer
from .parser import parse_playlist as parse_playlist
from .parser import parse_song
from .playlist_api import fetch_public_playlist_api_page
from .playlists import (
    PlaylistCheckpoint,
    PlaylistRef,
    _fetch_public_api_membership,
    expand_public_playlists,
)
from .producer_lookup import ProducerLookupError, _require_trusted_framing
from .producer_migration import MigrationError
from .tagging import (
    TaggingUnavailable,
    TagPublicationUncertain,
    retag_tree,
)
from .urls import UUID_RE, redact_url, song_page_url

URL_RE = re.compile(r"^https://suno\.com/(?:@[^/]+|playlist/[^/]+|song/[^/]+)/?$", re.I)

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_NO_TRACKS = 2


def _retag(root: Path, artist: str, dry_run: bool, lyrics_sidecar: str | None = None) -> int:
    """One-shot retag of an existing archive (artist/title tag migration).

    No network access and no config are needed: the artist comes from the
    filename (or ``--artist``), the title from the basename, and files that
    already carry a title tag are left alone.

    If ``lyrics_sidecar`` is "lrc" or "txt", also write sidecar lyrics files
    for tracks that have lyrics in their tags.
    """
    if not root.is_dir():
        raise click.ClickException(f"--retag: not a directory: {root}")
    stats = retag_tree(root, artist=artist, dry_run=dry_run, lyrics_sidecar=lyrics_sidecar)
    click.echo(
        f"Retag summary: scanned={stats['scanned']} "
        f"tagged={stats['tagged']} "
        f"already_tagged={stats['skipped']} "
        f"failed={stats['failed']}"
    )
    if dry_run and stats["failed"]:
        click.echo(
            "Some requested tag changes are blocked until metadata preservation is verified."
        )
    return EXIT_PARTIAL if stats["failed"] else EXIT_OK


@archive_locked_if_capped
async def _retag_locked(
    output_dir: Path,
    dry_run: bool,
    artist: str,
    lyrics_sidecar: str | None = None,
    target_dir: Path | None = None,
) -> int:
    target = (target_dir or output_dir).expanduser().resolve()
    if not target.is_relative_to(output_dir.expanduser().resolve()):
        raise click.ClickException("--retag target must be inside the declared archive root")
    return _retag(target, artist, dry_run, lyrics_sidecar)


def _url_kind(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    if path.startswith("/@"):
        return "profile"
    if path.startswith("/playlist/"):
        return "playlist"
    if path.startswith("/song/"):
        return "song"
    raise click.ClickException(
        "URL must be a public Suno profile, playlist, or song URL "
        "(https://suno.com/@handle, /playlist/<id>, or /song/<id>)."
    )


def _make_fetcher(cfg: Config, rate_limit_sec: float | None = None) -> Fetcher:
    """Build a Fetcher from config (rate limit and UA are optional).

    ``rate_limit_sec`` overrides the configured value (``--rate-limit``);
    ``0`` explicitly disables pacing.
    """
    return Fetcher(
        cfg.timeout_sec,
        cfg.max_retries,
        rate_limit_sec=cfg.rate_limit_sec if rate_limit_sec is None else rate_limit_sec,
        user_agent=cfg.user_agent or None,
    )


async def _expand_profile_playlists(
    cfg: Config,
    handle: str,
    tracks: list[TrackInfo],
    output_dir: Path,
    dry_run: bool,
    rate_limit: float | None,
) -> tuple[list[TrackInfo], dict[str, int]]:
    """Add public playlist membership to a profile's tracks (#160).

    Membership is recorded separately from the audio (#158), so one recording
    can belong to many playlists. ``--dry-run`` reads the checkpoint but never
    writes it and never records membership, so a preview cannot make a later
    real run skip work it never did.
    """
    root = resolve_output_dir(output_dir, None)
    checkpoint = PlaylistCheckpoint(root)

    def on_membership(playlist: PlaylistRef, ids: Sequence[str]) -> None:
        if not dry_run:
            record_membership(root, f"playlist:{playlist.id}", ids)

    async with _make_fetcher(cfg, rate_limit) as fetcher:

        async def fetch_page(page: int) -> dict:
            return await fetch_profile_feed_page(
                fetcher.client, handle, page, cfg.timeout_sec, fetcher.rate_limiter
            )

        async def fetch_html(url: str) -> str:
            return await fetcher.fetch_html(url)

        async def fetch_api_page(playlist: PlaylistRef, page: int) -> dict[str, Any]:
            return await fetch_public_playlist_api_page(
                playlist.id,
                page,
                timeout_sec=cfg.timeout_sec,
                rate_limiter=fetcher.rate_limiter,
            )

        async def complete_discovery():
            from dataclasses import replace

            from .playlists import discover_profile_playlists
            from .profile_playlists import (
                discover_cursor_playlists,
                fetch_public_profile_playlists_page,
            )

            async def cursor_page(user_id, cursor):
                return await fetch_public_profile_playlists_page(
                    user_id,
                    cursor,
                    timeout_sec=cfg.timeout_sec,
                    rate_limiter=fetcher.rate_limiter,
                )

            try:
                return await discover_cursor_playlists(fetch_html, cursor_page, handle)
            except Exception as exc:
                # Preserve bounded legacy discovery, but never claim its
                # repeated static playlist list proves complete coverage.
                fallback = await discover_profile_playlists(fetch_page)
                return replace(
                    fallback,
                    truncated=True,
                    reason=f"Complete public playlist listing unavailable ({type(exc).__name__})",
                )

        expansion = await expand_public_playlists(
            fetch_page,
            fetch_html,
            checkpoint=checkpoint,
            fetch_api_page=fetch_api_page,
            known_ids=[track.id for track in tracks],
            on_membership=on_membership,
            persist=not dry_run,
            discover_playlists=complete_discovery,
        )

    click.echo(
        "Playlists: "
        f"found={expansion.playlists_found} "
        f"expanded={expansion.playlists_expanded} "
        f"skipped={expansion.playlists_skipped} "
        f"unresolved={expansion.playlists_unresolved} "
        f"tracks={len(expansion.tracks)} "
        f"excluded={len(expansion.excluded_ids)}"
    )
    # Name the listings that could not be read: an unresolved playlist is a
    # gap in the archive, and silence would read as "that playlist was empty".
    for name, reason in expansion.unresolved[:10]:
        click.echo(f"  unresolved playlist {name!r}: {reason}", err=True)
    if len(expansion.unresolved) > 10:
        click.echo(
            f"  ... and {len(expansion.unresolved) - 10} more unresolved playlist(s)",
            err=True,
        )
    combined = [t for t in tracks + expansion.tracks if t.id not in expansion.excluded_ids]
    return combined, expansion.counts()


async def _source_playlist_tracks(fetcher: Fetcher, url: str) -> list[TrackInfo]:
    playlist_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    if not UUID_RE.fullmatch(playlist_id):
        raise ValueError("Invalid selected playlist UUID")

    async def page(ref: PlaylistRef, number: int) -> dict[str, Any]:
        return await fetch_public_playlist_api_page(
            ref.id, number, timeout_sec=fetcher.timeout, rate_limiter=fetcher.rate_limiter
        )

    excluded: set[str] = set()
    tracks = await _fetch_public_api_membership(
        page,
        PlaylistRef(id=playlist_id, name=playlist_id),
        on_excluded=excluded.add,
    )
    if excluded:
        click.echo(f"Excluded {len(excluded)} non-public or visibility-ambiguous playlist clips.")
    return list(tracks)


def _preflight_source_before_lock(func):
    """Reject disabled source work before creating an archive or lockfile."""
    signature = inspect.signature(func)

    @functools.wraps(func)
    async def guarded(*args, **kwargs):
        values = signature.bind(*args, **kwargs)
        values.apply_defaults()
        if not values.arguments["dry_run"]:
            raise_if_unsupported_archive_write()
        _require_trusted_framing()
        require_audio_sync(**values.arguments)
        return await func(*args, **kwargs)

    return guarded


@_preflight_source_before_lock
@archive_locked_if_capped
async def _run(
    url: str,
    cfg: Config,
    limit: int | None,
    pattern: str,
    concurrency: int,
    output_dir: Path,
    dry_run: bool,
    browser: bool,
    fmt: str | None = None,
    audio_only: bool = False,
    covers: bool = False,
    rate_limit: float | None = None,
    flat: bool = False,
    extract_audio: bool = False,
    lyrics_sidecar: str | None = None,
    blocked_ids: frozenset[str] = frozenset(),
    max_tracks: int = 0,
    max_disk_mb: int = 0,
    rotate: bool = False,
    min_free_mb: int = 2048,
    model_version: str | None = None,
    sidecar: bool = True,
    keep_video: bool = False,
    playback_paced: bool = True,
    pacer: PlaybackPacer | None = None,
    metadata_only: bool = False,
    update_existing: bool = False,
    force_redownload: bool = False,
    playlists: bool = False,
) -> dict[str, int]:
    # Expansion can persist checkpoints/memberships before downloading. Do not
    # begin an automated run while its producer provenance remains unverified.
    _require_trusted_framing()
    require_audio_sync(**locals())
    kind = _url_kind(url)
    subdir = None if flat else source_slug(url)
    if kind == "profile":
        handle = urlparse(url).path.rstrip("/").split("/")[-1].lstrip("@") or cfg.default_handle
    else:
        handle = cfg.default_handle.lstrip("@")

    if kind == "profile":
        # Use feed-based discovery with Playwright fallback controlled by --browser
        tracks = await discover_profile_tracks(
            url,
            handle,
            use_browser_fallback=browser,
            headless=True,
        )
    else:
        async with _make_fetcher(cfg, rate_limit) as fetcher:
            if kind == "song":
                clip_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
                if not UUID_RE.fullmatch(clip_id):
                    raise ValueError("Invalid selected song UUID")
                html = await fetcher.fetch_song_page(clip_id)
                # A song request selects exactly its URL UUID. Lyrics/RSC
                # must not redirect selection to another song on the page.
                matches = [t for t in parse_song(html) if t.id.lower() == clip_id]
                tracks = matches[:1] or [
                    TrackInfo(
                        id=clip_id, title="Pending verification", page_url=song_page_url(clip_id)
                    )
                ]
            else:
                tracks = await _source_playlist_tracks(fetcher, url)

    playlist_counts: dict[str, int] = {}
    if kind == "profile" and playlists:
        tracks, playlist_counts = await _expand_profile_playlists(
            cfg, handle, tracks, output_dir, dry_run, rate_limit
        )

    if model_version is not None:
        before = len(tracks)
        tracks = [t for t in tracks if t.model_version == model_version]
        click.echo(
            f"Filtered by model version '{model_version}': {before} -> {len(tracks)} track(s)."
        )

    if limit is not None:
        tracks = tracks[:limit]
    if max_tracks and len(tracks) > max_tracks:
        # Library cap: only the newest N are ever candidates, so a rotation
        # never fights a mirror of the whole profile. Profile pages list
        # newest first; an RSC payload carrying created_at is ground truth.
        tracks = sorted(tracks, key=lambda t: t.created_at or "", reverse=True)[:max_tracks]
        click.echo(f"Capped to the newest {max_tracks} track(s) (--max-tracks).")
    click.echo(f"Found {len(tracks)} public track(s).")
    if not tracks:
        click.echo("No public tracks found; Suno may have changed its page structure.", err=True)
        return {
            "total": 0,
            "downloaded": 0,
            "skipped_exists": 0,
            "failed": 0,
            "skipped_video": 0,
            **playlist_counts,
        }

    async with _make_fetcher(cfg, rate_limit) as fetcher:
        stats = await download_tracks(
            tracks,
            fetcher,
            concurrency,
            output_dir,
            pattern,
            handle,
            dry_run,
            preferred_format=fmt,
            audio_only=audio_only,
            covers=covers,
            subdir=subdir,
            extract_audio=extract_audio,
            # Avoid adding a new kwarg to legacy downloader call sites unless
            # WebUI explicitly requests both containers.
            **({"keep_video": True} if keep_video else {}),
            lyrics_sidecar=lyrics_sidecar,
            blocked_ids=blocked_ids,
            max_tracks=max_tracks,
            max_disk_mb=max_disk_mb,
            rotate=rotate,
            min_free_mb=min_free_mb,
            sidecar=sidecar,
            playback_paced=playback_paced,
            pacer=pacer,
            metadata_only=metadata_only,
            update_existing=update_existing,
            force_redownload=force_redownload,
            source=subdir or handle,
        )
    stats.update(playlist_counts)
    return stats


def _source_has_controls(value: str) -> bool:
    """Refuse characters that can forge or visually reorder log lines."""
    return any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in value)


def _safe_sync_error(exc: SunoError) -> str:
    """Describe expected failures without echoing untrusted exception text."""
    if isinstance(exc, DiskSpaceError):
        return "insufficient free space"
    if isinstance(exc, BlockedError):
        return "remote access was blocked"
    if isinstance(exc, FetchError):
        return "could not fetch the source"
    if isinstance(exc, ResolutionError):
        return "could not resolve media"
    if isinstance(exc, DownloadError):
        return "download failed"
    if isinstance(exc, ConfigError):
        return "invalid configuration"
    return "source failed"


def _safe_fatal_source_error(exc: Exception) -> str:
    """Classify a fatal failure without trusting its message."""
    if isinstance(exc, TaggingUnavailable):
        return "embedded tag updates are unavailable until metadata preservation is verified"
    if isinstance(exc, TagPublicationUncertain):
        return (
            "tag replacement may have occurred; stop and inspect the affected file "
            "and archive before any retry"
        )
    if isinstance(exc, MigrationError):
        return (
            "migration safety check failed; stop and inspect migration state "
            "before further archive writes"
        )
    if isinstance(exc, WindowsArchiveWriteUnavailable):
        return "Windows archive writes are unavailable until crash-safe recovery is supported"
    if isinstance(exc, UnsafeDownloadIntent):
        return (
            "archive write safety could not be established; stop and inspect platform "
            "support, the archive and journal status before any retry"
        )
    if isinstance(exc, ProducerLookupError):
        return "producer lookup unavailable: structured page provenance is unverified"
    if isinstance(exc, BrowserDependencyMissing):
        return (
            "Browser fallback requires Playwright. Install with "
            "\"pip install 'trackie-assistant[browser]'\" and run "
            '"python -m playwright install chromium", or pass --no-browser'
        )
    if isinstance(exc, ChromiumExecutableMissing):
        return (
            "Playwright Chromium is not installed. Run "
            '"python -m playwright install chromium", or pass --no-browser'
        )
    if isinstance(exc, ImportError):
        return "a required dependency is unavailable"
    if isinstance(exc, ValueError):
        return "invalid source or configuration"
    return f"unexpected {type(exc).__name__}"


def _run_cancellable_sync(
    operation: Coroutine[Any, Any, list[tuple[str, dict[str, int]]]],
) -> list[tuple[str, dict[str, int]]]:
    """Unwind Linux sync on the SIGTERM sent by the WebUI Cancel action."""

    async def run() -> list[tuple[str, dict[str, int]]]:
        if (
            not sys.platform.startswith("linux")
            or threading.current_thread() is not threading.main_thread()
        ):
            return await operation
        task = asyncio.current_task()
        if task is None:
            return await operation
        loop = asyncio.get_running_loop()
        stopping = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            if not stopping:
                stopping = True
                # Queue cancellation even while a synchronous media operation is running.
                loop.call_soon_threadsafe(task.cancel)

        previous = signal.signal(signal.SIGTERM, stop)
        try:
            return await operation
        except asyncio.CancelledError:
            if stopping:
                raise SystemExit(128 + signal.SIGTERM) from None
            raise
        finally:
            signal.signal(signal.SIGTERM, previous)

    return asyncio.run(run())


async def _run_sync(
    cfg: Config,
    watch_entries: list[str],
    limit: int | None,
    pattern: str,
    concurrency: int,
    dry_run: bool,
    browser: bool,
    fmt: str | None,
    audio_only: bool,
    covers: bool,
    rate_limit: float | None,
    flat: bool,
    extract_audio: bool,
    lyrics_sidecar: str | None,
    blocked_ids: frozenset[str] = frozenset(),
    max_tracks: int = 0,
    max_disk_mb: int = 0,
    rotate: bool = False,
    min_free_mb: int = 2048,
    model_version: str | None = None,
    sidecar: bool = True,
    keep_video: bool = False,
    playback_paced: bool = True,
    metadata_only: bool = False,
    update_existing: bool = False,
    force_redownload: bool = False,
    playlists: bool = False,
    blocked_path: Path | None = None,
) -> list[tuple[str, dict[str, int]]]:
    """Sync every watch-list entry sequentially; one source's failure must not
    abort the rest. Returns per-source stats in the same order as the input."""
    results: list[tuple[str, dict[str, int]]] = []
    # One pacer for the whole bulk run: switching source must not reset pacing.
    pacer = PlaybackPacer() if playback_paced else None
    for entry in watch_entries:
        try:
            if _source_has_controls(entry):
                raise ValueError("unsafe source")
            url, handle = normalize_watch_entry(entry)
        except ValueError:
            raise ValueError(
                "Invalid watch-list source; expected a handle or public Suno URL"
            ) from None
        display_url = redact_url(url)
        clip_id = (
            urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
            if _url_kind(url) == "song"
            else None
        )
        if (
            clip_id
            and blocked_path is not None
            and (clip_id in blocked_ids or clip_id in load_blocked_ids(blocked_path))
        ):
            if not dry_run:
                click.echo(f"{RETRY_PREFIX}{clip_id} skipped")
            results.append((display_url, {"total": 1, "skipped_blocked": 1}))
            continue
        if clip_id and not dry_run:
            click.echo(f"{RETRY_PREFIX}{clip_id} working")
        click.echo(f"Syncing {display_url} ...")
        try:
            stats = await _run(
                url,
                cfg,
                limit,
                pattern,
                concurrency,
                cfg.output_dir,
                dry_run,
                browser,
                fmt,
                audio_only=audio_only,
                covers=covers,
                rate_limit=rate_limit,
                flat=flat,
                extract_audio=extract_audio,
                keep_video=keep_video,
                lyrics_sidecar=lyrics_sidecar,
                blocked_ids=blocked_ids,
                max_tracks=max_tracks,
                max_disk_mb=max_disk_mb,
                rotate=rotate,
                min_free_mb=min_free_mb,
                model_version=model_version,
                sidecar=sidecar,
                playback_paced=playback_paced,
                pacer=pacer,
                metadata_only=metadata_only,
                update_existing=update_existing,
                force_redownload=force_redownload,
                playlists=playlists,
            )
        except SunoError as exc:
            # Per-source isolation: a dead entry must not block the archive.
            # Exception messages can embed signed URLs or arbitrary secrets.
            click.echo(f"Syncing {display_url} failed: {_safe_sync_error(exc)}", err=True)
            stats = {
                "total": 0,
                "downloaded": 0,
                "skipped_exists": 0,
                "failed": 0,
                "skipped_video": 0,
                "source_failed": 1,
            }
        except Exception as exc:  # noqa: BLE001 - do not echo untrusted exceptions
            # Fatal failures must still stop sync, but exception messages can
            # contain source URLs, signed media URLs, or other credentials.
            raise RuntimeError(
                f"Syncing {display_url} failed: {_safe_fatal_source_error(exc)}"
            ) from None
        if clip_id and not dry_run and not (stats.get("downloaded") or stats.get("skipped_exists")):
            skipped = any(
                stats.get(key)
                for key in ("skipped_blocked", "skipped_rotated", "skipped_capped", "skipped_video")
            )
            click.echo(f"{RETRY_PREFIX}{clip_id} {'skipped' if skipped else 'failed'}")
        results.append((handle or display_url, stats))
    return results


async def _run_network_styles(
    handle: str,
    session_cookie: str | None,
    rate_limit: float,
    network_cap: int,
    max_tracks: int,
    catalogue_path: Path | None,
    output_json: Path | None,
    output_csv: Path | None,
    verbose: bool,
) -> NetworkStyleAnalysis:
    """Run the network-styles analysis."""
    catalogue = load_catalogue(catalogue_path)

    network_profiles = await fetch_network_profiles(
        seed_handle=handle,
        session_cookie=session_cookie,
        rate_limit_sec=rate_limit,
        network_cap=network_cap,
        max_tracks_per_profile=max_tracks,
    )

    analysis = analyze_network_styles(network_profiles, catalogue)

    if output_json:
        dump_json(analysis, output_json)
        click.echo(f"JSON output written to {output_json}")
    if output_csv:
        dump_csv(analysis, output_csv)
        click.echo(f"CSV output written to {output_csv}")

    print_summary(analysis)
    return analysis


class DefaultGroup(click.Group):
    """A group that defaults to the download subcommand for backward compatibility."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and not args[0].startswith("-") and args[0] in self.commands:
            return super().parse_args(ctx, args)
        if args and args[0] in ("--help", "-h", "--version"):
            return super().parse_args(ctx, args)
        return super().parse_args(ctx, ["download"] + args)

    def get_help(self, ctx: click.Context) -> str:
        return self.commands["download"].get_help(ctx)


@click.group(cls=DefaultGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="trackie-assistant", prog_name="suno-dl")
def main() -> None:
    """Download public Suno tracks for personal archival."""
    # Click resolves subcommand environment options after this group callback.
    ensure_dotenv_loaded()


@main.command("download")
@click.option("--url", default=None, help="Public Suno profile, playlist, or song URL.")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Maximum tracks.")
@click.option("--pattern", default="{artist}_{title}", show_default=True, help="Filename pattern.")
@click.option(
    "--concurrency",
    type=click.IntRange(min=1, max=20),
    default=None,
    help="Parallel downloads (default 1 — sequential, human-paced).",
)
@click.option(
    "--rate-limit",
    type=click.FloatRange(min=0.0, max=60.0),
    default=None,
    help="Minimum seconds between requests to the same host, jittered. "
    "Default 1.5; 0 disables pacing.",
)
@click.option(
    "--playback-paced/--no-playback-paced",
    default=True,
    show_default=True,
    help="Space each media transfer by the previous track's playback duration "
    "(bulk-sync courtesy pacing; unknown durations use a 4 minute fallback). "
    "Skipped tracks never wait and never fetch media.",
)
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["m4a", "mp4", "mp3", "wav"], case_sensitive=False),
    default=None,
    help="Prefer this media format (others remain as fallbacks).",
)
@click.option(
    "--videos/--no-videos",
    "download_videos",
    default=None,
    help="Download MP4 video tracks. Default: off (SUNO_DOWNLOAD_VIDEOS=0). "
    "When off, only audio (m4a/mp3) is fetched.",
)
@click.option(
    "--covers/--no-covers",
    default=None,
    help="Fetch each track's cover JPEG/PNG. Default: on (SUNO_COVERS=1). "
    "Recompressed when the [image] extra is installed.",
)
@click.option(
    "--max-tracks",
    type=click.IntRange(min=1),
    default=None,
    help="Library cap: keep only the newest N tracks of the archive. Default: off. "
    "With --rotate, older tracks are deleted after the sync; without it, downloads "
    "stop at the cap and nothing is ever deleted (SUNO_MAX_TRACKS).",
)
@click.option(
    "--max-disk-mb",
    type=click.IntRange(min=1),
    default=None,
    help="Library cap on the archive size in MiB. Default: off. With --rotate, the "
    "oldest tracks are deleted until the archive fits; without it, downloads stop "
    "at the cap and nothing is ever deleted (SUNO_MAX_DISK_MB).",
)
@click.option(
    "--rotate/--no-rotate",
    "rotate",
    default=None,
    help="With a cap set, delete the OLDEST tracks (with their covers/videos) after "
    "the sync until the archive fits. Default: off — never deletes, just stops "
    "downloading (SUNO_ROTATE). Supports --dry-run.",
)
@click.option(
    "--sidecar/--no-sidecar",
    default=None,
    help="Write a <stem>.json sidecar (clip id, original title, public metadata) beside "
    "each track, and skip tracks whose id a sidecar already records. "
    "Default: on (SUNO_SIDECAR=1).",
)
@click.option(
    "--audio-only",
    is_flag=True,
    help="Audio-only output. Temporary published MP4 sources remain allowed "
    "unless --no-extract-audio.",
)
@click.option(
    "--flat",
    is_flag=True,
    help="Write into --output-dir directly instead of a per-source subfolder.",
)
@click.option(
    "--extract-audio/--no-extract-audio",
    default=True,
    help="Allow published video as an audio source when needed; discard the video. "
    "Audio is not re-encoded. Default: on.",
)
@click.option(
    "--keep-video",
    is_flag=True,
    help="With --videos --extract-audio, retain MP4 alongside extracted M4A.",
)
@click.option(
    "--metadata-only",
    is_flag=True,
    help="Repair tags and sidecars of already-archived clips and exit. Never "
    "downloads media, never replaces audio, never touches rotation.",
)
@click.option(
    "--update-existing",
    is_flag=True,
    help="Normal sync, but also repair tags/sidecars for clips already archived. "
    "Audio is never replaced; use --force-redownload for that.",
)
@click.option(
    "--force-redownload",
    is_flag=True,
    help="Explicitly validate and atomically replace existing recordings. Probes "
    "the current file read-only first; a failed transfer leaves it untouched.",
)
@click.option(
    "--lyrics-sidecar",
    type=click.Choice(["lrc", "txt"], case_sensitive=False),
    default=None,
    help="Write lyrics to a sidecar file (.lrc or .txt) next to each audio file. "
    "Skipped when lyrics are empty.",
)
@click.option(
    "--playlists/--no-playlists",
    default=False,
    envvar="SUNO_PLAYLISTS",
    show_default=True,
    help="Also archive the ordered membership of every explicitly public playlist "
    "the source profile advertises. Resumable across runs; private, hidden or "
    "trashed listings are never selected (SUNO_PLAYLISTS).",
)
@click.option(
    "--sync",
    is_flag=True,
    help="Sync every watch-list source (SUNO_WATCH or --watch); skips already-downloaded tracks.",
)
@click.option(
    "--watch",
    "watch_opt",
    multiple=True,
    help="Extra watch-list entry (handle or URL); repeatable, combined with SUNO_WATCH.",
)
@click.option("--dry-run", is_flag=True, help="List downloads without writing files.")
@click.option(
    "--retag",
    "retag_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Tag an existing archive in place (artist/title for Music Assistant) and exit. "
    "No requests are made; already-tagged files are skipped.",
)
@click.option(
    "--retag-archive-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Authoritative archive root for mutating --retag; must contain the target directory.",
)
@click.option(
    "--artist",
    "artist_override",
    default=None,
    help="Artist for files selected by --retag; files with a title are skipped. "
    "Default: the handle in each filename.",
)
@click.option(
    "--browser/--no-browser",
    default=True,
    help="Fall back to optional Playwright if the HTTP feed fails (default on; install [browser]).",
)
@click.option(
    "--model-version",
    "model_version",
    default=None,
    envvar="SUNO_MODEL_VERSION",
    help="Filter tracks by model version (e.g. v6, v4.5). Default: no filter.",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.option(
    "--min-free-mb",
    type=click.IntRange(min=0),
    default=None,
    help="Minimum free space (MB) on target volume before stopping. Default 2048.",
)
def download_cmd(
    url: str | None,
    limit: int | None,
    pattern: str,
    concurrency: int | None,
    rate_limit: float | None,
    playback_paced: bool,
    output_dir: Path | None,
    fmt: str | None,
    dry_run: bool,
    browser: bool,
    download_videos: bool | None,
    covers: bool | None,
    sidecar: bool | None,
    audio_only: bool,
    flat: bool,
    extract_audio: bool,
    keep_video: bool,
    metadata_only: bool,
    update_existing: bool,
    force_redownload: bool,
    lyrics_sidecar: str | None,
    playlists: bool,
    sync: bool,
    watch_opt: tuple[str, ...],
    retag_dir: Path | None,
    retag_archive_root: Path | None,
    artist_override: str | None,
    model_version: str | None,
    verbose: bool,
    min_free_mb: int | None,
    max_tracks: int | None = None,
    max_disk_mb: int | None = None,
    rotate: bool | None = None,
) -> None:
    """Download public Suno tracks for personal archival."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, which would leak signed-URL query
    # credentials into otherwise-normal output. Keep it at WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        # Retag is a pure-local migration: no URL, no config, no network.
        if retag_dir is not None:
            if not retag_dir.is_dir():
                raise click.ClickException(f"--retag: not a directory: {retag_dir}")
            if not dry_run and retag_archive_root is None:
                raise click.ClickException(
                    "A mutating --retag requires the authoritative --retag-archive-root."
                )
            root = (retag_archive_root or retag_dir).expanduser().resolve()
            target = retag_dir.expanduser().resolve()
            if not root.is_dir() or not target.is_relative_to(root):
                raise click.ClickException(
                    "--retag target must be inside the declared archive root"
                )
            raise SystemExit(
                asyncio.run(
                    _retag_locked(root, dry_run, artist_override or "", lyrics_sidecar, target)
                )
            )
        if retag_archive_root is not None:
            raise click.ClickException("--retag-archive-root is only used with --retag.")
        if artist_override:
            raise click.ClickException("--artist is only used with --retag.")
        # Validate an explicit URL before loading config: a bad --url should
        # never be masked by "no default handle configured".
        if url is not None and (_source_has_controls(url) or not URL_RE.match(url)):
            raise click.ClickException("Invalid Suno URL. Only public suno.com URLs are accepted.")
        # Flag conflicts are reported before config is even read, so a missing
        # default handle can never mask an impossible combination.
        if metadata_only and force_redownload:
            raise click.ClickException(
                "--metadata-only never fetches media; use --force-redownload on its own."
            )
        if metadata_only and sidecar is False:
            raise click.ClickException(
                "--metadata-only matches archived clips by their sidecars; remove --no-sidecar."
            )
        cfg = load_config(
            concurrency=concurrency,
            output_dir=output_dir,
            pattern=pattern,
            rate_limit_sec=rate_limit,
            max_tracks=max_tracks,
            max_disk_mb=max_disk_mb,
            rotate=rotate,
            min_free_mb=min_free_mb,
        )
        # Blocklist lives next to the .env the run uses, i.e. the working
        # directory (the same root ensure_dotenv_loaded searches) — anchoring
        # it on --output-dir would break when the flag overrides the archive
        # location, and uv injects the repo .env into the environment, which
        # can shadow the archive .env's SUNO_OUTPUT_DIR.
        blocked_path = cfg.blocked_file
        if not blocked_path.is_absolute():
            blocked_path = Path.cwd() / blocked_path
        blocked_ids = load_blocked_ids(blocked_path)
        # Defaults: audio + covers; videos opt-in (#85).
        videos = cfg.download_videos if download_videos is None else download_videos
        if audio_only:
            videos = False
        want_covers = cfg.covers if covers is None else covers
        audio_only = not videos
        if keep_video and (audio_only or not extract_audio):
            raise click.ClickException("--keep-video requires --videos and --extract-audio.")
        covers = want_covers
        want_sidecar = cfg.sidecar if sidecar is None else sidecar
        if metadata_only and not want_sidecar:
            raise click.ClickException(
                "--metadata-only matches archived clips by their sidecars; "
                "remove SUNO_SIDECAR=0 / --no-sidecar."
            )
        if sync:
            entries = list(watch_opt) if watch_opt else list(cfg.watch)
            if not entries:
                raise click.ClickException(
                    "--sync needs a watch list: set SUNO_WATCH=handle1,handle2 "
                    "in .env or pass --watch (repeatable)."
                )
            results = _run_cancellable_sync(
                _run_sync(
                    cfg,
                    entries,
                    limit,
                    pattern,
                    concurrency or cfg.concurrency,
                    dry_run,
                    browser,
                    fmt,
                    audio_only=audio_only,
                    covers=covers,
                    rate_limit=rate_limit,
                    flat=flat,
                    extract_audio=extract_audio,
                    keep_video=keep_video,
                    lyrics_sidecar=lyrics_sidecar,
                    blocked_ids=blocked_ids,
                    max_tracks=cfg.max_tracks,
                    max_disk_mb=cfg.max_disk_mb,
                    rotate=cfg.rotate,
                    min_free_mb=cfg.min_free_mb,
                    model_version=model_version,
                    sidecar=want_sidecar,
                    playback_paced=playback_paced,
                    metadata_only=metadata_only,
                    update_existing=update_existing,
                    force_redownload=force_redownload,
                    playlists=playlists,
                    blocked_path=blocked_path,
                )
            )
            totals: dict[str, int] = {}
            for _handle, stats in results:
                for key in (
                    "total",
                    "downloaded",
                    "skipped_exists",
                    "failed",
                    "skipped_video",
                    "skipped_blocked",
                    "skipped_capped",
                    "skipped_rotated",
                    "rotated_tracks",
                    "rotated_files",
                    "rotated_bytes",
                    "tagged",
                    "sidecars",
                    "replaced",
                    "invalid_existing",
                    "metadata_updated",
                    "metadata_unchanged",
                    "metadata_missing",
                    "metadata_failed",
                    "metadata_unresolved",
                    "playlists_found",
                    "playlists_expanded",
                    "playlists_skipped",
                    "playlists_unresolved",
                    "playlist_tracks",
                ):
                    totals[key] = totals.get(key, 0) + stats.get(key, 0)
            click.echo(
                "Sync summary: "
                f"sources={len(results)} "
                f"total={totals['total']} "
                f"downloaded={totals['downloaded']} "
                f"existing={totals['skipped_exists']} "
                f"video_skipped={totals['skipped_video']} "
                f"blocked={totals.get('skipped_blocked', 0)} "
                f"capped={totals.get('skipped_capped', 0)} "
                f"pruned_skipped={totals.get('skipped_rotated', 0)} "
                f"rotated={totals.get('rotated_tracks', 0)} "
                f"rotated_files={totals.get('rotated_files', 0)} "
                f"rotated_mb={totals.get('rotated_bytes', 0) / (1024 * 1024):.1f} "
                f"tagged={totals.get('tagged', 0)} "
                f"sidecars={totals.get('sidecars', 0)} "
                f"replaced={totals.get('replaced', 0)} "
                f"metadata_updated={totals.get('metadata_updated', 0)} "
                f"metadata_failed={totals.get('metadata_failed', 0)} "
                f"playlists={totals.get('playlists_expanded', 0)}"
                f"/{totals.get('playlists_found', 0)} "
                f"playlist_unresolved={totals.get('playlists_unresolved', 0)} "
                f"failed={totals['failed']}"
            )
            if any(stats.get("source_failed") for _h, stats in results):
                raise SystemExit(EXIT_PARTIAL)
            return
        source_url = url or cfg.build_default_url()
        if _source_has_controls(source_url) or not URL_RE.match(source_url):
            raise click.ClickException("Invalid Suno URL. Only public suno.com URLs are accepted.")
        try:
            stats = asyncio.run(
                _run(
                    source_url,
                    cfg,
                    limit,
                    pattern,
                    concurrency or cfg.concurrency,
                    output_dir if output_dir is not None else cfg.output_dir,
                    dry_run,
                    browser,
                    fmt,
                    audio_only=audio_only,
                    covers=covers,
                    rate_limit=rate_limit,
                    flat=flat,
                    extract_audio=extract_audio,
                    keep_video=keep_video,
                    lyrics_sidecar=lyrics_sidecar,
                    blocked_ids=blocked_ids,
                    max_tracks=cfg.max_tracks,
                    max_disk_mb=cfg.max_disk_mb,
                    rotate=cfg.rotate,
                    min_free_mb=cfg.min_free_mb,
                    model_version=model_version,
                    sidecar=want_sidecar,
                    playback_paced=playback_paced,
                    metadata_only=metadata_only,
                    update_existing=update_existing,
                    force_redownload=force_redownload,
                    playlists=playlists,
                )
            )
        except SunoError as exc:
            raise click.ClickException(
                f"Source {redact_url(source_url)} failed: {_safe_sync_error(exc)}"
            ) from None
        except Exception as exc:  # noqa: BLE001 - do not echo untrusted exception text
            raise click.ClickException(
                f"Source {redact_url(source_url)} failed: {_safe_fatal_source_error(exc)}"
            ) from None
    except (ValueError, RuntimeError, ImportError, SunoError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        "Summary: "
        f"total={stats.get('total', 0)} "
        f"downloaded={stats.get('downloaded', 0)} "
        f"existing={stats.get('skipped_exists', 0)} "
        f"video_skipped={stats.get('skipped_video', 0)} "
        f"blocked={stats.get('skipped_blocked', 0)} "
        f"capped={stats.get('skipped_capped', 0)} "
        f"pruned_skipped={stats.get('skipped_rotated', 0)} "
        f"rotated={stats.get('rotated_tracks', 0)} "
        f"rotated_files={stats.get('rotated_files', 0)} "
        f"rotated_mb={stats.get('rotated_bytes', 0) / (1024 * 1024):.1f} "
        f"tagged={stats.get('tagged', 0)} "
        f"sidecars={stats.get('sidecars', 0)} "
        f"replaced={stats.get('replaced', 0)} "
        f"metadata_updated={stats.get('metadata_updated', 0)} "
        f"metadata_unchanged={stats.get('metadata_unchanged', 0)} "
        f"metadata_missing={stats.get('metadata_missing', 0)} "
        f"metadata_failed={stats.get('metadata_failed', 0)} "
        f"playlists={stats.get('playlists_expanded', 0)}"
        f"/{stats.get('playlists_found', 0)} "
        f"playlist_tracks={stats.get('playlist_tracks', 0)} "
        f"playlist_unresolved={stats.get('playlists_unresolved', 0)} "
        f"failed={stats.get('failed', 0)}"
    )
    # Non-zero exit on failure or when nothing was found; 0 only on clean runs.
    if stats.get("total", 0) == 0:
        raise SystemExit(EXIT_NO_TRACKS)
    if stats.get("failed", 0) > 0 or stats.get("metadata_failed", 0) > 0:
        raise SystemExit(EXIT_PARTIAL)

    dest = output_dir if output_dir is not None else cfg.output_dir
    if not dry_run and not metadata_only:
        final_dir = resolve_output_dir(dest, None if flat else source_slug(source_url))
        click.echo(f"Saved to {final_dir}")


@main.command("network-styles")
@click.option("--handle", required=True, help="Seed profile handle (e.g. drinko).")
@click.option(
    "--session-cookie",
    default=None,
    help="Suno session cookie (NAME=VALUE) for follow-list expansion (env SUNO_SESSION_COOKIE).",
)
@click.option(
    "--rate-limit",
    type=click.FloatRange(min=0.0, max=60.0),
    default=1.5,
    help="Minimum seconds between requests to the same host (default 1.5).",
)
@click.option(
    "--network-cap",
    type=click.IntRange(min=1, max=100),
    default=20,
    help="Maximum profiles to fetch from followers+following (default 20).",
)
@click.option(
    "--max-tracks",
    type=click.IntRange(min=1, max=500),
    default=20,
    help="Maximum tracks per profile to analyze (default 20 newest).",
)
@click.option(
    "--catalogue",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to catalogue JSON file (env SUNO_CATALOGUE_PATH, defaults to test fixture).",
)
@click.option(
    "--output-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Write full analysis as JSON to this file.",
)
@click.option(
    "--output-csv",
    type=click.Path(path_type=Path),
    default=None,
    help="Write style rankings as CSV to this file.",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
def network_styles_cmd(
    handle: str,
    session_cookie: str | None,
    rate_limit: float,
    network_cap: int,
    max_tracks: int,
    catalogue: Path | None,
    output_json: Path | None,
    output_csv: Path | None,
    verbose: bool,
) -> None:
    """Analyze style tags across a profile's network (followers + following).

    Fetches public song metadata (title, style tags, play count) for the seed
    profile and its network neighbors. Compares aggregate style tags against
    a reference catalogue to find absent/rare styles.

    No audio is downloaded. Uses read-only public data and reuses auth.
    """
    import os

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    session_cookie = session_cookie or os.getenv("SUNO_SESSION_COOKIE")

    try:
        asyncio.run(
            _run_network_styles(
                handle=handle,
                session_cookie=session_cookie,
                rate_limit=rate_limit,
                network_cap=network_cap,
                max_tracks=max_tracks,
                catalogue_path=catalogue,
                output_json=output_json,
                output_csv=output_csv,
                verbose=verbose,
            )
        )
    except (ValueError, RuntimeError, ImportError, SunoError) as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
