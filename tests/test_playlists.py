"""Public playlist expansion and resumable checkpoints (#160).

Everything here is offline: the profile feed and the playlist pages are faked
at the transport boundary, so the tests describe the *contract* (what is
selected, what is unresolved, what a restart redoes) rather than any live page.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from suno_dl.playlists import (
    MAX_PLAYLIST_TRACKS,
    PlaylistCheckpoint,
    PlaylistRef,
    _stated_song_count,
    checkpoint_path,
    discover_profile_playlists,
    expand_public_playlists,
    fetch_playlist_membership,
)


def _uuid(n: int) -> str:
    return f"{n:08x}-1111-4111-8111-111111111111"


def _item(n: int, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": _uuid(n),
        "name": f"Playlist {n}",
        "num_total_clips": n,
    }
    item.update(extra)
    return item


def _html(*ids: str) -> str:
    # Valid fixture pages explicitly state their full unique membership size.
    anchors = "".join(f'<a href="/song/{clip}">Track {clip[:8]}</a>' for clip in ids)
    return f"<html><body>{anchors}<p>{len(set(ids))} songs</p></body></html>"


def _page_fetcher(pages: dict[int, dict[str, Any]], calls: list[int] | None = None):
    async def fetch(page: int) -> dict[str, Any]:
        if calls is not None:
            calls.append(page)
        return pages.get(page, {})

    return fetch


def _html_fetcher(html_by_id: dict[str, str], calls: list[str] | None = None):
    async def fetch(url: str) -> str:
        playlist_id = url.rsplit("/", 1)[-1]
        if calls is not None:
            calls.append(playlist_id)
        value = html_by_id.get(playlist_id)
        if isinstance(value, Exception):
            raise value
        return value if value is not None else "<html><body></body></html>"

    return fetch


# ───────────────────────────── discovery ─────────────────────────────


async def test_discovers_every_public_playlist_across_pages() -> None:
    """Five on page one, eleven on page two — the profile's full sixteen."""
    pages = {
        1: {"playlists": [_item(n) for n in range(1, 6)], "num_total_playlists": 16},
        2: {"playlists": [_item(n) for n in range(6, 17)]},
    }
    calls: list[int] = []
    discovery = await discover_profile_playlists(_page_fetcher(pages, calls))

    assert len(discovery.playlists) == 16
    assert discovery.pages == 2
    assert discovery.reported_total == 16
    # Reaching the reported total must stop paging: no third request.
    assert calls == [1, 2]


async def test_pagination_stops_on_an_empty_page() -> None:
    pages = {1: {"playlists": [_item(1)]}, 2: {"playlists": []}}
    calls: list[int] = []
    discovery = await discover_profile_playlists(_page_fetcher(pages, calls))

    assert [p.id for p in discovery.playlists] == [_uuid(1)]
    assert calls == [1, 2]


async def test_private_hidden_and_trashed_playlists_are_never_selected() -> None:
    pages = {
        1: {
            "playlists": [
                _item(1),
                _item(2, is_public=False),
                _item(3, visibility="Private"),
                _item(4, is_hidden=True),
                _item(5, trashed=True),
                _item(6, status="deleted"),
                _item(7, is_private=True),
            ]
        }
    }
    discovery = await discover_profile_playlists(_page_fetcher(pages))

    assert [p.id for p in discovery.playlists] == [_uuid(1)]
    assert discovery.skipped_not_public == 6


async def test_missing_visibility_markers_stay_public() -> None:
    """An entry from the anonymous feed without a marker is public, not hidden."""
    discovery = await discover_profile_playlists(
        _page_fetcher({1: {"playlists": [{"id": _uuid(1), "title": "Mix"}]}})
    )
    assert len(discovery.playlists) == 1
    assert discovery.playlists[0].name == "Mix"
    assert discovery.skipped_not_public == 0


async def test_unexpected_payload_shapes_are_tolerated() -> None:
    pages = {
        1: {"playlists": [{"id": "not-a-uuid"}, {"name": "no id"}, _item(2)]},
        2: {"data": {"playlists": [_item(3)]}},
        3: {"playlists": "not-a-list", "items": [None, "x", 3], "data": None},
    }
    discovery = await discover_profile_playlists(_page_fetcher(pages))

    assert [p.id for p in discovery.playlists] == [_uuid(2), _uuid(3)]
    assert discovery.skipped_invalid == 2


