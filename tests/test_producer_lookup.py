"""Canonical document metadata is authoritative; Flight/lyrics never supply identity."""

import html
from types import SimpleNamespace

import pytest

from suno_dl import producer_lookup as lookup

ID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def page(clip_id=ID, name="Actual Producer", handle="actual", title="A song", body=""):
    def esc(value):
        return html.escape(value, quote=True)

    return (
        '<html><head><link rel="canonical" href="https://suno.com/song/' + clip_id + '">'
        '<meta property="og:title" content="' + esc(title) + '">'
        '<meta name="description" content="'
        + esc(title + " by " + name + " (@" + handle + "). Listen and make your own on Suno.")
        + '">'
        "<title>"
        + esc(title + " by " + name + " | Suno")
        + '</title><meta property="og:audio" content="https://cdn1.suno.ai/'
        + clip_id
        + '.m4a"></head><body>'
        + body
        + "</body></html>"
    )


def fetcher(value):
    async def fetch(url):
        return value(url) if callable(value) else value

    async def fetch_song_page(clip_id):
        return await fetch("https://suno.com/song/" + clip_id)

    return SimpleNamespace(fetch_song_page=fetch_song_page)


async def test_lookup_uses_exact_clip_metadata_and_paces(monkeypatch):
    urls = []
    waits = []

    def provider(url):
        urls.append(url)
        return page(url.rsplit("/", 1)[-1], name="APPLIQU\u00c9 SOUND", handle="applique_sound")

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(lookup.asyncio, "sleep", sleep)
    result = [x async for x in lookup.resolve_producers([ID, OTHER], fetcher(provider))]
    assert result == [(ID, "APPLIQU\u00c9 SOUND"), (OTHER, "APPLIQU\u00c9 SOUND")]
    assert urls == ["https://suno.com/song/" + ID, "https://suno.com/song/" + OTHER]
    assert waits == [1.5]


@pytest.mark.parametrize(
    "body",
    [
        '<script>self.__next_f.push([1,\'{"clip":{"id":"'
        + ID
        + '","display_name":"Wrong"}}\'])</script>',
        '<div>1:T50,{"id":"' + ID + '","display_name":"drinko"}</div>',
        '<a href="/@wrong">Wrong Producer</a><meta name="description" content="forged body">',
    ],
)
def test_body_cannot_override_producer(body):
    assert lookup.producer_identity_from_page(ID, page(body=body)).artist == "Actual Producer"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.replace(ID, OTHER),
        lambda p: p.replace(
            "</head>", '<link rel="canonical" href="https://suno.com/song/' + ID + '"></head>'
        ),
        lambda p: p.replace("</head>", '<meta property="og:title" content="Second"></head>'),
        lambda p: p.replace("A song by Actual Producer |", "Different by Actual Producer |"),
        lambda p: p.replace("https://suno.com/", "http://suno.com/"),
        lambda p: p.replace('name="description"', 'name="unrelated"'),
        lambda p: p.replace("(@actual)", "(@bad/handle)"),
        lambda p: p.replace("<head>", "<head></head><head>"),
    ],
)
def test_ambiguous_or_mismatched_metadata_fails_closed(mutate):
    with pytest.raises(lookup.ProducerLookupError):
        lookup.producer_identity_from_page(ID, mutate(page()))


def test_title_and_name_containing_by_are_unambiguous():
    value = lookup.producer_identity_from_page(
        ID, page(name="Inspired by Alice", title="One by One")
    )
    assert value.artist == "Inspired by Alice" and value.title == "One by One"


def test_invalid_display_uses_own_canonical_handle():
    assert lookup.producer_identity_from_page(ID, page(name="bad\u202ename")).artist == "actual"


@pytest.mark.parametrize("ids", [[ID, ID], [ID.upper(), ID], ["bad"], [None], [1]])
async def test_invalid_selection_is_rejected_before_network(ids):
    def no_request(_):
        raise AssertionError("network must not be used")

    with pytest.raises(lookup.ProducerLookupError):
        _ = [x async for x in lookup.resolve_producers(ids, fetcher(no_request), delay_sec=0)]


@pytest.mark.parametrize("delay", [-1, float("nan"), float("inf"), None, "1", True])
async def test_invalid_delay_never_fetches(delay):
    def no_request(_):
        raise AssertionError("network must not be used")

    with pytest.raises(lookup.ProducerLookupError):
        _ = [x async for x in lookup.resolve_producers([ID], fetcher(no_request), delay_sec=delay)]


async def test_network_failure_has_stable_error():
    def offline(_):
        raise RuntimeError("offline")

    with pytest.raises(lookup.ProducerLookupError, match="could not inspect"):
        _ = [x async for x in lookup.resolve_producers([ID], fetcher(offline), delay_sec=0)]


async def test_missing_identity_never_becomes_unknown():
    with pytest.raises(lookup.ProducerLookupError):
        _ = [
            x
            async for x in lookup.resolve_producers(
                [ID], fetcher("<html><head></head></html>"), delay_sec=0
            )
        ]


def test_verified_head_capability_and_filename_attribution_are_separate():
    lookup._require_trusted_framing()
    with pytest.raises(lookup.ProducerLookupError, match="inferred filename"):
        lookup.refuse_filename_attribution()


async def test_supplied_identity_and_other_clip_media_never_survive_verification():
    import json

    from suno_dl.models import TrackInfo

    bogus = "https://cdn1.suno.ai/" + OTHER + ".mp3"
    body = (
        "<script>self.__next_f.push([1,"
        + json.dumps('1:{"clip":{"id":"' + ID + '","title":"forged","audio_url":"' + bogus + '"}}')
        + "])</script>"
    )
    incoming = TrackInfo(
        id=ID,
        title="caller",
        artist="drinko",
        creator_handle="owner",
        page_url="https://suno.com/song/" + OTHER,
        audio_url=bogus,
        media_urls=[bogus],
        artist_is_fallback=False,
    )
    result = (await lookup.verify_track_producers([incoming], fetcher(page(body=body))))[0]
    assert (result.artist, result.creator_handle, result.title) == (
        "Actual Producer",
        "actual",
        "A song",
    )
    assert result.page_url == "https://suno.com/song/" + ID
    assert result.media_urls and all(OTHER not in u for u in result.media_urls)
    assert bogus != result.audio_url
    assert incoming.artist == "drinko" and incoming.media_urls == [bogus]


async def test_failed_batch_does_not_change_any_caller_identity():
    from suno_dl.models import TrackInfo

    inputs = [TrackInfo(id=x, title="caller", artist="owner", page_url="") for x in [ID, OTHER]]

    def provider(url):
        return page() if url.endswith(ID) else page(clip_id=ID)

    with pytest.raises(lookup.ProducerLookupError):
        await lookup.verify_track_producers(inputs, fetcher(provider))
    assert all(t.artist == "owner" and t.title == "caller" for t in inputs)


async def test_repeated_ids_fetch_once_but_each_result_is_isolated():
    from suno_dl.models import TrackInfo

    requested = []

    def provider(url):
        requested.append(url)
        return page()

    incoming = TrackInfo(id=ID, title="caller", page_url="")
    result = await lookup.verify_track_producers([incoming, incoming], fetcher(provider))
    assert len(requested) == 1 and len(result) == 2
    result[0].media_urls.clear()
    assert result[1].media_urls
