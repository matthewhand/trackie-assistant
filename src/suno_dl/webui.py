"""Local web UI for driving Trackie Assistant.

Why this exists
---------------
The CLI's surface (sync, recommend, blocklist, dry-run) is powerful but
terminal-bound; a small local dashboard makes it drivable from a browser
without changing the CLI itself. The UI is a thin layer: every action maps
to the *real* CLI module run as a subprocess from the archive directory, so
pacing defaults, media validation, the blocklist, and `.env` handling are
byte-for-byte identical to terminal use.

Security posture
----------------
* Binds to 127.0.0.1 by default. ``--host`` may expose it to the LAN —
  explicitly and visibly (the banner says so) — while the Host-header guard
  still only accepts *this machine's* names, which is what defeats DNS
  rebinding: an attacker-chosen DNS name resolving to the server is refused
  even when the server listens on all interfaces.
* Rejects requests whose ``Host`` header is not localhost (DNS-rebinding
  guard).
* No secrets cross the boundary: session cookies and env vars are never
  read into UI state, JSON payloads, or logs. Subprocesses inherit the
  environment implicitly; the UI never mirrors it.
* One run at a time, matching the human-paced charter — the UI cannot be
  used to hammer Suno.

No new runtime dependencies: stdlib ``http.server`` + ``subprocess`` only.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import click
from dotenv import load_dotenv

from . import __version__
from .archive_lock import raise_if_unsupported_archive_write
from .blocklist import load_blocked_ids
from .download_intent import WindowsArchiveWriteUnavailable
from .linux_downloads import require_audio_sync
from .media_process import run as run_media_process
from .naming import normalize_watch_entry
from .opstatus import (
    ApiError,
    classify_exception,
    classify_run_failure,
    error_body,
    new_request_id,
    parse_progress,
    redact,
)
from .producer_lookup import ProducerLookupError, _require_trusted_framing
from .sync_capacity import destination_space, latest_capacity
from .tagging import TaggingUnavailable

__all__ = ["webui_cli"]

logger = logging.getLogger("suno_dl.webui")

_MAX_RUN_OUTPUT_LINES = 2000
_MAX_STORED_RUNS = 20
# Audio-only: what Music Assistant consumes. The mp4 video twins stay
# unlisted and unserved — the video would double every HA pull for nothing.
_AUDIO_SUFFIXES = frozenset({".m4a", ".mp3", ".wav"})


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class GatewayAuth:
    """Gateway access policy (#162), entirely operator-configured.

    Trust is decided from the **direct TCP peer** only. ``X-Forwarded-For``,
    ``Forwarded``, ``X-Real-IP`` and Host are never consulted: behind NAT, a
    Docker publish, a VPN or a reverse proxy they are attacker-controlled. If
    the peer cannot be attributed to a permitted direct client, the request is
    denied and the operator is told to configure CIDRs or enable auth.

    Defaults preserve the historical behaviour: no auth, LAN binding still
    requires the Host guard, and writes stay same-origin checked. Enabling
    ``require_auth`` is the strict override and takes precedence over every
    LAN bypass; enabling it without credentials is a startup error, never an
    open gateway.
    """

    require_auth: bool = False
    lan_readonly: bool = False
    trusted_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    credentials: tuple[str, str] | None = None

    @property
    def enabled(self) -> bool:
        return (
            self.require_auth
            or self.lan_readonly
            or bool(self.trusted_cidrs)
            or self.has_credentials
        )

    @property
    def has_credentials(self) -> bool:
        return self.credentials is not None

    @classmethod
    def from_env(cls) -> GatewayAuth:
        """Read ``TRACKIE_GATEWAY_*``; raises ValueError on unsafe config."""
        require = _truthy(os.environ.get("TRACKIE_GATEWAY_REQUIRE_AUTH"))
        readonly = _truthy(os.environ.get("TRACKIE_GATEWAY_LAN_READONLY"))
        raw = os.environ.get("TRACKIE_GATEWAY_TRUSTED_CIDRS", "")
        raw_cidrs = [part.strip() for part in raw.split(",")]
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw in raw_cidrs:
            if not raw:
                continue
            try:
                networks.append(ipaddress.ip_network(raw, strict=False))
            except ValueError as exc:
                raise ValueError(f"invalid TRACKIE_GATEWAY_TRUSTED_CIDRS entry {raw!r}") from exc
        user = os.environ.get("TRACKIE_GATEWAY_USER", "")
        password = os.environ.get("TRACKIE_GATEWAY_PASSWORD", "")
        credentials = (user, password) if user and password else None
        if require and credentials is None:
            raise ValueError(
                "TRACKIE_GATEWAY_REQUIRE_AUTH needs TRACKIE_GATEWAY_USER and "
                "TRACKIE_GATEWAY_PASSWORD; refusing to start an open gateway"
            )
        return cls(require, readonly, tuple(networks), credentials)

    def peer_networks(self, peer: str) -> bool:
        """True only when the direct socket peer is inside a configured CIDR."""
        try:
            address = ipaddress.ip_address(peer.split("%", 1)[0])
        except ValueError:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        candidates = [address, mapped] if mapped is not None else [address]
        return any(
            any(address.version == network.version and address in network for address in candidates)
            for network in self.trusted_cidrs
        )

    def allows_read(self, peer: str) -> bool:
        if self.require_auth:
            return False
        if self.lan_readonly:
            return self.peer_networks(peer)
        return True

    def allows_write(self, peer: str) -> bool:
        """Every state-changing request needs credentials once a policy is set.

        This deliberately has no loopback exception: a local browser and a LAN
        peer run the same UI, and a missing credential must fail closed rather
        than silently become an unauthenticated write path.
        """
        return not (self.require_auth or self.lan_readonly or self.has_credentials)

    def credentials_match(self, header: str | None) -> bool:
        """Constant-time HTTP Basic check; malformed headers never match."""
        if self.credentials is None or not header:
            return False
        scheme, _, payload = header.partition(" ")
        if scheme.lower() != "basic" or not payload:
            return False
        try:
            decoded = base64.b64decode(payload, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        user, sep, password = decoded.partition(":")
        if not sep:
            return False
        expected_user, expected_password = self.credentials
        ok_user = hmac.compare_digest(user.encode(), expected_user.encode())
        ok_password = hmac.compare_digest(password.encode(), expected_password.encode())
        return ok_user and ok_password


@dataclass
class Run:
    """One CLI subprocess: its command, live output tail, and outcome."""

    id: str
    kind: str
    cmd: list[str]
    started: float = field(default_factory=time.time)
    finished: float | None = None
    returncode: int | None = None
    output: list[str] = field(default_factory=list)
    request_id: str = field(default_factory=new_request_id)
    cancel_requested: bool = False
    process: subprocess.Popen[str] | None = field(default=None, repr=False)

    @property
    def state(self) -> str:
        if self.returncode is None:
            return "cancelling" if self.cancel_requested else "running"
        if self.cancel_requested:
            return "cancelled"
        return "done" if self.returncode == 0 else "failed"

    def snapshot(self) -> dict[str, object]:
        snap: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "returncode": self.returncode,
            "started": self.started,
            "finished": self.finished,
            "output": [redact(line) for line in self.output[-100:]],
            "request_id": self.request_id,
            "progress": parse_progress(self.output),
            "capacity": latest_capacity(self.output),
            "error": None,
        }
        if self.state in ("failed", "cancelled"):
            snap["error"] = error_body(
                classify_run_failure(self.returncode, self.output, cancelled=self.cancel_requested),
                self.request_id,
            )
        return snap


class RunManager:
    """Starts CLI subprocesses in the background; enforces one at a time."""

    def __init__(self, archive: Path) -> None:
        self.archive = archive
        self._lock = threading.Lock()
        self._runs: list[Run] = []

    @property
    def busy(self) -> bool:
        # Lock-free on purpose: called while holding the lock in start(),
        # and list-iteration is GIL-atomic enough for a best-effort check.
        return any(run.returncode is None for run in self._runs)

    def start(self, kind: str, cmd: list[str]) -> tuple[Run, None] | tuple[None, str]:
        with self._lock:
            if any(run.returncode is None for run in self._runs):
                return None, "another run is already active (one at a time)"
            run = Run(id=uuid.uuid4().hex[:8], kind=kind, cmd=cmd)
            self._runs.append(run)
            del self._runs[:-_MAX_STORED_RUNS]
        thread = threading.Thread(target=self._execute, args=(run,), daemon=True)
        thread.start()
        return run, None

    def _execute(self, run: Run) -> None:
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                run.cmd,
                cwd=str(self.archive),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            run.output.append(f"failed to start: {exc}")
            logger.error("[%s] run %s failed to start: %s", run.request_id, run.kind, redact(exc))
            run.returncode = -1
            run.finished = time.time()
            return
        # Register the process atomically with cancel(). If Cancel arrived while
        # Popen was still starting, honour it as soon as the handle exists.
        # Without the lock the UI can say "cancelling" while work continues.
        with self._lock:
            run.process = process
            cancelled_before_registration = run.cancel_requested
        if cancelled_before_registration:
            try:
                process.terminate()
            except OSError as exc:  # process may have exited during startup
                logger.warning("[%s] terminate failed: %s", run.request_id, redact(exc))
        if process.stdout is None:  # pragma: no cover - PIPE guarantees a stream
            run.output.append("subprocess produced no output stream")
            run.returncode = -1
            run.finished = time.time()
            return
        for line in process.stdout:
            run.output.append(line.rstrip("\n"))
            del run.output[:-_MAX_RUN_OUTPUT_LINES]
        run.returncode = process.wait()
        run.finished = time.time()
        if run.returncode not in (0, None) and not run.cancel_requested:
            logger.error(
                "[%s] run %s exited %s; last output: %s",
                run.request_id,
                run.kind,
                run.returncode,
                redact(" | ".join(run.output[-6:])),
            )

    def cancel(self, run_id: str) -> tuple[Run | None, ApiError | None]:
        """Ask a running subprocess to stop (SIGTERM / terminate)."""
        with self._lock:
            run = next((r for r in self._runs if r.id == run_id), None)
            if run is None:
                return None, ApiError("not_found", "No such run.", run_id)
            if run.returncode is not None:
                return run, ApiError("bad_request", "That run already finished.", run.state)
            if run.cancel_requested:
                return run, None  # idempotent: no second terminate()
            run.cancel_requested = True
            # _execute() attaches the handle under the same lock; precisely
            # one side is responsible for calling terminate().
            process = run.process
        if process is not None:
            try:
                process.terminate()
            except OSError as exc:  # already gone
                logger.warning("[%s] terminate failed: %s", run.request_id, redact(exc))
        return run, None

    def list(self) -> list[dict[str, object]]:
        with self._lock:
            return [run.snapshot() for run in reversed(self._runs)]


def _sync_command(
    dry_run: bool,
    *,
    videos: bool = False,
    covers: bool = True,
    keep_video: bool = True,
    extract_audio: bool = True,
    sources: tuple[str, ...] = (),
) -> list[str]:
    """Build WebUI sync argv, with HA ingress unable to retain MP4."""
    cmd = [sys.executable, "-u", "-m", "suno_dl.cli", "--sync"]
    cmd.append("--extract-audio" if extract_audio else "--no-extract-audio")
    cmd.append("--videos" if videos else "--no-videos")
    if videos and keep_video:
        # HA ingress disables this opt-in regardless of browser checkbox state.
        cmd.append("--keep-video")
    cmd.append("--covers" if covers else "--no-covers")
    for source in sources:
        cmd.extend(["--watch", source])
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def _recommend_command(candidates: list[str], taste: bool, archive: Path) -> list[str]:
    cmd = [sys.executable, "-u", "-m", "suno_dl.recommend", *candidates]
    if taste:
        cmd += ["--taste-from", str(archive)]
    return cmd


def _compute_status(archive: Path, env: dict[str, str] | None = None) -> dict[str, object]:
    """Everything the dashboard shows — no secrets, all derived from disk/env.

    ``env`` defaults to the process environment; tests pass an explicit dict
    to stay independent of whatever the host environment happens to carry.
    """
    environ = env if env is not None else os.environ
    downloads = Path(environ.get("SUNO_OUTPUT_DIR", "./downloads")).expanduser()
    if not downloads.is_absolute():
        downloads = archive / downloads
    profiles: list[dict[str, object]] = []
    if downloads.is_dir():
        for child in sorted(downloads.iterdir()):
            if not child.is_dir():
                continue
            media = (".m4a", ".mp4", ".mp3", ".wav")
            files = [f for f in child.iterdir() if f.is_file() and f.suffix.lower() in media]
            size_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
            profiles.append(
                {"name": child.name, "tracks": len(files), "size_mb": round(size_mb, 1)}
            )
    blocked_ids = load_blocked_ids(archive / ".blocked")
    watch = [w.strip() for w in environ.get("SUNO_WATCH", "").split(",") if w.strip()]
    engine = (
        "JEV configured"
        if environ.get("SUNO_JEV_URL")
        else "heuristic (no JEV endpoint configured)"
    )
    return {
        "version": __version__,
        "archive": str(archive),
        "downloads_dir": str(downloads),
        "storage": destination_space(downloads),
        "profiles": profiles,
        "blocked": {"count": len(blocked_ids), "ids": sorted(blocked_ids)},
        "watch": watch,
        "engine": engine,
    }


def _audio_files(profile_dir: Path) -> list[Path]:
    """One entry per audio file in a profile directory, sorted by name."""
    if not profile_dir.is_dir():
        return []
    return sorted(
        (f for f in profile_dir.iterdir() if f.is_file() and f.suffix.lower() in _AUDIO_SUFFIXES),
        key=lambda f: f.name,
    )


def _list_files(archive: Path, env: dict[str, str] | None = None) -> dict[str, object]:
    """Flat per-profile audio listing for remote pullers (HA integration).

    Names only — the client fetches bytes one file at a time via
    ``/media/<profile>/<file>``. Small index, no secrets, no URLs.
    """
    environ = env if env is not None else os.environ
    downloads = Path(environ.get("SUNO_OUTPUT_DIR", "./downloads")).expanduser()
    if not downloads.is_absolute():
        downloads = archive / downloads
    profiles: list[dict[str, object]] = []
    if downloads.is_dir():
        for child in sorted(downloads.iterdir()):
            if not child.is_dir():
                continue
            audio = _audio_files(child)
            files = [f.name for f in audio]
            sizes = {f.name: f.stat().st_size for f in audio}
            profiles.append(
                {
                    "name": child.name,
                    "files": files,
                    "count": len(files),
                    "sizes": sizes,
                }
            )
    return {"profiles": profiles}


def _safe_media_path(archive: Path, rel: str, env: dict[str, str] | None = None) -> Path | None:
    """Resolve a /media/<profile>/<file> request inside the downloads dir.

    Mirrors naming.py's containment discipline: no ``..``, no absolute
    components, the resolved path must stay inside the downloads tree, and
    the file must be an audio suffix we are willing to serve. Returns None
    for anything that fails a check (404, never a hint as to why).
    """
    environ = env if env is not None else os.environ
    downloads = Path(environ.get("SUNO_OUTPUT_DIR", "./downloads")).expanduser()
    if not downloads.is_absolute():
        downloads = archive / downloads
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if len(parts) != 2:
        return None
    profile, filename = parts
    if profile.startswith(".") or filename.startswith("."):
        return None
    candidate = (downloads / profile / filename).resolve()
    root = downloads.resolve()
    if candidate.parent != root / profile or candidate.suffix.lower() not in _AUDIO_SUFFIXES:
        return None
    return candidate if candidate.is_file() else None


def _host_allowed(
    host: str | None, extra: frozenset[str] = frozenset(), *, ingress: bool = False
) -> bool:
    """True if the Host header names this dashboard legitimately.

    Loopback names are always allowed (the default posture). ``extra``
    carries the additional names the UI is legitimately served as when
    bound beyond loopback — this machine's own addresses and hostname.
    Anything else (an attacker-chosen DNS name resolving at the server,
    say) is rejected, which is what defeats DNS rebinding.

    ``ingress=True`` disables the guard entirely: the server sits behind
    a trusted reverse proxy (e.g. the Home Assistant ingress), which
    rewrites Host to a proxy-internal name no client controls. Only
    legitimate in a container where the loopback interface is the proxy.
    """
    if ingress:
        return True
    name = _canonical_host(_host_name(host or ""))
    if not name:
        return False
    if name in ("127.0.0.1", "localhost", "::1"):
        return True
    return name in {_canonical_host(entry) for entry in extra}


def _host_name(raw: str) -> str:
    """The host part of a Host header, without port or IPv6 brackets.

    Brackets must come off *before* splitting on ``:``: ``[::1]`` has three
    colons, so the old rsplit-then-strip order produced ``":"`` and rejected
    the request. Every browser on an IPv6 loopback sends this bare form.
    """
    value = raw.strip()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:  # name:port — but not a bare IPv6 literal
        return value.split(":", 1)[0]
    return value


def _canonical_host(name: str) -> str:
    """Lowercase, bracket-free, FQDN-dot-free, with IPs in compressed form.

    So ``[0:0:0:0:0:0:0:1]`` and ``::1`` compare equal to the loopback entry,
    and a trailing root dot (``localhost.``) does not smuggle in a new name.
    """
    value = name.strip().lower().strip("[]").rstrip(".")
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return value


def _machine_addresses() -> frozenset[str]:
    """This host's own names/IPs — the acceptable Host values on LAN mode.

    Three stdlib sources, because any one can miss the LAN address: the
    hostname's resolutions (``/etc/hosts`` may map it to loopback only),
    ``gethostbyname_ex`` for the fuller alias list, and a UDP-connect probe
    (sends no packets) whose source address is the default-route interface's
    IP — i.e. the LAN address remote browsers will actually put in Host.
    """
    names: set[str] = set()
    try:
        hostname = socket.gethostname().lower()
        names.add(hostname)
        try:
            _name, _aliases, ips = socket.gethostbyname_ex(hostname)
            names.update(ip.lower() for ip in ips)
        except OSError:
            pass
        for info in socket.getaddrinfo(hostname, None):
            names.add(str(info[4][0]).lower())
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.5)
            probe.connect(("8.8.8.8", 80))
            names.add(str(probe.getsockname()[0]).lower())
    except OSError:
        pass
    return frozenset(name for name in names if name)


def _genres_dir(explicit: Path | None = None) -> Path | None:
    """Resolve the genres archive root (clips.json + genres.json).

    Order: explicit CLI path → SUNO_GENRES_DIR. No machine-specific default path.
    Returns None when no library is available (webui still serves sync/recommend).
    """
    if explicit is not None:
        return explicit.expanduser().resolve()
    env = os.environ.get("SUNO_GENRES_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return None


def _read_json_file(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _blocked_count(archive: Path) -> int | None:
    """How many clips .blocked lists; None if the file is malformed."""
    try:
        return len(load_blocked_ids(archive / ".blocked"))
    except Exception:  # noqa: BLE001 - a bad blocklist must not break browsing
        return None


def _library_payload(genres_dir: Path | None, archive: Path | None = None) -> dict[str, object]:
    """Browse payload for the shared genre UI. Never raises; empty on miss."""
    if genres_dir is None:
        return {
            "ok": False,
            "genres": {"categories": [], "assignments": {}, "pinned": []},
            "clips": [],
        }
    genres = _read_json_file(genres_dir / "genres.json")
    if not isinstance(genres, dict):
        genres = {"categories": [], "assignments": {}, "pinned": []}
    clips = _read_json_file(genres_dir / "clips.slim.json")
    if not isinstance(clips, list):
        clips = _read_json_file(genres_dir / "clips.json")
    if not isinstance(clips, list):
        clips = []
    # Keep the card-sized fields only.
    slim = []
    for c in clips:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        slim.append(
            {
                "id": c.get("id"),
                "title": c.get("title") or "",
                "style": c.get("style") or "",
                "model": c.get("model") or c.get("model_name") or "",
            }
        )
    raw_assignments: object = genres.get("assignments")
    assignments: dict[str, object] = raw_assignments if isinstance(raw_assignments, dict) else {}
    counts: dict[str, object] = {"archive": len(slim), "classified": len(assignments)}
    if archive is not None:
        counts["blocked"] = _blocked_count(archive)
    return {"ok": True, "genres": genres, "clips": slim, "root": str(genres_dir), "counts": counts}


# ───────────────────────── track metadata (duration / created / audio) ─────────

_CLIP_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _norm_stem(name: str) -> str:
    """Lowercase alphanumerics only: 'feat_' and 'feat.' spellings collide."""
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def _downloads_root(archive: Path, env: dict[str, str] | None = None) -> Path:
    environ = env if env is not None else os.environ
    downloads = Path(environ.get("SUNO_OUTPUT_DIR", "./downloads")).expanduser()
    return downloads if downloads.is_absolute() else archive / downloads


def _ffprobe_duration(path: Path) -> float | None:
    """Seconds of audio via ffprobe, or None if ffprobe is missing/fails."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = run_media_process(
            [exe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout.strip()
        return round(float(out), 2) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _load_clips(genres_dir: Path | None) -> list[dict[str, object]]:
    if genres_dir is None:
        return []
    for name in ("clips.slim.json", "clips.json"):
        data = _read_json_file(genres_dir / name)
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict) and c.get("id")]
    return []


