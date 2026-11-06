"""Profile recommendation for archive expansion (LAN JEV decision engines).

Why this module exists
----------------------
Suno exposes follow/follower *counts* publicly but not the *lists* (the list
endpoints require an authenticated session). What IS public:
profile pages (track titles, follower counts) and track pages. So instead of
crawling a social graph we cannot see, the flow is:

1. The user names candidate profiles (seed handles they actually follow —
   the "following/followed" knowledge lives with the user, where it is
   accurate).
2. This module fetches each candidate's public profile page and extracts
   public signals (track count, latest title, follower count when present).
3. Each candidate is framed as a decision question and scored by a JEV
   decision service only when the user configures an endpoint (any
   /v1/evaluate or /v1/systemone service works). If no JEV service is configured
   or the configured service does not answer,
   a deterministic heuristic ranks candidates so the command still works
   offline.

Taste-aware ranking
-------------------
``--taste-from`` (env ``SUNO_TASTE_FROM``) points at an archive directory
(the same layout ``suno-dl --sync`` writes: ``<dir>/<handle>/<handle>_<title>``).
Track titles mined from the filenames are fed into the JEV decision state, so
candidates are ranked for similarity to what the user actually kept rather
than size alone. When no JEV engine answers, the deterministic heuristic
ranks as before — it never pretends to know the user's taste.

Opt-in authenticated expansion
------------------------------
The follow/following LISTS exist behind the user's own session. With
``--session-cookie`` (env ``SUNO_SESSION_COOKIE``) the command may expand
a seed profile into its following/followers list (``--expand-following`` /
``--expand-followers``). The cookie travels in request headers only and is
never echoed, logged, or embedded in errors.

No new runtime dependencies: httpx (already required) does the HTTP, the
stdlib re does the signal extraction.

Decision-provider abstraction
-----------------------------
The JEV request/response handling lives in ``decisions.py``
(:class:`~suno_dl.decisions.DecisionClient`), which speaks the SystemOne
``{state, questions}`` contract to any provider — the local JEV fleet,
self-hosted Laya, or hosted SystemOne services (Bearer key via
``--jev-key``). This module only builds state text and interprets results.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import click
import httpx

from .config import ensure_dotenv_loaded
from .decisions import DecisionClient, DecisionError
from .fetcher import USER_AGENT

__all__ = [
    "CandidateProfile",
    "Recommendation",
    "_parse_follow_list",
    "recommend_profiles",
]

# Sanity cap on graph-expanded candidates: keeps the JEV question and the
# page-fetch burst bounded (politeness toward suno.com).
MAX_EXPANDED_CANDIDATES = 20

# An unconfigured installation must never probe a site-specific LAN address.
# Without an explicitly supplied provider, ranking is deterministic/offline.
DEFAULT_JEV_URL = os.environ.get("SUNO_JEV_URL", "")
DEFAULT_JEV_MODEL = os.environ.get("SUNO_JEV_MODEL") or None

# Bound the taste context fed to the JEV engine: the state payload should
# stay proportionate regardless of archive size. Titles are the signal.
MAX_TASTE_TITLES = 40
_TASTE_SUFFIXES = (".m4a", ".mp4")

_FOLLOWERS_RE = re.compile(r'"followers_count"\s*:\s*"?(\d+)"?')
_TITLE_RE = re.compile(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"')
# Pure navigation/UI labels that appear as "title" fields in Suno's page
# payload without belonging to any track. Kept minimal and evidence-based:
# section headings and modal chrome observed on real profile pages.
_UI_LABELS = {
    "followers",
    "following",
    "likes",
    "library",
    "playlists",
    "get the full app experience",
    "sharing settings",
    "publish song",
    "edit song details",
}


@dataclass
class SignalTrack:
    """A bare title scraped off a profile page — not a full TrackInfo.

    The recommender only ranks; downloads still flow through the parser's
    strictly-validated TrackInfo, so signal scraping stays lenient.
    """

    title: str


@dataclass
class CandidateProfile:
    """A user-named candidate profile plus its public signals."""

    handle: str
    url: str
    tracks: list[SignalTrack] = field(default_factory=list)
    followers: int | None = None

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    @property
    def latest_title(self) -> str:
        return self.tracks[-1].title if self.tracks else "(unknown)"


@dataclass
class Recommendation:
    """One candidate's score, the reason behind it, and where it came from."""

    handle: str
    track_count: int
    followers: int | None
    latest_title: str
    score: float
    rationale: str
    engine: str  # "jev:<model>@host" or "heuristic"


