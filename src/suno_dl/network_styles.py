"""Network style analysis for Suno profiles.

Analyzes the style tags of a profile's network (followers + following)
against a reference catalogue to find absent/rare styles.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import ensure_dotenv_loaded
from .discovery import discover_profile_tracks_feed
from .models import TrackInfo
from .recommend import _expand_follow_list

# Shipped inside the package (see [tool.setuptools.package-data]); the old path
# pointed at tests/fixtures, which does not exist in an installed wheel, so
# every installed run silently analysed against an empty catalogue.
DEFAULT_CATALOGUE_PATH = Path(__file__).with_name("data") / "genres.json"
DEFAULT_NETWORK_CAP = 20
DEFAULT_RATE_LIMIT_SEC = 1.5

_FOLLOWERS_RE = re.compile(r'"followers_count"\s*:\s*"?(\d+)"?')
_FOLLOWING_RE = re.compile(r'"following_count"\s*:\s*"?(\d+)"?')


@dataclass
class NetworkProfile:
    """A profile in the network with its track metadata."""

    handle: str
    url: str
    tracks: list[TrackInfo] = field(default_factory=list)
    followers: int | None = None
    following: int | None = None


@dataclass
class StyleStats:
    """Style tag statistics for a profile."""

    handle: str
    track_count: int
    style_counts: Counter[str]
    play_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class NetworkStyleAnalysis:
    """Complete network style analysis result."""

    seed_handle: str
    network_profiles: list[NetworkProfile]
    aggregate_styles: Counter[str]
    catalogue_styles: Counter[str]
    absent_styles: list[str]
    rare_styles: list[tuple[str, int]]
    style_rankings: list[dict[str, Any]]


def _catalogue_path() -> Path:
    """Get the catalogue path from env or use default fixture."""
    ensure_dotenv_loaded()
    return Path(os.getenv("SUNO_CATALOGUE_PATH", str(DEFAULT_CATALOGUE_PATH)))


def load_catalogue(path: Path | None = None) -> dict[str, Any]:
    """Load the genre/style catalogue from JSON file.

    Expected structure:
    {
        "genres": {
            "genre_name": ["style1", "style2", ...],
            ...
        },
        "songs": [
            {"id": "...", "title": "...", "styles": ["style1", ...], "genre": "genre_name"},
            ...
        ]
    }
    """
    catalogue_path = path or _catalogue_path()
    if not catalogue_path.is_file():
        return {"genres": {}, "songs": []}
    with catalogue_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def catalogue_style_counter(catalogue: dict[str, Any]) -> Counter[str]:
    """Extract a Counter of all style tags from the catalogue."""
    counter: Counter[str] = Counter()
    for song in catalogue.get("songs", []):
        for style in song.get("styles", []):
            counter[style.lower()] += 1
    return counter


def catalogue_genre_groups(catalogue: dict[str, Any]) -> dict[str, list[str]]:
    """Extract genre -> styles mapping from catalogue."""
    return catalogue.get("genres", {})


async def _fetch_profile_page(
    client: httpx.AsyncClient, handle: str
) -> tuple[str, int | None, int | None]:
    """Fetch a profile page and return HTML + follower/following counts."""
    url = f"https://suno.com/@{handle.lstrip('@')}"
    resp = await client.get(url, timeout=30.0)
    resp.raise_for_status()
    html = resp.text

    followers_match = _FOLLOWERS_RE.search(html)
    followers = int(followers_match.group(1)) if followers_match else None

    following_match = _FOLLOWING_RE.search(html)
    following = int(following_match.group(1)) if following_match else None

    return html, followers, following


async def _fetch_tracks_with_styles(
    handle: str,
    rate_limit_sec: float,
    max_tracks: int,
) -> tuple[list[TrackInfo], int | None, int | None]:
    """Fetch tracks with style tags for a profile using the feed API."""
    tracks = await discover_profile_tracks_feed(
        handle=handle,
        timeout=30.0,
        max_pages=50,
        rate_limit_sec=rate_limit_sec,
    )
    if max_tracks and len(tracks) > max_tracks:
        tracks = tracks[:max_tracks]

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        follow_redirects=True,
    ) as client:
        _, followers, following = await _fetch_profile_page(client, handle)

    return tracks, followers, following


async def fetch_network_profiles(
    seed_handle: str,
    session_cookie: str | None,
    rate_limit_sec: float = DEFAULT_RATE_LIMIT_SEC,
    network_cap: int = DEFAULT_NETWORK_CAP,
    max_tracks_per_profile: int = DEFAULT_NETWORK_CAP,
) -> list[NetworkProfile]:
    """Fetch network profiles (followers + following) with their track metadata."""
    ensure_dotenv_loaded()

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        follow_redirects=True,
    ) as client:
        seed_html, seed_followers, seed_following = await _fetch_profile_page(client, seed_handle)

    following_handles: list[str] = []
    follower_handles: list[str] = []

    if session_cookie:
        following_handles = await _expand_follow_list(
            seed_handle.lstrip("@"), "following", session_cookie
        )
        follower_handles = await _expand_follow_list(
            seed_handle.lstrip("@"), "followers", session_cookie
        )

    following_handles = following_handles[:network_cap]
    follower_handles = follower_handles[:network_cap]

    all_handles = [seed_handle.lstrip("@")] + following_handles + follower_handles
    unique_handles = list(dict.fromkeys(all_handles))  # order-preserving de-dup

    network_profiles: list[NetworkProfile] = []

    for handle in unique_handles:
        tracks, followers, following = await _fetch_tracks_with_styles(
            handle, rate_limit_sec, max_tracks_per_profile
        )
        profile = NetworkProfile(
            handle=handle,
            url=f"https://suno.com/@{handle}",
            tracks=tracks,
            followers=followers,
            following=following,
        )
        network_profiles.append(profile)

    return network_profiles


def analyze_network_styles(
    network_profiles: list[NetworkProfile],
    catalogue: dict[str, Any],
) -> NetworkStyleAnalysis:
    """Analyze network styles against catalogue."""
    aggregate_styles: Counter[str] = Counter()
    catalogue_styles = catalogue_style_counter(catalogue)

    for profile in network_profiles:
        for track in profile.tracks:
            style_tags = getattr(track, "style_tags", None)
            if style_tags:
                for style in style_tags:
                    aggregate_styles[style.lower()] += 1

    all_catalogue_styles = set(catalogue_styles.keys())
    all_network_styles = set(aggregate_styles.keys())

    absent_styles = sorted(all_catalogue_styles - all_network_styles)
    rare_styles = [
        (style, count)
        for style, count in aggregate_styles.most_common()
        if style in catalogue_styles and catalogue_styles[style] <= 5
    ]

    style_rankings = []
    for style, count in aggregate_styles.most_common():
        cat_count = catalogue_styles.get(style, 0)
        style_rankings.append(
            {
                "style": style,
                "network_count": count,
                "catalogue_count": cat_count,
                "ratio": round(count / cat_count, 3) if cat_count > 0 else float("inf"),
                "absent": style not in catalogue_styles,
                "rare": cat_count > 0 and cat_count <= 5,
            }
        )

    return NetworkStyleAnalysis(
        seed_handle=network_profiles[0].handle if network_profiles else "",
        network_profiles=network_profiles,
        aggregate_styles=aggregate_styles,
        catalogue_styles=catalogue_styles,
        absent_styles=absent_styles,
        rare_styles=rare_styles,
        style_rankings=style_rankings,
    )


def dump_json(analysis: NetworkStyleAnalysis, output_path: Path) -> None:
    """Dump analysis as JSON."""
    data = {
        "seed_handle": analysis.seed_handle,
        "network_profiles": [
            {
                "handle": p.handle,
                "url": p.url,
                "track_count": len(p.tracks),
                "followers": p.followers,
                "following": p.following,
                "tracks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "styles": getattr(t, "style_tags", []),
                        "play_count": getattr(t, "play_count", None),
                    }
                    for t in p.tracks
                ],
            }
            for p in analysis.network_profiles
        ],
        "aggregate_styles": dict(analysis.aggregate_styles),
        "catalogue_styles": dict(analysis.catalogue_styles),
        "absent_styles": analysis.absent_styles,
        "rare_styles": [{"style": s, "count": c} for s, c in analysis.rare_styles],
        "style_rankings": analysis.style_rankings,
    }
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def dump_csv(analysis: NetworkStyleAnalysis, output_path: Path) -> None:
    """Dump style rankings as CSV."""
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["style", "network_count", "catalogue_count", "ratio", "absent", "rare"])
        for row in analysis.style_rankings:
            writer.writerow(
                [
                    row["style"],
                    row["network_count"],
                    row["catalogue_count"],
                    row["ratio"],
                    row["absent"],
                    row["rare"],
                ]
            )


def print_summary(analysis: NetworkStyleAnalysis) -> None:
    """Print a human-readable summary."""
    print(f"\nNetwork Style Analysis for @{analysis.seed_handle}")
    print(f"Profiles analyzed: {len(analysis.network_profiles)}")
    print(f"Total tracks: {sum(len(p.tracks) for p in analysis.network_profiles)}")
    print(f"Unique style tags in network: {len(analysis.aggregate_styles)}")
    print(f"Unique style tags in catalogue: {len(analysis.catalogue_styles)}")
    print(f"\nAbsent styles (in catalogue but not in network): {len(analysis.absent_styles)}")
    for style in analysis.absent_styles[:20]:
        print(f"  - {style}")
    if len(analysis.absent_styles) > 20:
        print(f"  ... and {len(analysis.absent_styles) - 20} more")

    print(f"\nRare styles (in network, <=5 in catalogue): {len(analysis.rare_styles)}")
    for style, count in analysis.rare_styles[:20]:
        cat_count = analysis.catalogue_styles.get(style, 0)
        print(f"  - {style} (network: {count}, catalogue: {cat_count})")

    print("\nTop styles by network frequency:")
    for i, row in enumerate(analysis.style_rankings[:15], 1):
        marker = " ⚠ ABSENT" if row["absent"] else (" ⚠ RARE" if row["rare"] else "")
        print(
            f"  {i:2d}. {row['style']}: network={row['network_count']}, "
            f"catalogue={row['catalogue_count']}, ratio={row['ratio']}{marker}"
        )