class TrackIndex:
    """Maps archive clips to local audio files with duration + created date.

    * Audio: matched by the clip's ``file`` stem, then ``<profile>_<title>``,
      both normalised (case/punctuation-insensitive) so ``feat_``/``feat.``
      spellings agree. Unmatched clips report ``audio: null``.
    * Duration: ffprobe (injectable ``prober`` for tests), cached by
      (path, mtime, size); probing runs in one background thread.
    * Created: Suno's ``created_at`` when the archive kept it, else the
      audio file's mtime — ``created_source`` says which.
    """

    def __init__(
        self,
        archive: Path,
        genres_dir: Path | None,
        env: dict[str, str] | None = None,
        prober=_ffprobe_duration,
    ) -> None:
        self.archive = archive
        self.genres_dir = genres_dir
        self.env = env
        self.prober = prober
        self._durations: dict[tuple[str, int, int], float | None] = {}
        self._lock = threading.Lock()
        self._warming: threading.Thread | None = None

    def _files(self) -> dict[str, tuple[str, Path]]:
        root = _downloads_root(self.archive, self.env)
        index: dict[str, tuple[str, Path]] = {}
        if not root.is_dir():
            return index
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            for f in _audio_files(child):
                index.setdefault(_norm_stem(f.stem), (child.name, f))
        return index

    def _match(self, clip: dict[str, object], files: dict[str, tuple[str, Path]]):
        keys: list[str] = []
        fname = clip.get("file")
        if isinstance(fname, str) and fname:
            keys.append(_norm_stem(Path(fname).stem))
        title = str(clip.get("title") or "")
        if title:
            profiles = {p for p, _f in files.values()}
            keys.extend(_norm_stem(f"{p}_{title}") for p in sorted(profiles))
        for k in keys:
            if k and k in files:
                return files[k]
        return None

    @staticmethod
    def _key(path: Path) -> tuple[str, int, int]:
        st = path.stat()
        return (str(path), int(st.st_mtime), st.st_size)

    def warm(self, paths: list[Path] | None = None) -> None:
        """Probe every un-cached file (blocking)."""
        if paths is None:
            paths = [f for _p, f in self._files().values()]
        for path in paths:
            try:
                key = self._key(path)
            except OSError:
                continue
            with self._lock:
                if key in self._durations:
                    continue
            dur = self.prober(path)
            with self._lock:
                self._durations[key] = dur

    def warm_async(self) -> None:
        with self._lock:
            if self._warming is not None and self._warming.is_alive():
                return
            self._warming = threading.Thread(target=self.warm, name="suno-track-warm", daemon=True)
            self._warming.start()

    def payload(self) -> dict[str, object]:
        clips = _load_clips(self.genres_dir)
        files = self._files()
        tracks: dict[str, dict[str, object]] = {}
        missing = False
        with_audio = 0
        for clip in clips:
            cid = str(clip["id"])
            hit = self._match(clip, files)
            created = clip.get("created_at") or clip.get("created")
            info: dict[str, object] = {
                "duration": None,
                "created": created if isinstance(created, str) and created else None,
                "created_source": "suno" if isinstance(created, str) and created else None,
                "audio": None,
            }
            if hit is not None:
                profile, path = hit
                with_audio += 1
                info["audio"] = "/media/" + quote(profile) + "/" + quote(path.name)
                try:
                    key = self._key(path)
                    with self._lock:
                        if key in self._durations:
                            info["duration"] = self._durations[key]
                        else:
                            missing = True
                    if info["created"] is None:
                        info["created"] = datetime.fromtimestamp(
                            path.stat().st_mtime, tz=UTC
                        ).isoformat(timespec="seconds")
                        info["created_source"] = "file"
                except OSError:
                    pass
            tracks[cid] = info
        if missing:
            self.warm_async()
        blocked = _blocked_count(self.archive)
        return {
            "tracks": tracks,
            "counts": {"archive": len(clips), "audio": with_audio, "blocked": blocked},
            "warming": missing,
        }