def _profile_url(entry: str) -> tuple[str, str]:
    """Normalize a handle or URL to (profile_url, handle)."""
    text = entry.strip()
    if text.startswith("@"):
        text = text[1:]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        # A full URL — take the handle out of it.
        match = re.match(r"https?://suno\.com/@([A-Za-z0-9_.-]+)/?$", text, re.I)
        if not match:
            raise click.ClickException(f"Candidate must be a handle or profile URL, got: {entry!r}")
        text = match.group(1)
    url = f"https://suno.com/@{text}"
    # Containment choke point: every URL this module fetches must be a
    # profile page on suno.com. (is_allowed_media_url is the CDN-media
    # guard and deliberately not reused here.)
    if urlparse(url).hostname != "suno.com":  # pragma: no cover - defensive
        raise click.ClickException(f"Refusing non-Suno URL: {url}")
    return url, text


def _follow_list_url(handle: str, kind: str) -> str:
    """Build the authenticated follow-list URL for a handle.

    ``kind`` is "following" or "followers". The endpoint is not public
    (404 without a session — verified 2026-09-28); callers must pass the
    session cookie via headers only.
    """
    if kind not in ("following", "followers"):
        raise ValueError(f"unknown follow-list kind: {kind!r}")
    text = handle.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        raise click.ClickException(f"Invalid handle for --expand-{kind}: {handle!r}")
    return f"https://suno.com/api/profiles/{text}/{kind}"


