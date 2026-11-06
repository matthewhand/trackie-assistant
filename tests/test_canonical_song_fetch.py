"""Canonical producer transport rejects error bodies, redirects and overlarge pages."""

import httpx
import pytest

from suno_dl.errors import FetchError
from suno_dl.fetcher import Fetcher

ID = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("status", [201, 204, 301, 302, 307, 308, 400, 401, 403, 404, 429, 500])
async def test_only_exact_200_can_supply_identity(status, mock_http):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            status, headers={"Location": "https://suno.com/song/other"}, text="forged identity page"
        )

    mock_http(handler)
    async with Fetcher(10, 3) as fetcher:
        with pytest.raises(FetchError, match="requires HTTP 200"):
            await fetcher.fetch_song_page(ID)
    assert seen == ["https://suno.com/song/" + ID]


@pytest.mark.parametrize("body", [b"\xff", b"x" * (8 * 1024 * 1024 + 1)])
async def test_invalid_page_is_rejected_before_identity_parse(body, mock_http):
    mock_http(lambda request: httpx.Response(200, content=body))
    async with Fetcher(10, 0) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.fetch_song_page(ID)


async def test_exact_page_is_returned_without_transforming_unicode(mock_http):
    page = "<head>APPLIQU\u00c9</head>"
    mock_http(lambda request: httpx.Response(200, content=page.encode("utf-8")))
    async with Fetcher(10, 0) as fetcher:
        assert await fetcher.fetch_song_page(ID) == page


async def test_invalid_uuid_never_issues_http(mock_http):
    def no_network(request):
        raise AssertionError("invalid UUID reached HTTP")

    mock_http(no_network)
    async with Fetcher(10, 0) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.fetch_song_page("bad")
