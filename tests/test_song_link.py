import httpx
import pytest

from suno_dl.song_link import resolve_song_link


@pytest.mark.parametrize("location", ["https://example.com/song/fake", "//127.0.0.1/song/fake"])
async def test_share_lookup_rejects_offsite_redirects(mock_http, location):
    requests = []

    def redirect(request):
        requests.append(str(request.url))
        return httpx.Response(307, headers={"Location": location})

    mock_http(redirect)
    with pytest.raises(ValueError):
        await resolve_song_link("https://suno.com/s/TestShareCode123")
    assert requests == ["https://suno.com/s/TestShareCode123"]