def _parse_follow_list(payload: dict | list) -> list[str]:
    """Extract handles from a follow-list response, tolerating shape drift.

    The authed endpoint's exact shape is unverified (needs a live session),
    so this accepts the plausible families: a bare list, or an object
    wrapping the list under ``profiles``/``following``/``followers``/
    ``items``. Items may be handle strings or objects keyed by
    ``username``/``handle``/``name``. Output is order-preserving, deduped,
    and stripped of leading ``@``.
    """
    items: list = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("profiles", "following", "followers", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
    handles: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            handles.append(item.strip().lstrip("@"))
        elif isinstance(item, dict):
            for key in ("username", "handle", "name"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    handles.append(value.strip().lstrip("@"))
                    break
    seen: set[str] = set()
    out: list[str] = []
    for handle in handles:
        if handle and handle not in seen:
            seen.add(handle)
            out.append(handle)
    return out


def _unescape(title: str) -> str:
    return title.replace("\\u0026", "&").replace('\\"', '"').replace("\\/", "/")


def _parse_profile_signals(html: str) -> tuple[list[SignalTrack], int | None]:
    """Extract public signals from a profile page: tracks + follower count.

    Suno profile pages ship their data as Next.js flight payloads whose
    quotes are escaped (``\\"title\\":\\"...``), so the escaped-quote form is
    normalized once before the plain-JSON regexes run.
    """
    html = html.replace('\\"', '"')
    seen: set[str] = set()
    tracks: list[SignalTrack] = []
    for match in _TITLE_RE.finditer(html):
        title = _unescape(match.group(1))
        if not title or title.lower() in _UI_LABELS or title in seen:
            continue
        seen.add(title)
        tracks.append(SignalTrack(title=title))
    followers_match = _FOLLOWERS_RE.search(html)
    followers = int(followers_match.group(1)) if followers_match else None
    return tracks, followers


# ─────────────────────────── JEV decision layer ───────────────────────────


def _taste_titles_from_archive(path: str | os.PathLike[str]) -> list[str]:
    """Mine track titles from an archive tree written by ``suno-dl --sync``.

    The layout is ``<root>/[downloads/]<handle>/<handle>_<title>.<ext>``
    (the default ``{handle}_{title}`` filename pattern), so each file's
    parent directory name is the authoritative prefix to strip — this is
    why per-profile subfolders matter. The handle directory may sit at any
    depth under the root, so the walk is recursive. m4a and mp4 pairs
    dedupe to one title. Missing directories return ``[]``: taste is
    additive signal, never a hard failure.
    """
    root = Path(path)
    try:
        media: list[Path] = []
        for suffix in _TASTE_SUFFIXES:
            media.extend(root.rglob(f"*{suffix}"))
    except OSError:
        return []
    titles: list[str] = []
    seen: set[str] = set()
    for media_path in sorted(media):
        if not media_path.is_file():
            continue
        handle = media_path.parent.name
        stem = media_path.stem
        # Strip the handle prefix the {handle}_{title} pattern wrote;
        # tolerate the legacy flat layout (no prefix) too.
        if handle and stem.lower().startswith(handle.lower() + "_"):
            stem = stem[len(handle) + 1 :]
        title = stem.replace("_", " ").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(title)
    return titles[:MAX_TASTE_TITLES]


def _heuristic_score(candidate: CandidateProfile) -> tuple[float, str]:
    """Deterministic fallback: bigger + more-followed catalogs rank higher.

    Log-scaled so a 500-track profile does not flatten the signal from a
    10-track one; follower count only contributes when the page exposed it.
    """
    if candidate.track_count == 0:
        return 0.0, "no public tracks visible — nothing to archive"
    size_term = min(1.0, candidate.track_count / 100)
    follower_term = 0.0
    if candidate.followers is not None:
        follower_term = min(0.5, candidate.followers / 200)
    score = 0.5 + 0.3 * size_term + follower_term
    n = candidate.track_count
    reason = f"{n} public track{'s' if n != 1 else ''}" + (
        f", {candidate.followers} followers" if candidate.followers is not None else ""
    )
    return min(score, 1.0), reason


def _jev_rank_sync(
    candidates: list[CandidateProfile],
    jev_url: str,
    timeout: float,
    taste_titles: list[str] | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> tuple[dict[str, Recommendation], str] | None:
    """Ask a JEV service to rank all candidates in one decision.

    Returns (per-handle recommendations, engine label), or None if the
    service is unreachable/misbehaving — callers then fall back to the
    deterministic heuristic.
    """
    if not jev_url:
        return None
    options = [c.handle for c in candidates]
    # Never derive a display label from the URL: it may contain credentials,
    # bearer tokens, or other private endpoint details.
    engine = f"jev:{model or 'default'}" + ("+taste" if taste_titles else "")
    lines = [
        f"- {c.handle}: {c.track_count} public tracks"
        + (f", {c.followers} followers" if c.followers is not None else "")
        + f"; latest: {c.latest_title}"
        for c in candidates
    ]
    taste_block = ""
    if taste_titles:
        sample = "; ".join(f'"{t}"' for t in taste_titles[:MAX_TASTE_TITLES])
        taste_block = (
            "\n\nTitles already in the user's archive (their taste signal):\n"
            + sample
            + "\nWeight similarity to these titles alongside the size/audience signals."
        )
    instruction = (
        "Rank by similarity to the archived titles and overall archival value."
        if taste_titles
        else "Rank by archival value: catalog size, activity, and evidence of an engaged audience."
    )
    state = (
        "The user archives public Suno music profiles. Current archive is "
        "complete for existing seeds. Candidate profiles to expand into:\n"
        + "\n".join(lines)
        + taste_block
        + "\n"
        + instruction
    )
    client = DecisionClient(jev_url, api_key=api_key, model=model, timeout=timeout)
    try:
        picked = client.ask(state, "Which profile should be archived next?", options)
    except DecisionError:
        return None
    label, confidence = picked.label, picked.confidence
    # The JEV engine picks a winner; every candidate still needs a row. Blend
    # the engine's pick confidence with the deterministic heuristic, then
    # word the rationale by FINAL position so the top row never contradicts
    # the ranking (the engine's raw pick is recorded when it differs).
    blended: list[tuple[float, CandidateProfile, bool]] = []
    for candidate in candidates:
        is_pick = candidate.handle == label
        engine_score = max(0.5, confidence) if is_pick else max(0.05, min(0.49, confidence * 0.5))
        fallback_score, _ = _heuristic_score(candidate)
        blended.append(((engine_score + fallback_score) / 2, candidate, is_pick))
    blended.sort(key=lambda item: item[0], reverse=True)
    recs: dict[str, Recommendation] = {}
    total = len(blended)
    for position, (score, candidate, is_pick) in enumerate(blended, start=1):
        _, fallback_reason = _heuristic_score(candidate)
        pick_note = f"JEV pick (confidence {confidence:.2f})" if is_pick else "not the JEV pick"
        position_note = "recommended next" if position == 1 else f"alternative {position}/{total}"
        recs[candidate.handle] = Recommendation(
            handle=candidate.handle,
            track_count=candidate.track_count,
            followers=candidate.followers,
            latest_title=candidate.latest_title,
            score=round(score, 3),
            rationale=f"{position_note}; {pick_note}; {fallback_reason}",
            engine=engine,
        )
    return recs, engine


async def _expand_follow_list(
    handle: str,
    kind: str,
    cookie: str,
    timeout: float = 20.0,
) -> list[str]:
    """Fetch a seed profile's following/followers list with the user session.

    The cookie travels in request headers only; on any failure this returns
    an empty list after a stderr warning — the cookie itself is never
    included in a message (redaction discipline from urls.redact_url).
    """
    url = _follow_list_url(handle, kind)
    headers = {
        "Cookie": cookie,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        click.echo(
            f"warn: could not fetch {kind} list of @{handle}: "
            f"{type(exc).__name__} (session cookie may be missing or expired)",
            err=True,
        )
        return []
    handles = _parse_follow_list(payload)
    if not handles:
        click.echo(
            f"warn: @{handle} {kind} list returned no recognizable handles "
            "(response shape may differ from expectations)",
            err=True,
        )
    return handles


async def recommend_profiles(
    candidates: list[str],
    fetch_html: Callable[[str], Awaitable[str] | str] | None = None,
    jev_url: str = DEFAULT_JEV_URL,
    jev_timeout: float = 20.0,
    session_cookie: str | None = None,
    expand_kinds: tuple[str, ...] = (),
    taste_titles: list[str] | None = None,
    jev_model: str | None = None,
    jev_api_key: str | None = None,
) -> list[Recommendation]:
    """Rank candidate profiles for archive expansion.

    ``fetch_html(url) -> str`` is an injectable fetcher (tests pass canned
    HTML); by default a shared httpx.AsyncClient with browser-ish headers
    fetches the public pages.

    With ``session_cookie`` and ``expand_kinds`` ("following" and/or
    "followers"), each seed is first expanded into its follow list using
    the user's own session; the merged candidate set is capped at
    ``MAX_EXPANDED_CANDIDATES``.
    """
    if not candidates:
        return []

    seeds = list(candidates)
    if session_cookie and expand_kinds:
        expanded: list[str] = []
        for seed in seeds:
            try:
                _, seed_handle = _profile_url(seed)
            except click.ClickException:
                seed_handle = seed.strip().lstrip("@")
            for kind in expand_kinds:
                for found in await _expand_follow_list(seed_handle, kind, session_cookie):
                    if found not in expanded:
                        expanded.append(found)
        merged = list(seeds)
        for handle in expanded:
            if handle not in merged:
                merged.append(handle)
        candidates = merged[:MAX_EXPANDED_CANDIDATES]

    async def _default_fetch(url: str) -> str:
        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(
            headers=headers, timeout=30.0, follow_redirects=True
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    fetch = fetch_html or _default_fetch
    fetched = await asyncio.gather(
        *(_fetch_with(fetch, entry) for entry in candidates), return_exceptions=True
    )
    candidates_ok: list[CandidateProfile] = []
    for entry, result in zip(candidates, fetched, strict=True):
        if isinstance(result, BaseException):
            click.echo(f"warn: could not fetch {entry}: {result}", err=True)
            continue
        candidates_ok.append(result)
    if not candidates_ok:
        return []

    ranked = _jev_rank_sync(
        candidates_ok, jev_url, jev_timeout, taste_titles, jev_model, jev_api_key
    )
    if ranked is None:
        engine = "heuristic"
        recs = {}
        for candidate in candidates_ok:
            score, reason = _heuristic_score(candidate)
            recs[candidate.handle] = Recommendation(
                handle=candidate.handle,
                track_count=candidate.track_count,
                followers=candidate.followers,
                latest_title=candidate.latest_title,
                score=round(score, 3),
                rationale=reason,
                engine=engine,
            )
    else:
        recs, engine = ranked

    return sorted(recs.values(), key=lambda r: r.score, reverse=True)


async def _fetch_with(fetch, entry: str) -> CandidateProfile:
    url, handle = _profile_url(entry)
    fetched = fetch(url)
    html = await fetched if inspect.isawaitable(fetched) else fetched
    if isinstance(html, tuple):
        tracks, followers = html
        return CandidateProfile(handle=handle, url=url, tracks=tracks, followers=followers)
    tracks, followers = _parse_profile_signals(html)
    return CandidateProfile(handle=handle, url=url, tracks=tracks, followers=followers)


# ───────────────────────────────── CLI ─────────────────────────────────


@click.command("recommend")
@click.argument("candidates", nargs=-1, required=True)
@click.option(
    "--jev-url",
    default=None,
    help="SystemOne endpoint (env SUNO_JEV_URL, .env-aware)",
    show_default=False,
)
@click.option(
    "--jev-model",
    default=None,
    help="Model name sent to the engine (env SUNO_JEV_MODEL)",
    show_default=False,
)
@click.option(
    "--jev-key",
    default=None,
    help="Bearer token for hosted SystemOne providers (env SUNO_JEV_KEY; never logged)",
)
@click.option(
    "--session-cookie",
    default=None,
    help="Suno session cookie (NAME=VALUE) for opt-in follow-list expansion "
    "(env SUNO_SESSION_COOKIE; never logged)",
)
@click.option(
    "--expand-following",
    "expand_following",
    is_flag=True,
    help="Expand each seed into its 'following' list (requires --session-cookie)",
)
@click.option(
    "--expand-followers",
    "expand_followers",
    is_flag=True,
    help="Expand each seed into its 'followers' list (requires --session-cookie)",
)
@click.option(
    "--taste-from",
    "taste_from",
    default=None,
    type=click.Path(file_okay=False),
    help="Archive directory whose track titles feed the ranking "
    "(env SUNO_TASTE_FROM, .env-aware; e.g. ~/suno-archive)",
)
def recommend_cli(
    candidates: tuple[str, ...],
    jev_url: str | None,
    jev_model: str | None,
    jev_key: str | None,
    session_cookie: str | None,
    expand_following: bool,
    expand_followers: bool,
    taste_from: str | None,
) -> None:
    """Rank candidate Suno profiles for archive expansion via a LAN JEV engine.

    CANDIDATES are handles (@name) or profile URLs the user actually follows.
    With --session-cookie, --expand-following/--expand-followers pull the
    seed's follow lists from the user's own authenticated session and rank
    the whole graph neighborhood.
    ``--jev-key``/``SUNO_JEV_KEY`` adds a Bearer token for hosted
    SystemOne providers; it travels in request headers only and is never
    echoed or logged.
    """
    # .env must be loaded before any env-var resolution: click's envvar=
    # option hooks read the process environment during parameter parsing,
    # i.e. BEFORE this callback runs, so they can never see .env values.
    # Every env-backed option therefore resolves manually, here, where the
    # .env is guaranteed to be in the environment (same lookup order as
    # everywhere else in the CLI: flag > environment/.env > built-in default).
    ensure_dotenv_loaded()
    jev_url = jev_url or os.getenv("SUNO_JEV_URL") or DEFAULT_JEV_URL
    jev_model = jev_model or os.getenv("SUNO_JEV_MODEL") or DEFAULT_JEV_MODEL
    jev_key = jev_key or os.getenv("SUNO_JEV_KEY")
    session_cookie = session_cookie or os.getenv("SUNO_SESSION_COOKIE")
    taste_from = taste_from or os.getenv("SUNO_TASTE_FROM")
    expand_kinds: tuple[str, ...] = ()
    if expand_following:
        expand_kinds += ("following",)
    if expand_followers:
        expand_kinds += ("followers",)
    if expand_kinds and not session_cookie:
        raise click.ClickException("--expand-following/--expand-followers require --session-cookie")
    taste_titles: list[str] | None = None
    if taste_from:
        taste_titles = _taste_titles_from_archive(taste_from)
        if taste_titles:
            click.echo(f"Taste signal: {len(taste_titles)} archived titles loaded", err=True)
        else:
            click.echo(f"warn: no archived titles found under {taste_from}", err=True)
    import time as _time

    started = _time.monotonic()
    recs = asyncio.run(
        recommend_profiles(
            list(candidates),
            jev_url=jev_url,
            session_cookie=session_cookie,
            expand_kinds=expand_kinds,
            taste_titles=taste_titles,
            jev_model=jev_model,
            jev_api_key=jev_key,
        )
    )
    if not recs:
        click.echo("No candidates could be scored.")
        raise SystemExit(1)
    width = max(len(r.handle) for r in recs) + 2
    click.echo(f"{'PROFILE':<{width}}{'SCORE':>7}  {'TRACKS':>6}  {'FOLLOWS':>7}  EVIDENCE")
    for rec in recs:
        followers = str(rec.followers) if rec.followers is not None else "?"
        click.echo(
            f"{rec.handle:<{width}}{rec.score:>7.3f}  {rec.track_count:>6}  "
            f"{followers:>7}  {rec.rationale} [{rec.engine}]"
        )
    click.echo(f"\nScored {len(recs)} candidate(s) in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":  # pragma: no cover
    recommend_cli()
