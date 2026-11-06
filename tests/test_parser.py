"""Parser tests using static HTML fixtures (no network)."""

from __future__ import annotations

import json

import pytest

from suno_dl.parser import parse_playlist, parse_profile, parse_song

CLIP_A = "a1b2c3d4-1111-4111-8111-111111111111"
CLIP_B = "b2c3d4e5-2222-4222-8222-222222222222"


def _rsc_html(clips: list[dict]) -> str:
    """Build a page with embedded React Flight chunk like a real Suno page."""
    items = [
        {
            "content_type": "clip",
            "content_item": {
                "id": c["id"],
                "title": c["title"],
                "duration": c.get("duration", 100.0),
                # A nested producer object replaces the flattened fields, the
                # way a mixed-creator playlist row does.
                **({"display_name": c.get("artist", "drinko")} if not c.get("user") else {}),
                "audio_url": c.get("audio_url", "https://studio-api.prod.suno.com/api/forbidden"),
                **({"created_at": c["created_at"]} if c.get("created_at") else {}),
                **({"user": c["user"]} if c.get("user") else {}),
                "major_model_version": c.get("model_version"),
                "model_name": c.get("model_name"),
            },
        }
        for c in clips
    ]
    payload = json.dumps({"items": items})
    escaped = payload.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    html = (
        '<html><head><script src="/_next/x.js"></script></head>'
        '<body><div id="__next"></div>'
        f'<script>self.__next_f=self.__next_f||[];self.__next_f.push([1,"{escaped}"])</script>'
        "</body></html>"
    )
    return html


def test_parse_profile_rsc_clips() -> None:
    html = _rsc_html(
        [
            {"id": CLIP_A, "title": "Alpha Song", "duration": 120.5},
            {"id": CLIP_B, "title": "Beta Song", "duration": 60},
        ]
    )
    tracks = parse_profile(html, handle="drinko")
    assert [t.id for t in tracks] == [CLIP_A, CLIP_B]
    assert tracks[0].title == "Alpha Song"
    assert tracks[0].audio_url == f"https://cdn1.suno.ai/{CLIP_A}.mp4"
    assert tracks[0].artist == "drinko"


def test_mixed_playlist_credits_each_songs_nested_creator() -> None:
    """A playlist owned by one profile must not relabel other producers."""
    html = _rsc_html(
        [
            {
                "id": CLIP_A,
                "title": "One",
                "user": {"display_name": "SmileDJ", "handle": "smiledj"},
            },
            {
                "id": CLIP_B,
                "title": "Two",
                "user": {"display_name": "Ricktron 5000", "handle": "ricktron5000"},
            },
        ]
    )
    tracks = parse_playlist(html)
    assert [t.artist for t in tracks] == ["SmileDJ", "Ricktron 5000"]
    assert [t.creator_handle for t in tracks] == ["smiledj", "ricktron5000"]

    # The same rows on a profile page still use the clip's own producer, not
    # the profile being browsed.
    profile = parse_profile(html, handle="drinko")
    assert [t.artist for t in profile] == ["SmileDJ", "Ricktron 5000"]


def test_hostile_nested_creator_is_unresolved_not_the_playlist_owner() -> None:
    html = _rsc_html(
        [
            {
                "id": CLIP_A,
                "title": "One",
                "user": {"display_name": "Bad\u202eName", "handle": "evil/../x"},
            }
        ]
    )
    track = parse_profile(html, handle="drinko")[0]
    assert track.artist is None
    assert track.creator_handle is None


def test_nested_handle_is_used_when_display_name_is_absent() -> None:
    html = _rsc_html([{"id": CLIP_A, "title": "One", "user": {"handle": "@AESTHAETH"}}])
    track = parse_playlist(html)[0]
    assert track.artist == "AESTHAETH"
    assert track.creator_handle == "AESTHAETH"


def test_unicode_display_name_is_preserved_verbatim() -> None:
    html = _rsc_html(
        [
            {
                "id": CLIP_A,
                "title": "One",
                "user": {"display_name": "Zo\u00eb \U0001f3b5", "handle": "zoe"},
            }
        ]
    )
    assert parse_playlist(html)[0].artist == "Zo\u00eb \U0001f3b5"


def test_parse_profile_reads_created_at() -> None:
    """Rotation orders by Suno's clock, so the payload timestamp must parse."""
    html = _rsc_html(
        [
            {
                "id": CLIP_A,
                "title": "Alpha Song",
                "duration": 120.5,
                "artist": "drinko",
                "created_at": "2025-04-01T12:00:00.000Z",
            },
            {"id": CLIP_B, "title": "Beta Song"},
        ]
    )
    tracks = parse_profile(html, handle="drinko")
    assert tracks[0].created_at == "2025-04-01T12:00:00.000Z"
    assert tracks[1].created_at is None