async def test_playlist_cap_marks_the_result_truncated() -> None:
    pages = {1: {"playlists": [_item(n) for n in range(1, 11)], "num_total_playlists": 10}}
    discovery = await discover_profile_playlists(_page_fetcher(pages), max_playlists=3)
    assert len(discovery.playlists) == 3
    assert discovery.truncated is True


async def test_page_budget_is_honoured() -> None:
    pages = {page: {"playlists": [_item(page)]} for page in range(1, 10)}
    calls: list[int] = []
    discovery = await discover_profile_playlists(_page_fetcher(pages, calls), max_pages=3)
    assert discovery.pages == 3
    assert calls == [1, 2, 3]


async def test_playlist_identity_is_parsed_or_dropped() -> None:
    discovery = await discover_profile_playlists(
        _page_fetcher(
            {
                1: {
                    "playlists": [
                        _item(1, user={"handle": "@drinko"}, num_total_clips=7),
                        _item(2, owner={"display_name": "Ricktron 5000"}),
                        _item(3, user={"handle": "evil/../x"}),
                    ]
                }
            }
        )
    )
    first, second, third = discovery.playlists
    assert (first.owner, first.track_count) == ("drinko", 7)
    assert second.owner == "Ricktron 5000"
    # A handle carrying a path separator is not a usable identity.
    assert third.owner is None
    assert first.url == f"https://suno.com/playlist/{_uuid(1)}"


# ───────────────────────────── membership ─────────────────────────────


async def test_membership_keeps_order_and_drops_duplicates() -> None:
    ref = PlaylistRef(id=_uuid(9), name="Mix", track_count=3)
    membership = await fetch_playlist_membership(
        _html_fetcher({_uuid(9): _html(_uuid(3), _uuid(1), _uuid(3), _uuid(2))}), ref
    )
    assert membership.resolved
    assert membership.ids == (_uuid(3), _uuid(1), _uuid(2))


async def test_empty_page_is_unresolved_not_zero() -> None:
    """#160: the older records' pages yielded nothing; that is a gap, not zero."""
    ref = PlaylistRef(id=_uuid(1), name="Old mix")
    membership = await fetch_playlist_membership(
        _html_fetcher({_uuid(1): "<html><body>Sign in to view</body></html>"}), ref
    )
    assert not membership.resolved
    assert membership.reason == "no public song ids in the page"
    assert membership.ids == ()


async def test_page_that_states_zero_songs_is_resolved_empty() -> None:
    ref = PlaylistRef(id=_uuid(2), name="Empty", track_count=0)
    membership = await fetch_playlist_membership(
        _html_fetcher({_uuid(2): "<html><body><p>0 songs</p></body></html>"}), ref
    )
    assert membership.resolved
    assert membership.ids == ()


async def test_page_stating_a_count_but_no_ids_names_the_count() -> None:
    ref = PlaylistRef(id=_uuid(3), name="Gated")
    membership = await fetch_playlist_membership(
        _html_fetcher({_uuid(3): "<html><body><p>112 songs</p></body></html>"}), ref
    )
    assert not membership.resolved
    assert "112 songs" in (membership.reason or "")


async def test_fetch_failure_is_unresolved() -> None:
    ref = PlaylistRef(id=_uuid(4), name="Broken")
    membership = await fetch_playlist_membership(
        _html_fetcher({_uuid(4): RuntimeError("boom")}), ref
    )
    assert not membership.resolved
    assert "RuntimeError" in (membership.reason or "")


def test_stated_song_count_handles_thousands_separator() -> None:
    assert _stated_song_count("<p>1,024 songs</p>") == 1024
    assert _stated_song_count("<p>no count here</p>") is None


# ───────────────────────────── expansion ─────────────────────────────