def _lyrics_payload(genres_dir: Path | None, raw_ids: str) -> dict[str, object]:
    """Lyrics for up to 40 clip ids: lyrics/<id>.txt, else clips.json's field."""
    ids = [i.strip() for i in raw_ids.split(",") if i.strip()][:40]
    ids = [i for i in ids if _CLIP_ID_RE.match(i)]
    out: dict[str, str] = {}
    if genres_dir is None or not ids:
        return {"lyrics": out}
    for cid in ids:
        f = genres_dir / "lyrics" / f"{cid}.txt"
        try:
            if f.is_file():
                out[cid] = f.read_text(encoding="utf-8")
        except OSError:
            pass
    rest = [i for i in ids if i not in out]
    if rest:
        data = _read_json_file(genres_dir / "clips.json")
        if isinstance(data, list):
            by_id = {str(c.get("id")): c for c in data if isinstance(c, dict)}
            for cid in rest:
                text = (by_id.get(cid) or {}).get("lyrics")
                if isinstance(text, str) and text:
                    out[cid] = text
    return {"lyrics": out}


_STATIC_DIR = Path(__file__).resolve().parent / "webui_static"
_STATIC_FILES = frozenset(
    {
        "suno-genre-card.js",
        "suno-genre-core.js",
        "suno-view-modes.js",
        "trackie-dacks.svg",
        "trackie-assistant-logo.webp",
    }
)


def _static_file(name: str) -> bytes | None:
    if name not in _STATIC_FILES:
        return None
    try:
        return (_STATIC_DIR / name).read_bytes()
    except OSError:
        return None


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Single 'bytes=a-b' range → inclusive (start, end); None = whole file."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    start_s, _, end_s = header[6:].strip().partition("-")
    try:
        if start_s == "":
            n = int(end_s)
            if n <= 0:
                return None
            return (max(0, size - n), size - 1)
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start >= size or end < start:
        return (-1, -1)
    return (start, min(end, size - 1))