def test_parse_profile_dedup_and_dom_fallback() -> None:
    html = _rsc_html([{"id": CLIP_A, "title": "Alpha Song"}]) + (
        f'<a href="/song/{CLIP_A}">Alpha Song</a>'
        f'<a href="/song/{CLIP_B}">Beta &lt;Beta&gt; Song</a>'
    )
    tracks = parse_profile(html, handle="drinko")
    ids = [t.id for t in tracks]
    assert ids == [CLIP_A, CLIP_B]
    assert tracks[1].title == "Beta <Beta> Song"
    assert tracks[1].audio_url == f"https://cdn1.suno.ai/{CLIP_B}.mp4"


def test_parse_song_resolves_one_track() -> None:
    html = _rsc_html([{"id": CLIP_A, "title": "Solo Track", "duration": 90}])
    tracks = parse_song(html)
    assert len(tracks) == 1
    assert tracks[0].id == CLIP_A
    assert tracks[0].title == "Solo Track"


def test_parse_song_ignores_forbidden_audio() -> None:
    """audio_url may literally be the sentinel 'forbidden'; fall back to CDN."""
    html = _rsc_html(
        [
            {
                "id": CLIP_A,
                "title": "X",
                "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
            }
        ]
    )
    tracks = parse_song(html)
    assert tracks[0].audio_url == f"https://cdn1.suno.ai/{CLIP_A}.mp4"


def test_parse_playlist_uses_dom_anchors() -> None:
    html = f'<a href="/song/{CLIP_A}">First</a><a href="/song/{CLIP_B}">Second</a>'
    tracks = parse_playlist(html)
    assert {t.id for t in tracks} == {CLIP_A, CLIP_B}


def test_parse_profile_extracts_model_version() -> None:
    """RSC payload includes major_model_version and model_name."""
    html = _rsc_html(
        [
            {"id": CLIP_A, "title": "V6 Song", "model_version": "v6", "model_name": "chirp-hawk"},
            {"id": CLIP_B, "title": "V4.5 Song", "model_version": "v4.5", "model_name": "chirp-v4"},
        ]
    )
    tracks = parse_profile(html, handle="drinko")
    assert tracks[0].model_version == "v6"
    assert tracks[1].model_version == "v4.5"


def test_parse_profile_model_version_fallback_to_model_name() -> None:
    """If major_model_version is missing, model_name is used."""
    html = _rsc_html(
        [
            {"id": CLIP_A, "title": "Song", "model_name": "chirp-hawk"},
        ]
    )
    tracks = parse_profile(html, handle="drinko")
    assert tracks[0].model_version == "chirp-hawk"


def test_parse_profile_no_model_version_when_missing() -> None:
    """When neither field is present, model_version is None."""
    html = _rsc_html(
        [
            {"id": CLIP_A, "title": "Song"},
        ]
    )
    tracks = parse_profile(html, handle="drinko")
    assert tracks[0].model_version is None


def test_parse_song_extracts_model_version() -> None:
    """Single song page also extracts model_version."""
    html = _rsc_html([{"id": CLIP_A, "title": "Solo", "model_version": "v6"}])
    tracks = parse_song(html)
    assert len(tracks) == 1
    assert tracks[0].model_version == "v6"


def test_parse_playlist_dom_fallback_has_no_model_version() -> None:
    """DOM fallback (no RSC) yields tracks with model_version=None."""
    html = f'<a href="/song/{CLIP_A}">First</a>'
    tracks = parse_playlist(html)
    assert len(tracks) == 1
    assert tracks[0].model_version is None


ASSET_ID = "d3e4f5a6-3333-4333-8333-333333333333"


def _react_query_song_html(clip_id: str, title: str, *, with_state: bool) -> str:
    """Song page where the clip lives in react-query ``state.data`` (no ``"clip"`` key)."""
    data = {
        "status": "complete",
        "title": title,
        "id": clip_id,
        "entity_type": "song_schema",
        "video_url": f"https://cdn1.suno.ai/{clip_id}.mp4",
        "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
        "image_url": f"https://cdn2.suno.ai/{ASSET_ID}.jpeg",
    }
    payload = json.dumps({"queries": [{"state": {"data": data}}]}) if with_state else "{}"
    escaped = payload.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "<html><head>"
        f'<meta property="og:title" content="{title}"/>'
        f'<meta property="og:image" content="https://cdn2.suno.ai/{ASSET_ID}.jpeg"/>'
        f'<link rel="canonical" href="https://suno.com/song/{clip_id}"/>'
        f'</head><body><script>self.__next_f=self.__next_f||[];self.__next_f.push([1,"{escaped}"])</script>'
        "</body></html>"
    )