async def test_expansion_adds_only_unseen_tracks_and_records_membership(
    tmp_path: Path,
) -> None:
    pages = {1: {"playlists": [_item(1, num_total_clips=2), _item(2, num_total_clips=2)]}}
    html = {
        _uuid(1): _html(_uuid(10), _uuid(11)),
        _uuid(2): _html(_uuid(11), _uuid(12)),
    }
    recorded: list[tuple[str, tuple[str, ...]]] = []
    checkpoint = PlaylistCheckpoint(tmp_path)

    expansion = await expand_public_playlists(
        _page_fetcher(pages),
        _html_fetcher(html),
        checkpoint=checkpoint,
        known_ids={_uuid(10)},
        on_membership=lambda ref, ids: recorded.append((ref.id, tuple(ids))),
    )

    assert [t.id for t in expansion.tracks] == [_uuid(11), _uuid(12)]
    assert expansion.playlists_expanded == 2
    assert expansion.counts()["playlist_tracks"] == 2
    assert recorded == [(_uuid(1), (_uuid(10), _uuid(11))), (_uuid(2), (_uuid(11), _uuid(12)))]
    assert checkpoint_path(tmp_path).is_file()


async def test_one_unreadable_playlist_does_not_abort_the_rest(tmp_path: Path) -> None:
    pages = {1: {"playlists": [_item(1), _item(2, num_total_clips=1)]}}
    html = {_uuid(1): RuntimeError("blocked"), _uuid(2): _html(_uuid(20))}

    expansion = await expand_public_playlists(
        _page_fetcher(pages), _html_fetcher(html), checkpoint=PlaylistCheckpoint(tmp_path)
    )

    assert [t.id for t in expansion.tracks] == [_uuid(20)]
    assert (expansion.playlists_expanded, expansion.playlists_unresolved) == (1, 1)
    assert expansion.unresolved[0][1] == "fetch failed (RuntimeError)"


async def test_checkpoint_rechecks_membership_without_churning_checkpoint(tmp_path: Path) -> None:
    """A restart rechecks membership and avoids rewriting unchanged progress."""
    pages = {1: {"playlists": [_item(1), _item(2, num_total_clips=1)]}}
    html = {_uuid(1): _html(_uuid(1)), _uuid(2): _html(_uuid(2))}

    await expand_public_playlists(
        _page_fetcher(pages), _html_fetcher(html), checkpoint=PlaylistCheckpoint(tmp_path)
    )
    before = checkpoint_path(tmp_path).read_bytes()

    calls: list[str] = []
    expansion = await expand_public_playlists(
        _page_fetcher(pages),
        _html_fetcher(html, calls),
        checkpoint=PlaylistCheckpoint(tmp_path),
        known_ids=[_uuid(1), _uuid(2)],
    )

    assert calls == [_uuid(1), _uuid(2)]
    assert expansion.playlists_skipped == 0
    assert expansion.playlists_expanded == 2
    assert expansion.tracks == []
    # Nothing changed, so the file is neither rewritten nor reordered.
    assert checkpoint_path(tmp_path).read_bytes() == before


async def test_changed_track_count_reexpands_that_playlist(tmp_path: Path) -> None:
    first = {1: {"playlists": [_item(1)]}}
    await expand_public_playlists(
        _page_fetcher(first),
        _html_fetcher({_uuid(1): _html(_uuid(1))}),
        checkpoint=PlaylistCheckpoint(tmp_path),
    )

    calls: list[str] = []
    changed = {1: {"playlists": [_item(1, num_total_clips=2)]}}
    expansion = await expand_public_playlists(
        _page_fetcher(changed),
        _html_fetcher({_uuid(1): _html(_uuid(2), _uuid(3))}, calls),
        checkpoint=PlaylistCheckpoint(tmp_path),
    )

    assert calls == [_uuid(1)]
    assert [t.id for t in expansion.tracks] == [_uuid(2), _uuid(3)]


async def test_dry_run_persists_nothing(tmp_path: Path) -> None:
    pages = {1: {"playlists": [_item(1)]}}
    expansion = await expand_public_playlists(
        _page_fetcher(pages),
        _html_fetcher({_uuid(1): _html(_uuid(1))}),
        checkpoint=PlaylistCheckpoint(tmp_path),
        persist=False,
    )

    assert expansion.playlists_expanded == 1
    assert not checkpoint_path(tmp_path).exists()
    assert not (tmp_path / ".library").exists()