_PAGE = """<!doctype html>
 <html lang="en"><head><meta charset="utf-8">
 <meta name="viewport" content="width=device-width,initial-scale=1">
 <title>Trackie Assistant</title>
 <style>
 :root{--blue:#2563eb;--bg:#0b0b0b;--chip:#1f2937}
 *{box-sizing:border-box}
 body{background:var(--bg);color:#ddd;font:14px/1.45 system-ui,sans-serif;
      margin:0;padding:12px 12px 24px}
 .top{display:flex;align-items:baseline;gap:10px;max-width:1320px;margin:0 auto 4px;padding:0 12px}
h1{font-size:1.05rem;margin:0;font-weight:600}
suno-genre-card{display:block;--header-height:0px;--card-background-color:#141414}
 .tools{margin:14px auto 0;max-width:1320px;padding:0 12px;display:grid;gap:10px;
        grid-template-columns:1fr 1fr}
.card{background:#1b1b1b;border:1px solid #333;border-radius:10px;padding:12px}
.card h2{font-size:.95rem;margin:0 0 8px;color:#9ad}
table{border-collapse:collapse;font-size:.85rem}
td,th{padding:.2rem .6rem;text-align:left;border-bottom:1px solid #2a2a2a;overflow-wrap:anywhere}
 input[type=text]{background:#111;color:#ddd;border:1px solid #444;border-radius:6px;
                   padding:.4rem .6rem;width:100%;max-width:22rem}
 pre{background:#0a0a0a;border:1px solid #2a2a2a;border-radius:6px;padding:.7rem;
     max-height:14rem;overflow:auto;white-space:pre-wrap;font-size:.8rem}
 button.act{min-height:36px;padding:7px 14px;border-radius:8px;border:none;
            cursor:pointer;font:inherit;font-size:.85rem;background:var(--blue);color:#fff}
button.act:disabled{opacity:.45;cursor:default}
.muted{color:#777}.note{font-size:.78rem;color:#8a8a8a}
.op{max-width:1296px;margin:8px auto;padding:10px 12px;border-radius:10px;background:#14213a;
    border:1px solid #2f4b80;font-size:.85rem;display:flex;flex-direction:column;gap:6px}
#opbar{position:sticky;top:0;z-index:5}
.op[data-stale="true"] .spin{animation:none}
.op .op-stale{font-weight:600;color:inherit}
.op .oprow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.op .opline{flex:1 1 200px;min-width:0;overflow-wrap:anywhere}
.op .spin{width:16px;height:16px;flex:none;border-radius:50%;border:2px solid #3b5a96;
    border-top-color:#9ecbff;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.op .bar{height:6px;border-radius:4px;background:#0d1320;overflow:hidden}
.op .bar i{display:block;height:100%;background:#4c8dff;transition:width .25s}
.op.failed{background:#2a1416;border-color:#8a2f36}
.op.cancelled{background:#1d1d1d;border-color:#555}
.op code{font-size:.78rem;background:#00000055;padding:1px 5px;border-radius:4px}
button.act.secondary{background:#1f2937;color:#eee;border:1px solid #444}
@media (max-width:700px){.tools{grid-template-columns:1fr}}
/* Independent view, appearance, and density selectors. */
body{--ui-bg:#0b0b0b;--ui-surface:#1b1b1b;--ui-raised:#141414;--ui-text:#e5e7eb;
 --ui-muted:#aaa;--ui-border:#414141;--ui-accent:#60a5fa;--ui-primary:#2563eb;
 --ui-input:#111;--ui-op-bg:#14213a;--ui-op-text:#f0f4fa;--ui-op-border:#6f91be;
 --ui-op-error-bg:#2a1416;--ui-op-error-text:#fff;--ui-op-error-border:#b85560;
 --ui-op-cancel-bg:#1d1d1d;--ui-op-cancel-text:#fff;--ui-op-code-bg:#0006;
 --ui-op-secondary-bg:#243551;--ui-warn:#fbbf24;
 color-scheme:dark;background:var(--ui-bg);color:var(--ui-text);padding:0 12px 28px}
body[data-theme="light"]{--ui-bg:#f4f6f9;--ui-surface:#fff;--ui-raised:#f8fafc;
 --ui-text:#172033;--ui-muted:#46576d;--ui-border:#afbbce;--ui-accent:#1245b4;
 --ui-primary:#2159c8;--ui-input:#fff;--ui-op-bg:#e7f0ff;--ui-op-text:#172033;
 --ui-op-border:#7398d5;--ui-op-error-bg:#ffeaed;--ui-op-error-text:#612033;
 --ui-op-error-border:#c97386;--ui-op-cancel-bg:#edf0f5;
 --ui-op-cancel-text:#172033;--ui-op-code-bg:#d5e2f3;
 --ui-op-secondary-bg:#dbe7f7;--ui-warn:#805000;color-scheme:light}
body[data-theme="contrast"]{--ui-bg:#000;--ui-surface:#070707;--ui-raised:#000;
 --ui-text:#fff;--ui-muted:#fff;--ui-border:#fff;--ui-accent:#ffeb3b;
 --ui-primary:#1345ff;--ui-input:#000;--ui-op-bg:#000;--ui-op-text:#fff;
 --ui-op-border:#fff;--ui-op-error-bg:#000;--ui-op-error-text:#fff;
 --ui-op-error-border:#ffeb3b;--ui-op-cancel-bg:#000;--ui-op-cancel-text:#fff;
 --ui-op-code-bg:#222;--ui-op-secondary-bg:#000;--ui-warn:#ffeb3b;color-scheme:dark}
@media(prefers-color-scheme:light){body[data-theme="system"]{
 --ui-bg:#f4f6f9;--ui-surface:#fff;--ui-raised:#f8fafc;--ui-text:#172033;
 --ui-muted:#46576d;--ui-border:#afbbce;--ui-accent:#1245b4;
 --ui-primary:#2159c8;--ui-input:#fff;--ui-op-bg:#e7f0ff;--ui-op-text:#172033;
 --ui-op-border:#7398d5;--ui-op-error-bg:#ffeaed;--ui-op-error-text:#612033;
 --ui-op-error-border:#c97386;--ui-op-cancel-bg:#edf0f5;
 --ui-op-cancel-text:#172033;--ui-op-code-bg:#d5e2f3;
 --ui-op-secondary-bg:#dbe7f7;--ui-warn:#805000;color-scheme:light}}
.top{align-items:center;justify-content:space-between;flex-wrap:wrap;
 gap:12px;padding:12px 8px;max-width:1510px}
.brand{display:flex;align-items:center;gap:10px;flex-wrap:wrap;min-width:0}
/* Display the mascot half of the approved 420x315 hero, without altering its bytes.
   The README retains the complete wordmark; the nearby h1 names the product here. */
.brand-mark{width:64px;height:108px;position:relative;overflow:hidden;flex:none}
.brand-mark img{position:absolute;left:-4px;top:-2px;width:150px;height:auto;
 max-width:none;display:block;animation:brand-sway 3.8s ease-in-out infinite;
 transform-origin:35px 94px}
@keyframes brand-sway{0%,100%{transform:rotate(-1deg)}50%{transform:rotate(1deg)}}
@media(prefers-reduced-motion:reduce){.brand-mark img{animation:none!important}}
.brand h1{font-size:1.15rem}
.connection-state{display:inline-flex;align-items:center;gap:6px;
 font-size:.78rem;font-weight:650;padding:5px 9px;border-radius:20px;
 border:1px solid var(--ui-border);color:var(--ui-text)}
.connection-state::before{content:"";width:8px;height:8px;border-radius:50%;
 background:var(--ui-muted)}
.connection-state[data-state="online"]::before{background:#29985b}
.connection-state[data-state="offline"]::before{background:#dc5252}
.connection-state[data-state="stale"]::before{background:#e5a020}
.operation-notice{margin-top:8px;font-size:.84rem;font-weight:600}
.operation-notice[hidden]{display:none}
.mode-controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.mode-controls label{display:flex;align-items:center;gap:6px;font-weight:600;font-size:.82rem}
.mode-controls select{min-height:44px;border:1px solid var(--ui-border);
 color:var(--ui-text);background:var(--ui-surface);border-radius:9px;
 padding:8px 11px;font:inherit;max-width:132px;cursor:pointer}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;
 overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.feature-nav{display:none;align-items:center;gap:7px;max-width:1500px;
 margin:0 auto 10px;padding:0 8px;flex-wrap:wrap}
.feature-nav button{min-height:46px;padding:9px 13px;font:inherit;
 font-weight:600;background:var(--ui-surface);color:var(--ui-text);
 border:1px solid var(--ui-border);border-radius:10px;cursor:pointer}
.feature-nav button[aria-pressed="true"]{border-color:var(--ui-accent);
 box-shadow:inset 0 -3px var(--ui-accent)}
.workspace{max-width:1500px;margin:auto}
.workspace [data-panel]{scroll-margin-top:18px}
.library-panel{min-width:0}
body[data-view="studio"] .feature-nav,body[data-view="minimal"] .feature-nav{display:flex}
body[data-view="studio"] .workspace{display:grid;
 grid-template-columns:minmax(0,1fr) minmax(290px,340px);gap:18px;align-items:start}
body[data-view="studio"] .tools{display:flex;flex-direction:column;gap:12px;
 max-width:none;padding:0;margin:12px 0 0}
body[data-view="studio"] .card{border-radius:16px;box-shadow:0 8px 24px #00000015}
body[data-view="studio"] .library-panel{border:1px solid var(--ui-border);
 border-radius:18px;background:var(--ui-raised);padding:2px;margin-top:12px}
body[data-view="minimal"] .workspace{max-width:940px}
body[data-view="minimal"] .tools{display:block;max-width:850px}
body[data-view="minimal"] .tools .card{max-width:700px;margin:12px auto}
body[data-view="minimal"] [data-panel]:not([data-active="true"]){display:none}
body[data-view="classic"] .activity-panel{grid-column:1/-1}
/* Readable status palettes for every appearance. */
.op{background:var(--ui-op-bg);color:var(--ui-op-text);border-color:var(--ui-op-border)}
.op.failed{background:var(--ui-op-error-bg);color:var(--ui-op-error-text);
 border-color:var(--ui-op-error-border)}
.op.cancelled{background:var(--ui-op-cancel-bg);color:var(--ui-op-cancel-text);
 border-color:var(--ui-border)}
.op .note{color:inherit}
.op code{background:var(--ui-op-code-bg)}
.op button.act.secondary{background:var(--ui-op-secondary-bg);color:inherit;
 border-color:var(--ui-op-border)}
.card{background:var(--ui-surface);border-color:var(--ui-border);color:var(--ui-text)}
.card h2{color:var(--ui-accent)}.muted,.note{color:var(--ui-muted)}
td,th{border-color:var(--ui-border)}
input[type=text]{color:var(--ui-text);background:var(--ui-input);border-color:var(--ui-border);
 min-height:44px;max-width:none}
pre{color:var(--ui-text);background:var(--ui-raised);border-color:var(--ui-border)}
button.act{min-height:44px;background:var(--ui-primary)}
button.act.secondary{background:var(--ui-raised);color:var(--ui-text);border-color:var(--ui-border)}
.card label{display:inline-flex;align-items:center;gap:6px;min-height:38px}
.card input[type=checkbox]{width:18px;height:18px}
.card .actions{display:flex;flex-wrap:wrap;gap:8px}
.activity-panel pre{overflow-wrap:anywhere}
body[data-density="compact"] .card{padding:9px}
body[data-density="compact"] .tools{gap:8px}
suno-genre-card{--sg-theme-surface:var(--ui-surface);--sg-theme-raised:var(--ui-raised);
 --sg-theme-text:var(--ui-text);--sg-theme-muted:var(--ui-muted);
 --sg-theme-border:var(--ui-border);--sg-theme-accent:var(--ui-accent);
 --sg-theme-primary:var(--ui-primary);--sg-theme-warn:var(--ui-warn);
 --sg-op-bg:var(--ui-op-bg);--sg-op-text:var(--ui-op-text);
 --sg-op-border:var(--ui-op-border);--sg-op-error-bg:var(--ui-op-error-bg);
 --sg-op-error-text:var(--ui-op-error-text);
 --sg-op-error-border:var(--ui-op-error-border);
 --sg-op-cancel-bg:var(--ui-op-cancel-bg);--sg-op-cancel-text:var(--ui-op-cancel-text);
 --sg-op-code-bg:var(--ui-op-code-bg);--sg-op-secondary-bg:var(--ui-op-secondary-bg)}
:is(button,select,input,textarea):focus-visible{outline:3px solid var(--ui-accent);
 outline-offset:3px}
/* Studio avoids putting a 700px-minimum genre layout beside a sidebar. */
@media(max-width:1199px){
 body[data-view="studio"] .workspace{display:block}
 body[data-view="studio"] .tools{display:block;margin:0;padding:0}
 body[data-view="studio"] .tools .card{margin:0 0 12px}
 body[data-view="studio"] [data-panel]:not([data-active="true"]){display:none}}
@media(max-width:700px){
 .top{align-items:flex-start}
 .mode-controls{width:100%;display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}
 .mode-controls label{flex-direction:column;align-items:stretch;gap:3px}
 .mode-controls select{width:100%;max-width:none}
 .feature-nav{display:grid!important;grid-template-columns:repeat(4,minmax(0,1fr))}
 body[data-view="classic"] [data-panel]:not([data-active="true"]){display:none}
 .feature-nav{position:sticky;top:0;z-index:12;background:var(--ui-bg);
  padding:7px 8px 9px;margin-bottom:5px}
 #opbar{top:58px}
 .feature-nav button{font-size:.75rem;padding:8px 4px;min-width:0}
 .tools{padding:0 8px}.card .actions button{flex:1 1 45%}}
@media(prefers-reduced-motion:reduce){.op .spin{animation:none;border-top-color:currentColor}
 .op .bar i{transition:none}}
</style>
<script type="module" src="static/suno-genre-card.js?v=9"></script>
</head><body data-view="classic" data-theme="system" data-density="comfortable">
<header class="top">
 <div class="brand"><span class="brand-mark" aria-hidden="true"><img
  src="static/trackie-assistant-logo.webp?v=1" alt="" width="420" height="315"></span>
  <div><h1>Trackie Assistant</h1><span class="muted" id="ver"></span></div>
  <span id="connection-state" class="connection-state" data-state="checking"
    role="status" aria-live="polite">Checking…</span></div>
 <div class="mode-controls" aria-label="Display preferences">
  <label for="view-mode">View<select id="view-mode" aria-label="Dashboard view">
   <option value="classic">Classic</option><option value="studio">Studio</option>
   <option value="minimal">Minimal</option></select></label>
  <label for="appearance-mode">Theme<select id="appearance-mode" aria-label="Appearance theme">
   <option value="system">System</option><option value="dark">Dark</option>
   <option value="light">Light</option>
   <option value="contrast">High contrast</option></select></label>
  <label for="density-mode">Density<select id="density-mode" aria-label="Display density">
   <option value="comfortable">Comfort</option>
   <option value="compact">Compact</option></select></label>
 </div>
</header>
<nav class="feature-nav" aria-label="Dashboard features" id="feature-nav">
 <button type="button" data-section="library" aria-controls="panel-library">Library</button>
 <button type="button" data-section="sync" aria-controls="panel-sync">Sync</button>
 <button type="button" data-section="recommend" aria-controls="panel-recommend">Discover</button>
 <button type="button" data-section="activity" aria-controls="panel-activity">Activity</button>
</nav>
<p id="view-status" class="sr-only" role="status" aria-live="polite"></p>
<div id="opbar"></div>
<p id="op-announcement" class="sr-only" role="status" aria-live="polite"
 aria-atomic="true"></p>
<main class="workspace" id="workspace">
<section id="panel-library" class="library-panel" data-panel="library" tabindex="-1"
 aria-label="Music library">
<suno-genre-card id="lib"></suno-genre-card>
</section>
<div class="tools">
 <section id="panel-sync" data-panel="sync" tabindex="-1" class="card sync-panel">
  <h2>Update your music</h2>
  <div id="status" class="muted">Checking library…</div>
  <div id="sync-capacity" class="note" aria-label="Download queue and storage">
    Queue size available after checking sources.</div>
  <label for="sync-sources">Playlists or profiles</label>
  <textarea id="sync-sources" rows="3" style="width:100%;box-sizing:border-box"
    placeholder="Paste one public Suno playlist URL or @profile per line"></textarea>
  <p class="note">Leave blank to use configured sources. Entered sources replace that list.
    Use playlist URLs when you want to be certain a particular playlist is included.</p>
  <div style="margin-top:.6rem">
   <label><input type="checkbox" id="extract-audio" checked>
    Allow temporary video sources for audio or missing artwork</label>
   <label><input type="checkbox" id="covers" checked> Save cover artwork</label>
  </div>
  <div class="actions" style="margin-top:.5rem">
   <button class="act secondary" id="sync-dry">Preview sync (no downloads)</button>
   <button class="act" id="sync-real">Download missing tracks</button>
  </div>
  <div class="note">Preview checks your selected playlists without downloading media.
    Existing recordings are skipped. Downloads are spaced at playback speed.
    A published video may be used as a source, but only audio and artwork are saved;
    the temporary video is discarded after verification.</div>
  <p id="sync-uncertain" class="operation-notice" role="alert" hidden>
    Download request outcome unknown. Check Activity before any new operation.
    <button class="act secondary" id="sync-unlock" type="button">
      I reviewed Activity — enable actions</button>
  </p>
 </section>
 <section id="panel-recommend" data-panel="recommend" tabindex="-1" class="card recommend-panel">
  <h2>Recommend</h2>
  <input type="text" id="cands" placeholder="@handle1 @handle2">
  <label><input type="checkbox" id="taste" checked> use taste</label>
  <div style="margin-top:.5rem"><button class="act" id="rec">Rank</button></div>
 </section>
 <section id="panel-activity" data-panel="activity" tabindex="-1" class="card activity-panel">
  <h2>Runs &amp; activity</h2>
  <div id="runs" class="muted">no runs yet</div>
 </section>
</div>
</main>
<script type="module" src="static/suno-view-modes.js?v=1"></script>
<script type="module">
await customElements.whenDefined('suno-genre-card');
document.getElementById('lib').setConfig({standalone:true,title:'Suno archive',
  library_url:'api/library',tracks_url:'api/tracks',lyrics_url:'api/lyrics',
  client_error_url:'api/client-error'});
</script>
<script>
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
async function j(url,opts){
  try{const r=await fetch(url,opts);
    return {code:r.status,body:await r.json().catch(()=>null)};}
  catch(e){return {code:0,body:{code:'ai_unreachable',cause_class:'Server unreachable',
    message:'Could not reach the Trackie Assistant server.',detail:String(e&&e.message||e),
    request_id:'',time:new Date().toISOString()}};}}
var shown=null,dismissed={},lastReq=null;
var refreshSeq=0,appliedSeq=0,pollHealth='initial';
var submitting=false,awaitingRun=null,uncertainReal=false;
const uncertainKey='suno.webui.real-operation-uncertain.v1';
try{uncertainReal=sessionStorage.getItem(uncertainKey)==='1';}catch(_){}
function actionLocks(busy=false){
  for(const id of ['sync-dry','sync-real','rec']){
    const button=document.getElementById(id);
    if(button)button.disabled=busy||submitting||!!awaitingRun||uncertainReal;
  }
  const note=document.getElementById('sync-uncertain');
  if(note)note.hidden=!uncertainReal;
}
function setConnection(state){
  const el=document.getElementById('connection-state');
  if(!el||el.dataset.state===state)return;
  el.dataset.state=state;
  el.textContent=state==='online'?'Online':state==='offline'?'Offline':'Stale connection';
}
function holdUncertainReal(){
  uncertainReal=true;
  try{sessionStorage.setItem(uncertainKey,'1');}catch(_){}
  actionLocks();
}
function pollAnnouncement(healthy){
  const next=healthy?'online':'offline';
  if(next===pollHealth)return;
  const previous=pollHealth;
  pollHealth=next;
  if(!healthy||previous==='offline'){
    document.getElementById('op-announcement').textContent=healthy
      ?'Connection restored; operation status updated.'
      :'Connection unavailable; operation progress may be outdated. Retrying.';
  }
}
function markRunStale(){
  if(shown&&shown.kind==='running'&&!shown.stale)setBar({...shown,stale:true});
  pollAnnouncement(false);
  setConnection("offline");
}
function opHtml(o){
  if(!o)return '';
  if(o.kind==='running'){
    const p=o.progress;const pct=p&&p.total?Math.round(100*p.done/p.total):null;
    const displayLine=(o.stale?'Last confirmed: ':'')+(o.line||'');
    let h='<div class="op" data-stale="'+(o.stale?'true':'false')+'"><div class="oprow">'
      +'<span class="spin" aria-hidden="true"></span>'
      +'<span class="opline" id="op-line">'+esc(displayLine)+'</span>'
      +(o.cancellable?'<button class="act secondary" id="op-cancel"'
        +(o.cancelling?' disabled':'')+'>Cancel</button>':'')
      +'</div>';
    h+='<div class="bar" id="op-bar" role="progressbar" aria-valuemin="0"'
      +' aria-valuemax="100" aria-label="'+(o.stale?'Last confirmed progress':'Run progress')+'"'
      +(pct===null?' hidden':' aria-valuenow="'+pct+'"')
      +'><i style="width:'+(pct===null?0:pct)+'%"></i></div>';
    h+='<div class="op-stale" id="op-stale"'+(o.stale?'':' hidden')
      +'>Connection unavailable; last confirmed progress shown. Retrying.</div>';
    return h+'</div>';}
  const e=o.error||{};const cancelled=o.kind==='cancelled';
  let when=e.time||'';try{when=new Date(e.time).toLocaleString();}catch(_){}
  return '<div class="op '+(cancelled?'cancelled':'failed')+'" role="alert" id="op-banner">'
    +'<div><b>'+esc(cancelled?'Cancelled':(e.cause_class||'Failed'))+'</b> &middot; '
    +esc(o.title)+'</div>'
    +'<div>'+esc(e.message||'')+'</div>'
    +'<div class="note">'+esc(o.outcome||'')+'</div>'
    +'<div class="note">Request <code>'+esc(e.request_id||'-')+'</code> &middot; '
    +esc(when)+'</div>'
    +'<div>'+(o.retryable?'<button class="act" id="op-retry">Retry preview</button> ':'')
    +(cancelled?'':'<button class="act secondary" id="op-copy">Copy details</button> ')
    +'<button class="act secondary" id="op-dismiss">Dismiss</button></div></div>';}
function detailsText(o){const e=o.error||{};
  return ['Action: '+o.title,'Cause: '+(e.cause_class||'?')+' ('+(e.code||'?')+')',
    'Message: '+(e.message||''),'Completed: '+(o.outcome||''),'Request id: '+(e.request_id||''),
    'Time: '+(e.time||''),'Detail: '+(e.detail||'')].join(String.fromCharCode(10));}
function setBar(o){
  const bar=document.getElementById('opbar');
  // Keep the same Cancel button and status nodes during routine polling:
  // replacing them every 2s discards keyboard focus and accessibility context.
  if(shown&&o&&shown.kind==='running'&&o.kind==='running'
      &&shown.runId===o.runId&&shown.cancellable===o.cancellable){
    const line=bar.querySelector('#op-line');
    const progress=bar.querySelector('#op-bar');
    const cancel=bar.querySelector('#op-cancel');
    const stale=bar.querySelector('#op-stale');
    const panel=bar.querySelector('.op');
    if(line&&progress&&stale&&panel&&(!o.cancellable||cancel)){
      panel.setAttribute('data-stale',o.stale?'true':'false');
      line.textContent=(o.stale?'Last confirmed: ':'')+(o.line||'');
      stale.hidden=!o.stale;
      if(cancel)cancel.disabled=!!o.cancelling;
      const p=o.progress;
      const pct=p&&p.total?Math.round(100*p.done/p.total):null;
      progress.hidden=pct===null;
      progress.setAttribute('aria-label',o.stale?'Last confirmed progress':'Run progress');
      if(pct===null)progress.removeAttribute('aria-valuenow');
      else progress.setAttribute('aria-valuenow',String(pct));
      const fill=progress.querySelector('i');
      if(fill)fill.style.width=(pct===null?0:pct)+'%';
      shown=o;
      return;
    }
  }
  shown=o;
  bar.innerHTML=opHtml(o);
}
function failFrom(title,code,body,done,retryable=false){
  const b=body||{};
  setBar({kind:'failed',title:title,error:b,retryable:retryable,
    outcome:done||'The action did not complete.'});}
document.getElementById('opbar').onclick=async function(ev){
  const id=ev.target&&ev.target.id;if(!id)return;
  if(id==='op-dismiss'){if(shown&&shown.runId)dismissed[shown.runId]=1;setBar(null);}
  else if(id==='op-copy'&&shown){const t=detailsText(shown);
    try{await navigator.clipboard.writeText(t);ev.target.textContent='Copied';}
    catch(_){window.prompt('Copy details',t);}}
  else if(id==='op-retry'&&lastReq&&lastReq.data.dry_run==='1'
      &&!submitting&&!awaitingRun&&!uncertainReal){
    if(shown&&shown.runId)dismissed[shown.runId]=1;
    setBar(null);post(lastReq.url,lastReq.data);}
  else if(id==='op-cancel'&&shown&&shown.runId){
    ev.target.disabled=true;
    const r=await j('api/runs/'+shown.runId+'/cancel',{method:'POST'});
    if(r.code!==200)failFrom('Cancel',r.code,r.body,'The run was not cancelled.');
    refresh();}};
function capacityBytes(n){
  if(!Number.isFinite(n)||n<0)return 'unknown';
  const units=['B','KiB','MiB','GiB','TiB'];let i=0;
  while(n>=1024&&i<units.length-1){n/=1024;i++;}
  return n.toLocaleString(undefined,{maximumFractionDigits:1})+' '+units[i];
}
function renderCapacity(storage,run){
  const panel=document.getElementById('sync-capacity');
  panel.replaceChildren();
  function line(text){const p=document.createElement('p');p.textContent=text;panel.append(p);}
  const c=run&&run.capacity,q=c&&c.queue;
  if(q&&Number.isInteger(q.count)&&q.count>=0){
    const prefix=run.kind==='sync (dry-run)'?'Preview':
      run.state==='running'?'Queued':'Last reported queue';
    line(prefix+' ('+(c.scope||'Current batch')+'): '+q.count+' track(s)');
    line('Estimated saved size: '+capacityBytes(q.estimated_bytes_min)+'–'+
      capacityBytes(q.estimated_bytes_max));
    if(q.duration_unknown)line(q.duration_unknown+
      ' track(s) have unknown length; assuming 4 minutes each.');
    line('Estimated audio and artwork only. Temporary video downloads and working space '+
      'are additional; network transfer size is unknown.');
    if(storage&&storage.available&&Number.isFinite(q.estimated_bytes_max)
        &&q.estimated_bytes_max>storage.free_bytes)
      line('Low space: the estimated queue may exceed available storage. '+
        'Free-space safety checks still apply.');
  }else line('Queued tracks and size will appear after sources are checked.');
  line(storage&&storage.available?
    'Destination free: '+capacityBytes(storage.free_bytes)+' of '+
      capacityBytes(storage.total_bytes)+
      ' · '+storage.destination:'Destination free space unavailable.');
}
async function refresh(){
  // Latest settled result wins; slow (>2s) polls remain usable.
  const seq=++refreshSeq;
  const s=await j('api/status');
  if(seq<appliedSeq)return;
  const statusEl=document.getElementById('status');
  if(s.code!==200||!s.body||typeof s.body!=='object'
      ||!s.body.blocked||typeof s.body.blocked.count!=='number'
      ||(s.body.watch&&!Array.isArray(s.body.watch))
      ||(s.body.profiles&&!Array.isArray(s.body.profiles))){
    appliedSeq=seq;
    statusEl.textContent='Status unavailable (offline or invalid response); retrying.';
    document.getElementById('runs').textContent='Activity may be stale until status returns.';
    markRunStale();
    document.getElementById('sync-capacity').textContent=
      'Queue and disk information unavailable; retrying.';
    return;
  }
  document.getElementById('ver').textContent='v'+s.body.version;
  let h='<table><tr><th>archive</th><td>'+esc(s.body.archive)+'</td></tr>';
  h+='<tr><th>engine</th><td>'+esc(s.body.engine)+'</td></tr>';
  h+='<tr><th>profiles</th><td>'+esc((s.body.watch||[]).join(', ')||'—')+'</td></tr>';
  h+='<tr><th>blocked</th><td>'+s.body.blocked.count+' clip(s)</td></tr></table>';
  if(s.body.profiles&&s.body.profiles.length){
    h+='<table><tr><th>profile</th><th>tracks</th><th>MB</th></tr>';
    for(const p of s.body.profiles){
      h+='<tr><td>'+esc(p.name)+'</td><td>'+p.tracks+'</td><td>'+p.size_mb+'</td></tr>';}
    h+='</table>';}
  document.getElementById('status').innerHTML=h;
  renderCapacity(s.body.storage,null);
  const r=await j('api/runs');
  if(seq<appliedSeq)return;
  appliedSeq=seq;
  const el=document.getElementById('runs');
  if(r.code!==200||!r.body||!Array.isArray(r.body.runs)){
    el.textContent='Run history unavailable; activity may be stale. Retrying.';
    markRunStale();
    setConnection('stale');
    return;
  }
  setConnection('online');
  pollAnnouncement(true);
  if(r.body.runs.length){
    el.classList.remove('muted');el.innerHTML='';
    for(const run of r.body.runs){
      const d=document.createElement('div');
      const head=document.createElement('div');
      head.textContent=run.kind+' — '+run.state+(run.returncode?' (exit '+run.returncode+')':'');
      const pre=document.createElement('pre');
      pre.textContent=(run.output||[]).join('\\n')||'(no output yet)';
      d.appendChild(head);d.appendChild(pre);el.appendChild(d);}}
  else{
    el.classList.add('muted');
    el.textContent='no runs yet';
  }
  const runs=r.body.runs;
  const active=runs.find(x=>x.state==='running'||x.state==='cancelling');
  const busy=!!active;
  renderCapacity(s.body.storage,active||runs.find(x=>x.kind==='sync'||x.kind==='sync (dry-run)'));
  if(awaitingRun&&runs.some(x=>x.id===awaitingRun))awaitingRun=null;
  actionLocks(busy);
  if(active){
    const p=active.progress;
    let line=active.kind+(active.state==='cancelling'?' - cancelling...':' - running...');
    if(p&&p.total)line+=' '+p.done+' of '+p.total;
    else if(p&&p.label)line+=' '+p.label;
    setBar({kind:'running',runId:active.id,line:line,progress:p,cancellable:true,
      cancelling:active.state==='cancelling'});
  }else if(shown&&shown.kind==='running'){setBar(null);}
  const last=runs[0];
  if(!active&&last&&(last.state==='failed'||last.state==='cancelled')&&!dismissed[last.id]
     &&!(shown&&shown.runId===last.id)){
    const p=last.progress;
    setBar({kind:last.state,runId:last.id,title:last.kind,error:last.error,
      outcome:(p&&p.total?p.done+' of '+p.total+' done, '+(p.total-p.done)+' not done. ':'')
        +(last.state==='cancelled'?'Stopped before it finished.'
          :'The run stopped with exit code '+last.returncode+'.')});}
}
async function post(url,data){
  if(submitting||awaitingRun||uncertainReal)return;
  const real=url==='api/sync'&&data.dry_run==='0';
  if(real&&!window.confirm('REAL DOWNLOAD: this writes files to the archive output folder. '
    +'Start now?'))return;
  // Lock before awaiting any network call; a double tap can start only once.
  submitting=true;actionLocks();
  const title=real?'Real download':data.dry_run==='1'?'Sync preview':'Recommend';
  try{
    const r=await j(url,{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams(data)});
    if(r.code===200&&r.body&&r.body.id){
      awaitingRun=r.body.id;lastReq=null;
      setBar({kind:'running',runId:r.body.id,line:title+' — accepted, checking progress…',
        progress:null,cancellable:false});
    }else if(r.code===503&&r.body&&r.body.code==='windows_archive_unavailable'){
      lastReq=null;
      failFrom(title,r.code,r.body,'Windows archive writes are unavailable; nothing was started.');
    }else if(r.code===503&&r.body&&r.body.code==='producer_unavailable'){
      lastReq=null;
      failFrom(title,r.code,r.body,'Producer verification is unavailable; nothing was started.');
    }else if(r.code===503&&r.body&&r.body.code==='tagging_unavailable'){
      lastReq=null;
      failFrom(title,r.code,r.body,'Tag updates are unavailable; nothing was started.');
    }else if(r.code===0||r.code>=500||r.code===200){
      // Transport failure, server failure, and malformed success are ambiguous:
      // never send the write request again on behalf of the user.
      if(real)holdUncertainReal();
      lastReq=null;
      failFrom(title,r.code,r.body,
        'Outcome unknown. Check Activity before considering a new request. No automatic retry.');
      setConnection(r.code===0?'offline':'stale');
    }else{
      // A definitive non-2xx rejection did not create a job. Even in this
      // case only preview can show an explicit, user-initiated retry.
      lastReq=data.dry_run==='1'?{url,data}:null;
      failFrom(title,r.code,r.body,'Server rejected the request; nothing was started.',
        !!lastReq);
    }
  }finally{
    submitting=false;actionLocks();
    refresh();
  }
}
function syncOpts(dry){
  return{dry_run:dry,sources:document.getElementById('sync-sources').value,videos:'0',extract_audio:document.getElementById('extract-audio').checked?'1':'0',
    covers:document.getElementById('covers').checked?'1':'0'};}
document.getElementById('sync-dry').onclick=()=>post('api/sync',syncOpts('1'));
document.getElementById('sync-real').onclick=()=>post('api/sync',syncOpts('0'));
document.getElementById('rec').onclick=()=>post('api/recommend',{candidates:document.getElementById('cands').value,taste:document.getElementById('taste').checked?'1':'0'});
document.getElementById('sync-unlock').onclick=()=>{
  if(!uncertainReal||pollHealth!=='online')return;
  // No re-submission: this only re-enables the buttons after a human review.
  uncertainReal=false;
  try{sessionStorage.removeItem(uncertainKey);}catch(_){}
  actionLocks();
};
actionLocks();
refresh(); setInterval(refresh,2000);
</script></body></html>
"""