def test_parse_song_reads_react_query_state_clip() -> None:
    html = _react_query_song_html(CLIP_A, "Fresh Track (feat. X)", with_state=True)
    (track,) = parse_song(html)
    assert track.id == CLIP_A
    assert track.media_urls or track.audio_url


def test_parse_song_fallback_uses_canonical_id_not_og_image_asset() -> None:
    html = _react_query_song_html(CLIP_A, "Fresh Track", with_state=False)
    (track,) = parse_song(html)
    assert track.id == CLIP_A  # not ASSET_ID (the og:image asset appears first in the page)


def test_clip_to_track_keeps_public_metadata_for_the_sidecar() -> None:
    from suno_dl.parser import _clip_to_track

    clip = {
        "id": "a1b2c3d4-1111-4111-8111-111111111111",
        "title": "Song: Part 1/2",
        "created_at": "2026-10-09T01:02:03.000Z",
        "model_name": "chirp-fenix",
        "major_model_version": "v5",
        "tags": "chipfunk",
        "is_public": True,
        "duration": 213.4,
    }
    track = _clip_to_track(clip, default_handle="drinko")
    assert track is not None
    assert (track.title, track.created_at, track.model_name) == (
        "Song: Part 1/2",
        "2026-10-09T01:02:03.000Z",
        "chirp-fenix",
    )
    assert (track.model_version, track.tags, track.is_public) == ("v5", "chipfunk", True)
    assert track.duration_sec == 213
    bare = _clip_to_track({"id": clip["id"]}, default_handle=None)
    assert bare is not None and bare.created_at is None and bare.is_public is None


def test_rsc_clip_preserves_explicit_creator_and_canonical_handle() -> None:
    """#157: playlist/song clip identity is separate from the source owner."""
    from suno_dl.parser import _clip_to_track
    from suno_dl.sidecar import build_sidecar

    name = "Æsthaeth 🎵 e\u0301"
    item = {
        "id": CLIP_A,
        "title": "Mixed creator / Playlist?",
        "display_name": name,
        "handle": "@aesthaeth",
    }
    track = _clip_to_track(item, default_handle="drinko")
    assert track is not None
    assert track.artist == name
    assert track.creator_handle == "aesthaeth"
    assert track.title == item["title"]
    sidecar = build_sidecar(
        track, handle="drinko", saved_files=["existing.m4a"], downloaded_at="2026-01-01T00:00:00Z"
    )
    assert sidecar["handle"] == "drinko"
    assert sidecar["creator_handle"] == "aesthaeth"


def test_rsc_clip_artist_without_display_name_uses_canonical_handle() -> None:
    from suno_dl.parser import _clip_to_track

    track = _clip_to_track(
        {"id": CLIP_A, "title": "Shared track", "handle": "@other_creator"},
        default_handle="drinko",
    )
    assert track is not None
    assert track.artist == "other_creator"
    assert track.creator_handle == "other_creator"


@pytest.mark.parametrize(
    "malformed",
    [
        "Spoof\u202eexe.m4a",
        "Spoof\u2066Alice",
        "<img src=x onerror=alert(1)>",
        "Zero\x00Ctrl",
        "Bad\ud800Surrogate",
        "A" * 700,
        "🎵" * 150,
    ],
)
def test_rsc_explicit_malformed_creator_is_unknown_not_source_owner(
    malformed: str,
) -> None:
    from suno_dl.parser import _clip_to_track

    item = {"id": CLIP_A, "title": "Shared song", "display_name": malformed}
    track = _clip_to_track(item, default_handle="drinko")
    assert track is not None
    assert track.artist is None
    assert track.creator_handle is None


@pytest.mark.parametrize("extract", [parse_profile, parse_playlist, parse_song])
def test_rsc_page_rejects_hostile_clip_creator_in_any_entrypoint(extract) -> None:
    html = _rsc_html([{"id": CLIP_A, "title": "Shared", "artist": "Bogus\u202e.exe"}])
    tracks = extract(html, "drinko") if extract is parse_profile else extract(html)
    assert len(tracks) == 1
    assert tracks[0].artist is None