async def test_unresolved_playlists_are_remembered_as_unresolved(tmp_path: Path) -> None:
    """A retry still tries an unresolved playlist; it is not 'complete'."""
    pages = {1: {"playlists": [_item(1)]}}
    await expand_public_playlists(
        _page_fetcher(pages),
        _html_fetcher({_uuid(1): "<html></html>"}),
        checkpoint=PlaylistCheckpoint(tmp_path),
    )
    data = json.loads(checkpoint_path(tmp_path).read_text())
    assert data["playlists"][_uuid(1)]["status"] == "unresolved"

    calls: list[str] = []
    await expand_public_playlists(
        _page_fetcher(pages),
        _html_fetcher({_uuid(1): _html(_uuid(1))}, calls),
        checkpoint=PlaylistCheckpoint(tmp_path),
    )
    assert calls == [_uuid(1)]


async def test_track_cap_stops_expansion(tmp_path: Path) -> None:
    pages = {1: {"playlists": [_item(1), _item(2)]}}
    html = {_uuid(1): _html(_uuid(1)), _uuid(2): _html(_uuid(2))}
    expansion = await expand_public_playlists(
        _page_fetcher(pages),
        _html_fetcher(html),
        checkpoint=PlaylistCheckpoint(tmp_path),
        max_tracks=1,
    )
    assert [t.id for t in expansion.tracks] == [_uuid(1)]
    assert expansion.playlists_unresolved == 1
    assert expansion.unresolved[0][1] == "track cap reached"


async def test_membership_cap_is_documented() -> None:
    """The per-playlist cap exists; a page beyond it is truncated, not crashed."""
    ids = [_uuid(n) for n in range(1, 6)]
    ref = PlaylistRef(id=_uuid(99), name="Huge", track_count=len(ids))
    membership = await fetch_playlist_membership(_html_fetcher({_uuid(99): _html(*ids)}), ref)
    assert membership.ids == tuple(ids)
    assert len(ids) < MAX_PLAYLIST_TRACKS


def test_checkpoint_file_sits_beside_the_membership_index(tmp_path: Path) -> None:
    assert checkpoint_path(tmp_path) == tmp_path / ".library" / "playlist-checkpoint.json"


# ───────────────────────────── CLI wiring ─────────────────────────────


def _fake_fetcher(page_loader, html_by_id):
    """A stand-in for ``Fetcher`` used by the CLI's playlist helper."""

    class _Fake:
        client = object()
        rate_limiter = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def fetch_html(self, url: str) -> str:
            value = html_by_id.get(url.rsplit("/", 1)[-1])
            if isinstance(value, Exception):
                raise value
            return value or "<html></html>"

    return _Fake()


async def test_cli_helper_records_membership_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl.cli import _expand_profile_playlists
    from suno_dl.config import load_config
    from suno_dl.library import load_membership

    pages = {1: {"playlists": [_item(1)]}}
    monkeypatch.setattr(
        "suno_dl.cli._make_fetcher",
        lambda cfg, rate_limit=None: _fake_fetcher(pages, {_uuid(1): _html(_uuid(7))}),
    )

    async def fake_page(client, handle, page, timeout=30.0, rate_limiter=None):
        return pages.get(page, {})

    monkeypatch.setattr("suno_dl.cli.fetch_profile_feed_page", fake_page)
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    cfg = load_config(output_dir=tmp_path)

    tracks, counts = await _expand_profile_playlists(cfg, "drinko", [], tmp_path, False, None)

    assert [t.id for t in tracks] == [_uuid(7)]
    assert counts["playlists_expanded"] == 1
    assert load_membership(tmp_path)[f"playlist:{_uuid(1)}"] == [_uuid(7)]
    assert checkpoint_path(tmp_path).is_file()