def _make_handler(
    archive: Path,
    manager: RunManager,
    allowed_hosts: frozenset[str] = frozenset(),
    ingress_mode: bool = False,
    genres_dir: Path | None = None,
    cors_origins: frozenset[str] = frozenset(),
    tracks: TrackIndex | None = None,
    trusted_ingress_peer: str | None = None,
    auth: GatewayAuth | None = None,
) -> type[BaseHTTPRequestHandler]:
    gateway_auth = auth if auth is not None else GatewayAuth()
    # Resolve the fixed HA Supervisor service through Docker DNS. If DNS is
    # unavailable, fail closed before any unauthenticated ingress listener runs.
    # Do not trust request headers to identify the proxy.
    if ingress_mode and trusted_ingress_peer is None:
        trusted_ingress_peer = socket.gethostbyname("supervisor")
    track_index = tracks if tracks is not None else TrackIndex(archive, genres_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # keep the terminal clean; runs carry their own output

        def _json(self, code: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, err: ApiError, request_id: str | None = None) -> None:
            """Send the structured error envelope and log it (redacted)."""
            body = error_body(err, request_id)
            logger.warning(
                "[%s] %s %s -> %s %s: %s",
                body["request_id"],
                self.command,
                redact(self.path),
                err.status,
                err.code,
                body["detail"] or body["message"],
            )
            self._json(err.status or 500, body)

        def _cors(self) -> None:
            """Echo an allow-listed Origin (e.g. the HA dashboard), never '*'."""
            origin = self.headers.get("Origin")
            if origin and origin in cors_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def _media(self, media: Path) -> None:
            size = media.stat().st_size
            ctype, _enc = mimetypes.guess_type(media.name)
            rng = _parse_range(self.headers.get("Range"), size)
            if rng == (-1, -1):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
                return
            start, end = rng if rng else (0, size - 1)
            length = max(0, end - start + 1)
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self._cors()
            self.end_headers()
            try:
                with media.open("rb") as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser aborted a range read, normal while seeking

        def _reject_host(self) -> bool:
            # HA ingress authenticates upstream; the HTTP peer must be Supervisor.
            # X-Forwarded-For and X-Ingress-Path are attacker-controlled if a
            # different add-on connects directly to this internal-network port.
            if ingress_mode and self.client_address[0] != trusted_ingress_peer:
                self._json(403, {"error": "Home Assistant ingress proxy required"})
                return True
            if _host_allowed(self.headers.get("Host"), allowed_hosts, ingress=ingress_mode):
                return False
            self._fail(
                ApiError(
                    "forbidden_host",
                    "Host not allowed for this dashboard.",
                    str(self.headers.get("Host")),
                )
            )
            return True

        def _reject_auth(self, *, write: bool) -> bool:
            """Enforce the operator's gateway policy on the direct TCP peer."""
            if not gateway_auth.enabled:
                return False
            peer = str(self.client_address[0])
            if gateway_auth.credentials_match(self.headers.get("Authorization")):
                return False
            if gateway_auth.allows_write(peer) if write else gateway_auth.allows_read(peer):
                return False
            if gateway_auth.has_credentials:
                body = json.dumps({"error": "authentication required"}).encode()
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate", 'Basic realm="Trackie Assistant", charset="UTF-8"'
                )
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return True
            self._json(
                403,
                {
                    "error": (
                        "writes require authentication"
                        if write
                        else "this client is not in TRACKIE_GATEWAY_TRUSTED_CIDRS"
                    )
                },
            )
            return True

        def _guarded(self, fn: object) -> None:
            rid = new_request_id()
            try:
                fn()  # type: ignore[operator]
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser went away mid-response
            except Exception as exc:  # noqa: BLE001 - last-resort mapper
                logger.error(
                    "[%s] unhandled %s %s:\n%s",
                    rid,
                    self.command,
                    redact(self.path),
                    redact(traceback.format_exc()),
                )
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self._fail(classify_exception(exc), rid)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self._guarded(self._do_get)

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            self._guarded(self._do_post)

        def _do_get(self) -> None:
            if self._reject_host() or self._reject_auth(write=False):
                return
            path = urlparse(self.path).path
            if path == "/":
                page = _PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif path == "/api/status":
                self._json(200, _compute_status(archive))
            elif path == "/api/runs":
                self._json(200, {"runs": manager.list()})
            elif path == "/api/library":
                self._json(200, _library_payload(genres_dir, archive))
            elif path == "/api/tracks":
                self._json(200, track_index.payload())
            elif path == "/api/lyrics":
                ids = parse_qs(urlparse(self.path).query).get("ids", [""])[0]
                self._json(200, _lyrics_payload(genres_dir, ids))
            elif path == "/api/files":
                self._json(200, _list_files(archive))
            elif path.startswith("/static/"):
                body = _static_file(path[len("/static/") :])
                if body is None:
                    self._fail(ApiError("not_found", "Not found.", redact(self.path)))
                    return
                self.send_response(200)
                content_type = (
                    "image/svg+xml"
                    if path.endswith(".svg")
                    else "image/webp"
                    if path.endswith(".webp")
                    else "application/javascript; charset=utf-8"
                )
                self.send_header("Content-Type", content_type)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith("/media/"):
                rel = unquote(path[len("/media/") :])
                media = _safe_media_path(archive, rel)
                if media is None:
                    self._fail(ApiError("not_found", "Not found.", redact(self.path)))
                    return
                self._media(media)
            else:
                self._fail(ApiError("not_found", "Not found.", redact(self.path)))

        def _reject_cross_site_write(self) -> bool:
            """Block browser form CSRF on LAN mode (CORS alone does not block POST).

            Supervisor ingress authenticates the browser and rewrites Host, so
            only direct non-ingress access uses this same-origin check. An
            origin-less CLI client remains supported; this is not LAN auth.
            """
            origin = self.headers.get("Origin")
            if not origin or ingress_mode:
                return False
            if origin in cors_origins:
                return False
            try:
                parsed = urlparse(origin)
            except ValueError:
                self._json(403, {"error": "cross-site write origin not allowed"})
                return True
            host = (self.headers.get("Host") or "").lower()
            if (
                parsed.scheme in ("http", "https")
                and parsed.netloc.lower() == host
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and not parsed.username
                and not parsed.password
            ):
                return False
            self._json(403, {"error": "cross-site write origin not allowed"})
            return True

        def _do_post(self) -> None:
            if (
                self._reject_host()
                or self._reject_auth(write=True)
                or self._reject_cross_site_write()
            ):
                return
            path = urlparse(self.path).path
            # The form API expects small requests. Do not let a hostile LAN
            # client allocate an unbounded body, block a worker indefinitely,
            # or crash a handler on a malformed length/encoding. Run cancel
            # carries no payload, so an empty POST stays valid for it only;
            # every other endpoint requires a non-empty body so an empty POST
            # can never start a real sync.
            bodyless_ok = re.fullmatch(r"/api/runs/[0-9a-f]{8}/cancel", path) is not None
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                if not bodyless_ok:
                    self._json(411, {"error": "Content-Length required"})
                    return
                raw_length = "0"
            try:
                length = int(raw_length)
            except ValueError:
                self._json(400, {"error": "invalid Content-Length"})
                return
            if length < 0 or (length == 0 and not bodyless_ok):
                self._json(400, {"error": "empty or invalid request body"})
                return
            if length > 65536:
                self._json(413, {"error": "request body too large"})
                return
            try:
                self.connection.settimeout(15)
                raw_body = self.rfile.read(length)
                if len(raw_body) != length:
                    self._json(400, {"error": "incomplete request body"})
                    return
                form = (
                    parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
                    if "json" not in (self.headers.get("Content-Type") or "")
                    else {}
                )
            except (TimeoutError, OSError):
                self._json(408, {"error": "request body read timed out"})
                return
            except UnicodeDecodeError:
                self._json(400, {"error": "invalid request encoding"})
                return
            if path == "/api/sync":
                content_type = (
                    (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                )
                if content_type != "application/x-www-form-urlencoded":
                    self._json(415, {"error": "sync requires form-encoded options"})
                    return
                if form.get("dry_run") not in (["0"], ["1"]) or any(
                    key in form and form[key] not in (["0"], ["1"])
                    for key in ("videos", "covers", "extract_audio")
                ):
                    self._json(400, {"error": "invalid sync options; provide dry_run as 0 or 1"})
                    return
                dry = form.get("dry_run", ["0"])[0] == "1"
                try:
                    raw_sources = form.get("sources", [""])
                    if len(raw_sources) != 1 or len(raw_sources[0]) > 4096:
                        raise ValueError("Too many sources")
                    sources = tuple(
                        line.strip() for line in raw_sources[0].splitlines() if line.strip()
                    )
                    if len(sources) > 20:
                        raise ValueError("At most 20 sources")
                    for source in sources:
                        normalized, _ = normalize_watch_entry(source)
                        parts = urlparse(normalized)
                        if parts.query or parts.fragment or any(ord(c) < 32 for c in source):
                            raise ValueError("Use public source URLs without query strings")
                except ValueError:
                    self._json(
                        400, {"error": "Enter public Suno playlist URLs or @profiles, one per line"}
                    )
                    return
                if not dry:
                    try:
                        raise_if_unsupported_archive_write()
                    except WindowsArchiveWriteUnavailable:
                        self._json(
                            503,
                            {
                                "error": "Windows archive writes are unavailable until "
                                "crash-safe recovery is supported.",
                                "code": "windows_archive_unavailable",
                            },
                        )
                        return
                try:
                    _require_trusted_framing()
                except ProducerLookupError as exc:
                    self._json(503, {"error": str(exc), "code": "producer_unavailable"})
                    return
                if not dry:
                    try:
                        require_audio_sync(
                            keep_video=(form.get("videos", ["0"])[0] == "1" and not ingress_mode),
                        )
                    except TaggingUnavailable:
                        self._json(
                            503,
                            {
                                "error": "Embedded tag updates are unavailable until metadata "
                                "preservation is verified.",
                                "code": "tagging_unavailable",
                            },
                        )
                        return
                videos = form.get("videos", ["0"])[0] == "1"
                covers = form.get("covers", ["1"])[0] == "1"
                kind = "sync (dry-run)" if dry else "sync"
                run, error = manager.start(
                    kind,
                    _sync_command(
                        dry,
                        videos=videos,
                        covers=covers,
                        keep_video=not ingress_mode,
                        extract_audio=form.get("extract_audio", ["1"])[0] == "1",
                        sources=sources,
                    ),
                )
                if run is None:
                    self._fail(
                        ApiError(
                            "busy", "Another run is already active (one at a time).", str(error)
                        )
                    )
                else:
                    self._json(200, {"id": run.id, "request_id": run.request_id})
            elif path == "/api/recommend":
                raw = form.get("candidates", [""])[0].split()
                taste = form.get("taste", ["0"])[0] == "1"
                if not raw:
                    self._fail(
                        ApiError(
                            "bad_request",
                            "Enter at least one @handle to rank.",
                            "no candidates given",
                        )
                    )
                    return
                run, error = manager.start("recommend", _recommend_command(raw, taste, archive))
                if run is None:
                    self._fail(
                        ApiError(
                            "busy", "Another run is already active (one at a time).", str(error)
                        )
                    )
                else:
                    self._json(200, {"id": run.id, "request_id": run.request_id})
            elif re.fullmatch(r"/api/runs/[0-9a-f]{8}/cancel", path):
                run, err = manager.cancel(path.split("/")[3])
                if err is not None:
                    self._fail(err)
                else:
                    self._json(200, {"id": run.id if run else "", "state": "cancelling"})
            elif path == "/api/client-error":
                # The browser reports a failed long action (e.g. an Ask-AI split
                # that ran in the HA card) so the full cause lands in this log.
                if length > 8192:
                    self._fail(ApiError("bad_request", "Report too large.", str(length)))
                    return
                try:
                    report = json.loads(raw_body.decode() or "{}")
                except ValueError:
                    report = {}
                logger.error(
                    "[%s] client-reported %s failure code=%s cause=%s: %s",
                    redact(str(report.get("request_id", "-")))[:16],
                    redact(str(report.get("kind", "?")))[:32],
                    redact(str(report.get("code", "?")))[:32],
                    redact(str(report.get("cause", "?")))[:64],
                    redact(str(report.get("detail", "")))[:1000],
                )
                self._json(200, {"ok": True})
            else:
                self._fail(ApiError("not_found", "Not found.", redact(self.path)))

    return Handler


@click.command("webui")
@click.option("--port", type=click.IntRange(0, 65535), default=8760, show_default=True)
@click.option(
    "--host",
    "host_opt",
    default="127.0.0.1",
    show_default=True,
    help="Bind address: 127.0.0.1 (default), a LAN IP, or 0.0.0.0 for all interfaces.",
)
@click.option(
    "--archive",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path.cwd,
    help="Archive directory holding .env and .blocked (default: working directory).",
)
@click.option(
    "--ingress",
    is_flag=True,
    default=False,
    help="Behind a trusted reverse proxy (e.g. HA ingress): disable the Host guard.",
)
@click.option(
    "--genres-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory with genres.json + clips.json for the archive browser "
    "(default: SUNO_GENRES_DIR; none = library off).",
)
@click.option(
    "--cors-origin",
    "cors_origin",
    multiple=True,
    help="Origin allowed to read /api/tracks, /api/library and /api/lyrics cross-site "
    "(e.g. https://ha.example.lan:8123). Repeatable; default none.",
)
def webui_cli(
    port: int,
    host_opt: str,
    archive: Path,
    ingress: bool,
    genres_dir: Path | None,
    cors_origin: tuple[str, ...] = (),
) -> None:
    """Serve a local dashboard for driving suno-dl."""
    archive = archive.expanduser().resolve()
    if not (archive / ".env").exists():
        raise click.ClickException(
            f"no .env in {archive}; run suno-webui from your archive directory "
            "or pass --archive (the same directory you run suno-dl --sync from)."
        )
    manager = RunManager(archive)
    # Explicit archive configuration takes precedence over an unrelated cwd.
    # Preserve environment variables deliberately provided by the operator.
    load_dotenv(archive / ".env", override=False)
    library_root = _genres_dir(genres_dir)
    track_index = TrackIndex(archive, library_root)
    track_index.warm_async()  # ffprobe durations in the background
    lan = host_opt not in ("127.0.0.1", "localhost", "::1")
    allowed = _machine_addresses() if lan else frozenset()
    try:
        gateway_auth = GatewayAuth.from_env()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        server = ThreadingHTTPServer(
            (host_opt, port),
            _make_handler(
                archive,
                manager,
                allowed,
                ingress_mode=ingress,
                genres_dir=library_root,
                cors_origins=frozenset(o.rstrip("/") for o in cors_origin),
                tracks=track_index,
                auth=gateway_auth,
            ),
        )
    except OSError as exc:
        raise click.ClickException(
            f"cannot bind {host_opt}:{port} ({exc}); try another --port"
        ) from exc
    bound = server.server_address[1]
    if lan:
        policy = (
            "HTTP Basic required for every request"
            if gateway_auth.require_auth
            else (
                "read-only for TRACKIE_GATEWAY_TRUSTED_CIDRS; writes need credentials"
                if gateway_auth.lan_readonly
                else "no auth"
            )
        )
        click.echo(
            f"Trackie Assistant dashboard: http://{host_opt}:{bound}/  (Ctrl-C to stop)\n"
            f"  LAN MODE — {policy}.\n"
            "  Trust is the direct TCP peer only: forwarded IP headers are ignored;\n"
            "  the Host-header guard still accepts only this machine's names.\n"
            "  Do not port-forward it to the internet."
        )
    else:
        click.echo(f"Trackie Assistant dashboard: http://127.0.0.1:{bound}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nbye")


if __name__ == "__main__":  # pragma: no cover
    webui_cli()