@pytest.mark.parametrize(
    "bad_handle",
    ["evil/../", "evil%2fother", "@@@", "trick\u202eAlice", "a\\b"],
)
def test_rsc_invalid_canonical_handle_never_falls_back_to_profile(
    bad_handle: str,
) -> None:
    """Malformed creator handles remain unresolved, rather than credited to the source."""
    from suno_dl.parser import _clip_to_track

    track = _clip_to_track(
        {"id": CLIP_A, "title": "Shared", "handle": bad_handle},
        default_handle="drinko",
    )
    assert track is not None
    assert track.artist is None
    assert track.creator_handle is None


def test_rsc_legacy_missing_clip_identity_keeps_source_fallback() -> None:
    from suno_dl.parser import _clip_to_track

    track = _clip_to_track({"id": CLIP_A, "title": "Legacy"}, default_handle="drinko")
    assert track is not None
    assert track.artist == "drinko"
    assert track.creator_handle is None


@pytest.mark.parametrize(
    "untrusted",
    [
        f"https://attacker.invalid/song/{CLIP_A}",
        f"https://suno.com.attacker.invalid/song/{CLIP_A}",
        f"//attacker.invalid/song/{CLIP_A}",
        f"javascript:alert(1)//song/{CLIP_A}",
        f"/redirect?next=/song/{CLIP_A}",
        f"/song/%2e%2e/{CLIP_A}",
        f"/song/{CLIP_A}/other",
        f"https://account@suno.com/song/{CLIP_A}",
        f"/song/{CLIP_A}%2fother",
    ],
)
@pytest.mark.parametrize("parser", [parse_profile, parse_playlist])
def test_dom_fallback_rejects_forged_or_ambiguous_song_links(untrusted: str, parser) -> None:
    """#157: hostile anchors cannot inject a fabricated song/producer into a catalogue."""
    html = f'<a href="{untrusted}">Forged artist</a>'
    tracks = parser(html, handle="drinko") if parser is parse_profile else parser(html)
    assert tracks == []


@pytest.mark.parametrize(
    "valid",
    [
        f"/song/{CLIP_A}",
        f"/song/{CLIP_A}?share=1",
        f"https://suno.com/song/{CLIP_A}",
        f"https://www.suno.com/song/{CLIP_A}#track",
    ],
)
def test_dom_fallback_accepts_only_real_suno_song_links(valid: str) -> None:
    (track,) = parse_profile(f'<a href="{valid}">Mixed creator</a>', handle="drinko")
    assert track.id == CLIP_A
    assert track.page_url == f"https://suno.com/song/{CLIP_A}"


@pytest.mark.parametrize("parser", [parse_profile, parse_playlist, parse_song])
@pytest.mark.parametrize("hostile_duration", ["Infinity", "-Infinity", "1e500", 10**400])
def test_rsc_nonfinite_duration_cannot_erase_or_crash_song(
    parser, hostile_duration: str | int
) -> None:
    """Issue #157: untrusted numeric metadata must not discard a valid clip."""
    html = _rsc_html([{"id": CLIP_A, "title": "Æsthaeth 🎵 e\u0301", "duration": hostile_duration}])
    tracks = parser(html, handle="source") if parser is parse_profile else parser(html)
    assert len(tracks) == 1
    assert tracks[0].id == CLIP_A
    assert tracks[0].title == "Æsthaeth 🎵 e\u0301"
    assert tracks[0].duration_sec is None


@pytest.mark.parametrize("hostile_count", ["Infinity", "-Infinity", float("inf"), float("-inf")])
def test_rsc_nonfinite_play_count_is_unknown_not_a_crash(hostile_count: str | float) -> None:
    """Numerics in clip-local statistics are untrusted like producer fields."""
    from suno_dl.parser import _clip_to_track

    track = _clip_to_track(
        {"id": CLIP_A, "title": "Track", "play_count": hostile_count},
        default_handle="source",
    )
    assert track is not None
    assert track.play_count is None
    assert track.artist == "source"


def test_rsc_valid_numeric_metadata_still_preserved() -> None:
    from suno_dl.parser import _clip_to_track

    track = _clip_to_track(
        {"id": CLIP_A, "title": "Track", "duration": "213.9", "play_count": "123"},
        default_handle="source",
    )
    assert track is not None
    assert track.duration_sec == 213
    assert track.play_count == 123