async def test_cli_helper_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suno_dl.cli import _expand_profile_playlists
    from suno_dl.config import load_config
    from suno_dl.library import load_membership

    pages = {1: {"playlists": [_item(1)]}}
    monkeypatch.setattr(
        "suno_dl.cli._make_fetcher",
        lambda cfg, rate_limit=None: _fake_fetcher(pages, {_uuid(1): _html(_uuid(7))}),
    )

    async def fake_page(client, handle, page, timeout=30.0, rate_limiter=None):
        return pages.get(page, {})

    monkeypatch.setattr("suno_dl.cli.fetch_profile_feed_page", fake_page)
    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    cfg = load_config(output_dir=tmp_path)

    tracks, counts = await _expand_profile_playlists(cfg, "drinko", [], tmp_path, True, None)

    assert [t.id for t in tracks] == [_uuid(7)]
    assert counts["playlists_expanded"] == 1
    assert load_membership(tmp_path) == {}
    assert not checkpoint_path(tmp_path).exists()


def test_playlists_flag_is_off_by_default_and_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from suno_dl.cli import main

    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUNO_PLAYLISTS", raising=False)
    seen: list[object] = []

    async def fake_run(*a, **kw):
        seen.append(kw.get("playlists"))
        return {"total": 1, "downloaded": 1, "skipped_exists": 0, "failed": 0}

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    base = ["--url", "https://suno.com/@drinko", "--output-dir", str(tmp_path)]

    assert CliRunner().invoke(main, base).exit_code == 0
    assert CliRunner().invoke(main, [*base, "--playlists"]).exit_code == 0
    assert CliRunner().invoke(main, [*base, "--no-playlists"]).exit_code == 0
    monkeypatch.setenv("SUNO_PLAYLISTS", "1")
    assert CliRunner().invoke(main, base).exit_code == 0

    assert seen == [False, True, False, True]


def test_playlist_summary_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from suno_dl.cli import main

    monkeypatch.setenv("SUNO_DEFAULT_HANDLE", "drinko")
    monkeypatch.setenv("SUNO_OUTPUT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    async def fake_run(*a, **kw):
        return {
            "total": 2,
            "downloaded": 1,
            "skipped_exists": 1,
            "failed": 0,
            "playlists_found": 16,
            "playlists_expanded": 9,
            "playlists_unresolved": 7,
            "playlist_tracks": 5,
        }

    monkeypatch.setattr("suno_dl.cli._run", fake_run)
    result = CliRunner().invoke(main, ["--url", "https://suno.com/@drinko", "--playlists"])

    assert result.exit_code == 0, result.output
    assert "playlists=9/16" in result.output
    assert "playlist_unresolved=7" in result.output


async def test_expansion_fetches_one_playlist_at_a_time(tmp_path: Path) -> None:
    """Politeness: playlists are read strictly one after another, never fanned out."""
    pages = {1: {"playlists": [_item(1), _item(2), _item(3)]}}
    in_flight = 0
    peak = 0

    async def fetch(url: str) -> str:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return _html(_uuid(1))

    await expand_public_playlists(
        _page_fetcher(pages), fetch, checkpoint=PlaylistCheckpoint(tmp_path)
    )

    assert peak == 1


async def test_partial_html_cannot_persist_complete_checkpoint(tmp_path: Path) -> None:
    """A one-song shell for a two-song feed remains retryable, never complete."""
    playlist_id = _uuid(1)
    calls: list[tuple[str, tuple[str, ...]]] = []
    page = {1: {"playlists": [_item(1, num_total_clips=2)]}}
    # A link with no trustworthy count used to be accepted as complete.
    partial_html = {_uuid(1): f'<a href="/song/{_uuid(2)}">one</a>'}
    checkpoint = PlaylistCheckpoint(tmp_path)
    result = await expand_public_playlists(
        _page_fetcher(page),
        _html_fetcher(partial_html),
        checkpoint=checkpoint,
        on_membership=lambda ref, ids: calls.append((ref.id, tuple(ids))),
    )

    assert result.playlists_expanded == 0
    assert result.playlists_unresolved == 1
    assert result.tracks == []
    assert calls == []
    state = json.loads(checkpoint_path(tmp_path).read_text(encoding="utf-8"))
    assert state["playlists"][playlist_id]["status"] == "unresolved"


@pytest.mark.parametrize("field", ["song_count", "num_total_results"])
async def test_public_feed_counts_alternate_fields_preserve_membership_total(
    field: str,
) -> None:
    """Public feed song totals must not vanish during playlist discovery."""
    ref_id = _uuid(1)
    pages = {
        1: {
            "playlists": [
                {
                    "id": ref_id,
                    "name": "Public",
                    "is_public": True,
                    "is_hidden": False,
                    "is_trashed": False,
                    field: 17,
                }
            ]
        }
    }
    discovery = await discover_profile_playlists(_page_fetcher(pages), max_pages=1)
    assert len(discovery.playlists) == 1
    assert discovery.playlists[0].track_count == 17


async def test_missing_profile_count_never_accepts_unrelated_html_total(tmp_path: Path) -> None:
    """End-to-end feed, parse, and checkpoint guard with unknown count."""
    ref_id = _uuid(1)
    feed = {
        1: {
            "playlists": [
                {
                    "id": ref_id,
                    "name": "Public",
                    "is_public": True,
                    "is_hidden": False,
                    "is_trashed": False,
                }
            ]
        }
    }
    html = {ref_id: f'<div>1 songs</div><a href="/song/{_uuid(2)}">One</a>'}
    expansion = await expand_public_playlists(
        _page_fetcher(feed), _html_fetcher(html), checkpoint=PlaylistCheckpoint(tmp_path)
    )
    assert expansion.playlists_unresolved == 1
    assert expansion.playlists_expanded == 0
    assert (
        json.loads(checkpoint_path(tmp_path).read_text())["playlists"][ref_id]["status"]
        == "unresolved"
    )


async def test_conflicting_public_feed_counts_skip_invalid_playlist() -> None:
    """Disagreeing advertised totals cannot become a complete checkpoint."""
    entry = {
        "id": _uuid(1),
        "name": "Contradictory",
        "num_total_clips": 1,
        "song_count": 17,
        "is_public": True,
    }
    result = await discover_profile_playlists(_page_fetcher({1: {"playlists": [entry]}}))
    assert result.playlists == ()
    assert result.skipped_invalid == 1


async def test_actual_over_cap_html_never_marks_checkpoint_complete(tmp_path: Path) -> None:
    """The real 5,000-track boundary must fail closed end-to-end."""
    total = MAX_PLAYLIST_TRACKS + 1
    playlist_id = _uuid(1)
    html = "".join(f'<a href="/song/{_uuid(i)}">Track</a>' for i in range(1, total + 1))
    html += f"<p>{total} songs</p>"
    calls: list[tuple[str, ...]] = []
    result = await expand_public_playlists(
        _page_fetcher(
            {
                1: {
                    "playlists": [
                        {
                            "id": playlist_id,
                            "name": "Over cap",
                            "num_total_clips": total,
                            "is_public": True,
                        }
                    ]
                }
            }
        ),
        _html_fetcher({playlist_id: html}),
        checkpoint=PlaylistCheckpoint(tmp_path),
        on_membership=lambda ref, ids: calls.append(tuple(ids)),
        max_tracks=total + 1,
    )
    assert result.playlists_expanded == 0
    assert result.playlists_unresolved == 1
    assert result.tracks == []
    assert calls == []
    data = json.loads(checkpoint_path(tmp_path).read_text())
    assert data["playlists"][playlist_id]["status"] == "unresolved"


async def test_nested_hidden_and_private_playlist_markers_are_excluded() -> None:
    data = {
        1: {
            "playlists": [
                _item(1, visibility={"state": "hidden"}),
                _item(2, privacy={"value": "private"}),
                _item(3, status={"visibility": "unlisted"}),
                _item(4),
            ]
        }
    }
    result = await discover_profile_playlists(_page_fetcher(data))
    assert [ref.id for ref in result.playlists] == [_uuid(4)]
    assert result.skipped_not_public == 3


async def test_conflicting_profile_page_totals_do_not_terminate_discovery() -> None:
    calls: list[int] = []
    pages = {
        1: {"playlists": [_item(1)], "num_total_playlists": 5},
        2: {"playlists": [_item(2)], "num_total_playlists": 2},
        3: {"playlists": [_item(3)]},
        4: {"playlists": []},
    }
    result = await discover_profile_playlists(_page_fetcher(pages, calls))
    assert [ref.id for ref in result.playlists] == [_uuid(1), _uuid(2), _uuid(3)]
    assert calls == [1, 2, 3, 4]
    assert result.truncated is True
    assert result.reported_total is None


async def test_global_track_cap_does_not_checkpoint_partial_playlist(tmp_path: Path) -> None:
    playlist_id = _uuid(1)
    data = {1: {"playlists": [_item(1, num_total_clips=3)]}}
    html = {playlist_id: _html(_uuid(2), _uuid(3), _uuid(4))}
    recorded: list[tuple[str, ...]] = []
    result = await expand_public_playlists(
        _page_fetcher(data),
        _html_fetcher(html),
        checkpoint=PlaylistCheckpoint(tmp_path),
        max_tracks=2,
        on_membership=lambda ref, ids: recorded.append(tuple(ids)),
    )
    assert result.playlists_expanded == 0
    assert result.playlists_unresolved == 1
    assert result.tracks == []
    assert recorded == []
    state = json.loads(checkpoint_path(tmp_path).read_text())
    assert state["playlists"][playlist_id]["status"] == "unresolved"


def test_checkpoint_identical_updates_do_not_refresh_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    import suno_dl.playlists as playlists_module

    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr(playlists_module, "datetime", SimpleNamespace(now=lambda tz: clock[0]))
    ref = PlaylistRef(id=_uuid(1), name="Playlist", track_count=1)
    checkpoint = PlaylistCheckpoint(tmp_path)
    checkpoint.mark_unresolved(ref, "incomplete")
    assert checkpoint.save()
    before = checkpoint.path.read_bytes()
    clock[0] = datetime(2026, 1, 2, tzinfo=UTC)
    checkpoint = PlaylistCheckpoint(tmp_path)
    assert not checkpoint.is_complete(ref)
    checkpoint.mark_unresolved(ref, "incomplete")
    assert not checkpoint.save()
    assert checkpoint.path.read_bytes() == before
    checkpoint.mark_complete(ref, 1)
    assert checkpoint.save()
    entry = PlaylistCheckpoint(tmp_path).entry(ref)
    assert entry is not None
    assert entry["updated_at"] == "2026-01-02T00:00:00Z"
    assert PlaylistCheckpoint(tmp_path).is_complete(ref)
    checkpoint = PlaylistCheckpoint(tmp_path)
    clock[0] = datetime(2026, 1, 3, tzinfo=UTC)
    checkpoint.mark_complete(ref, 1)
    assert not checkpoint.save()


def test_playlist_tests_do_not_pin_temporary_directory_to_linux() -> None:
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "TemporaryDirectory"
        ):
            for keyword in node.keywords:
                if keyword.arg == "dir" and isinstance(keyword.value, ast.Constant):
                    assert not str(keyword.value.value).startswith(("/dev/", "/" + "tmp/"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("is_public", "false"),
        ("is_hidden", " TRUE "),
        ("is_private", "true"),
        ("is_trashed", "true"),
        ("privacy", {"is_public": False}),
        ("visibility", {"is_hidden": True}),
        ("status", {"is_private": "true"}),
    ],
)
async def test_feed_rejects_nonpublic_boolean_markers(field: str, value: object) -> None:
    discovery = await discover_profile_playlists(
        _page_fetcher({1: {"playlists": [_item(1, **{field: value})]}})
    )
    assert discovery.playlists == ()
    assert discovery.skipped_not_public == 1