@pytest.mark.parametrize(
    "bad_canonical",
    [
        None,
        f"https://attacker.invalid/song/{CLIP_A}",
        f"https://suno.com.attacker.invalid/song/{CLIP_A}",
        f"//attacker.invalid/song/{CLIP_A}",
        f"javascript:alert(1)//song/{CLIP_A}",
        f"/redirect?next=/song/{CLIP_A}",
        f"https://suno.com/song/{CLIP_A}/another",
        f"https://suno.com/song/{CLIP_A}%2fextra",
        f"https://user@suno.com/song/{CLIP_A}",
    ],
)
def test_song_og_fallback_rejects_ambiguous_or_foreign_identity(
    bad_canonical: str | None,
) -> None:
    """#157: cover-asset UUID and hostile canonical URLs are not clip identity."""
    canonical = (
        f'<link rel="canonical" href="{bad_canonical}">' if bad_canonical is not None else ""
    )
    html = (
        f'<meta property="og:image" content="https://cdn2.suno.ai/{CLIP_B}.jpeg">'
        '<meta property="og:title" content="Zoë 🎵">' + canonical
    )
    assert parse_song(html) == []


@pytest.mark.parametrize(
    "canonical",
    [
        f"/song/{CLIP_A}",
        f"https://suno.com/song/{CLIP_A}",
        f"https://www.suno.com/song/{CLIP_A}?share=private",
        f"https://suno.com/song/{CLIP_A}/#player",
    ],
)
def test_song_og_fallback_requires_canonical_and_normalizes_url(canonical: str) -> None:
    html = (
        f'<meta property="og:image" content="https://cdn2.suno.ai/{CLIP_B}.jpeg">'
        '<meta property="og:title" content="Zoë 🎵 e\u0301">'
        f'<link rel="canonical" href="{canonical}">'
    )
    tracks = parse_song(html)
    assert len(tracks) == 1
    assert tracks[0].id == CLIP_A
    assert tracks[0].page_url == f"https://suno.com/song/{CLIP_A}"
    assert tracks[0].title == "Zoë 🎵 e\u0301"


def test_song_og_fallback_accepts_one_trusted_legacy_song_anchor() -> None:
    html = (
        f'<meta property="og:image" content="https://cdn2.suno.ai/{CLIP_B}.jpeg">'
        f'<a href="/song/{CLIP_A}">Zoë 🎵</a>'
    )
    tracks = parse_song(html)
    assert len(tracks) == 1
    assert tracks[0].id == CLIP_A
    assert tracks[0].page_url == f"https://suno.com/song/{CLIP_A}"


def test_song_og_fallback_rejects_conflicting_legacy_song_anchors() -> None:
    html = f'<a href="/song/{CLIP_A}">One</a><a href="/song/{CLIP_B}">Two</a>'
    assert parse_song(html) == []


def test_song_og_fallback_rejects_untrusted_canonical_even_with_valid_anchor() -> None:
    html = (
        f'<link rel="canonical" href="https://attacker.invalid/song/{CLIP_A}">'
        f'<a href="/song/{CLIP_A}">One</a>'
    )
    assert parse_song(html) == []


@pytest.mark.parametrize("source", ["drinko", "@drinko", "playlistowner"])
def test_dom_only_profile_cannot_forge_clip_producer_from_source(source: str) -> None:
    """#157: profile provenance is not proof of a DOM-only clip's producer."""
    from suno_dl.sidecar import build_sidecar

    html = f'<a href="/song/{CLIP_A}" data-user="different_producer">Zoë 🎵 shared song</a>'
    (track,) = parse_profile(html, handle=source)
    assert track.id == CLIP_A
    assert track.title == "Zoë 🎵 shared song"
    assert track.artist is None
    assert track.creator_handle is None
    provenance = build_sidecar(
        track,
        handle=source.lstrip("@"),
        saved_files=["synthetic.m4a"],
        downloaded_at="2026-10-30T00:00:00Z",
    )
    assert provenance["artist"] is None
    assert provenance["creator_handle"] is None
    assert provenance["handle"] == source.lstrip("@")


def test_mixed_rsc_and_dom_profile_preserves_known_creator_and_unknown_producer() -> None:
    """Do not corrupt verified RSC creator credits when DOM fallback adds tracks."""
    html = (
        _rsc_html(
            [
                {
                    "id": CLIP_A,
                    "title": "Verified producer song",
                    "user": {"display_name": "Zoë 🎵", "handle": "zoe"},
                }
            ]
        )
        + f'<a href="/song/{CLIP_B}">Shared unrelated song</a>'
    )
    tracks = parse_profile(html, handle="drinko")
    assert [track.id for track in tracks] == [CLIP_A, CLIP_B]
    assert (tracks[0].artist, tracks[0].creator_handle) == ("Zoë 🎵", "zoe")
    assert (tracks[1].artist, tracks[1].creator_handle) == (None, None)


def test_dom_only_playlist_producer_remains_unknown() -> None:
    (track,) = parse_playlist(f'<a href="/song/{CLIP_A}">Shared song</a>')
    assert track.artist is None
    assert track.creator_handle is None