async def test_restart_after_discovery_before_download_reoffers_missing_tracks(
    tmp_path: Path,
) -> None:
    pages = {1: {"playlists": [_item(1, num_total_clips=2)]}}
    html = {_uuid(1): _html(_uuid(10), _uuid(11))}
    first = await expand_public_playlists(
        _page_fetcher(pages), _html_fetcher(html), checkpoint=PlaylistCheckpoint(tmp_path)
    )
    assert [t.id for t in first.tracks] == [_uuid(10), _uuid(11)]
    saved = json.loads(checkpoint_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["playlists"][_uuid(1)]["status"] == "membership_verified"
    # Simulate a crash after the checkpoint was saved, before downloading.
    resumed = await expand_public_playlists(
        _page_fetcher(pages), _html_fetcher(html), checkpoint=PlaylistCheckpoint(tmp_path)
    )
    assert [t.id for t in resumed.tracks] == [_uuid(10), _uuid(11)]


async def test_same_count_changed_playlist_is_rechecked(tmp_path: Path) -> None:
    pages = {1: {"playlists": [_item(1, num_total_clips=1)]}}
    await expand_public_playlists(
        _page_fetcher(pages),
        _html_fetcher({_uuid(1): _html(_uuid(10))}),
        checkpoint=PlaylistCheckpoint(tmp_path),
    )
    resumed = await expand_public_playlists(
        _page_fetcher(pages),
        _html_fetcher({_uuid(1): _html(_uuid(11))}),
        checkpoint=PlaylistCheckpoint(tmp_path),
    )
    assert [t.id for t in resumed.tracks] == [_uuid(11)]


@pytest.mark.usefixtures("disable_producer_and_tagging_guards_for_legacy_transfer_test")
async def test_interrupted_download_retries_only_missing_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import httpx

    import suno_dl.downloader as downloader
    from suno_dl.fetcher import Fetcher

    valid_media = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 2048
    pages = {1: {"playlists": [_item(1, num_total_clips=2)]}}
    html = {_uuid(1): _html(_uuid(10), _uuid(11))}
    attempts: list[str] = []
    interrupted = True

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield valid_media * 40
            raise httpx.ReadError("synthetic interrupted transfer")

        async def aclose(self) -> None:
            pass

    def handler(req: httpx.Request) -> httpx.Response:
        attempts.append(str(req.url))
        if _uuid(11) in str(req.url) and interrupted:
            return httpx.Response(200, stream=InterruptedStream())
        return httpx.Response(200, content=valid_media)

    monkeypatch.setattr(
        downloader,
        "tag_file",
        lambda *args, **kwargs: SimpleNamespace(tagged=True, backend="fake"),
    )

    async def run_download(tracks):
        for track in tracks:
            track.artist = "Verified creator"
            track.audio_url = f"https://cdn1.suno.ai/{track.id}.m4a"
            track.media_urls = [track.audio_url]
        fetcher = Fetcher(timeout_sec=1, max_retries=1, rate_limit_sec=0)
        fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await downloader.download_tracks(
                tracks, fetcher, 1, tmp_path, "{id}", "playlist_owner", covers=False
            )
        finally:
            await fetcher.__aexit__()

    first = await expand_public_playlists(
        _page_fetcher(pages), _html_fetcher(html), checkpoint=PlaylistCheckpoint(tmp_path)
    )
    initial = await run_download(first.tracks)
    assert initial["downloaded"] == 1 and initial["failed"] == 1
    assert (tmp_path / f"{_uuid(10)}.m4a").read_bytes() == valid_media
    assert not (tmp_path / f"{_uuid(11)}.m4a").exists()
    assert not list(tmp_path.glob("*.part"))
    interrupted = False
    attempts.clear()
    resumed = await expand_public_playlists(
        _page_fetcher(pages), _html_fetcher(html), checkpoint=PlaylistCheckpoint(tmp_path)
    )
    retry = await run_download(resumed.tracks)
    assert retry["downloaded"] == 1 and retry["skipped_exists"] == 1
    assert len(attempts) == 1 and _uuid(11) in attempts[0]
    assert (tmp_path / f"{_uuid(10)}.m4a").read_bytes() == valid_media
    assert (tmp_path / f"{_uuid(11)}.m4a").read_bytes() == valid_media


async def test_repeated_profile_playlist_window_is_not_reported_complete():
    rows = [_item(1), _item(2)]
    calls = []
    discovery = await discover_profile_playlists(
        _page_fetcher({1: {"playlists": rows}, 2: {"playlists": rows}}, calls)
    )
    assert calls == [1, 2]
    assert discovery.truncated
    assert "repeats" in discovery.reason
    assert len(discovery.playlists) == 2
