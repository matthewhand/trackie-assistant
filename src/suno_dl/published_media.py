"""Published own-clip assets only; never manufacture a CDN URL.

Producer identity remains independently verified from the canonical document
head. Video sources use exact UUID paths or an explicitly bound uploaded asset:
even a clip-shaped string inside lyrics cannot select another clip or host.
"""

from __future__ import annotations

import math
import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .models import TrackInfo
from .rsc import flight_documents
from .urls import UUID_RE, is_allowed_media_url, song_page_url


def _bound_upload_video(data: dict, clip_id: str, video: str) -> bool:
    """Accept the observed upload variant only with its own matching source binding."""
    parts = urlsplit(video)
    match = re.fullmatch(
        r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_[0-9]{1,3})\.mp4",
        parts.path,
    )
    if (
        data.get("is_public") is not True
        or parts.scheme != "https"
        or parts.netloc != "cdn1.suno.ai"
        or parts.query
        or parts.fragment
        or match is None
    ):
        return False
    expected = "suno-data-uploads/studio/uploads/" + match.group(1) + ".m4a"
    for item in data.get("media_urls") or []:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        media = urlsplit(item["url"])
        if (
            media.scheme != "https"
            or media.netloc != "d2lwuy8qc234o3.cloudfront.net"
            or media.path != "/1/clip/" + clip_id + ".m4a"
            or media.fragment
            or item.get("encoding") != "1.0.0"
            or item.get("delivery") != "progressive"
            or item.get("content_type") != "m4a-opus"
        ):
            continue
        try:
            query = parse_qs(media.query, strict_parsing=True, max_num_fields=3)
        except ValueError:
            continue
        if (
            set(query) == {"src", "sig", "kid"}
            and query["src"] == [expected]
            and len(query["sig"]) == 1
            and re.fullmatch(r"[A-Za-z0-9_-]{20,128}", query["sig"][0])
            and query["kid"] == ["1"]
        ):
            return True
    return False


def published_sources(html: str, clip_id: str) -> tuple[list[str], TrackInfo]:
    if not UUID_RE.fullmatch(clip_id) or len(html.encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("Invalid clip page")
    soup = BeautifulSoup(html, "html.parser")
    heads = soup.find_all("head")
    if len(heads) != 1:
        raise ValueError("Clip page has no unique head")
    canonicals = heads[0].select('link[rel="canonical"]')
    if len(canonicals) != 1 or canonicals[0].get("href") != song_page_url(clip_id):
        raise ValueError("Clip asset page is not canonical")
    own = []
    for document in flight_documents(html):
        todo = [document]
        while todo:
            node = todo.pop()
            if isinstance(node, dict):
                for key in ("clip", "content_item", "data"):
                    data = node.get(key)
                    if (
                        isinstance(data, dict)
                        and data.get("id") == clip_id
                        and "title" in data
                        and (key != "data" or data.get("entity_type") == "song_schema")
                    ):
                        own.append(data)
                todo.extend(node.values())
            elif isinstance(node, list):
                todo.extend(node)
    audio: list[str] = []
    videos: set[str] = set()
    covers: set[str] = set()
    durations: list[float] = []
    public = None
    encoded_audio = False

    def add_audio(value):
        if not isinstance(value, str) or not is_allowed_media_url(value):
            return
        parts = urlsplit(value)
        official = (
            parts.netloc == "cdn1.suno.ai"
            and parts.path in {"/" + clip_id + suffix for suffix in (".m4a", ".mp3", ".wav")}
        ) or (
            parts.netloc == "d2lwuy8qc234o3.cloudfront.net"
            and parts.path == "/1/clip/" + clip_id + ".m4a"
        )
        if official and not parts.query and not parts.fragment and value not in audio:
            audio.append(value)

    for data in own:
        if data.get("is_public") is False:
            raise ValueError("Own clip is not public")
        if data.get("is_public") is True:
            public = True
        for key in ("audio_url", "mp3_url", "wav_url"):
            add_audio(data.get(key))
        for item in data.get("media_urls") or []:
            if isinstance(item, dict):
                # Encoded website streams are not normal downloadable audio.
                if item.get("encoding") not in (None, "", "none", "plain"):
                    encoded_audio = True
                    continue
                add_audio(item.get("url"))
            elif isinstance(item, str):
                add_audio(item)
        video = data.get("video_url")
        if isinstance(video, str):
            parts = urlsplit(video)
            if (
                parts.scheme == "https"
                and parts.netloc == "cdn1.suno.ai"
                and parts.path == "/" + clip_id + ".mp4"
                and not parts.query
                and not parts.fragment
            ) or _bound_upload_video(data, clip_id, video):
                videos.add(video)
        cover = data.get("image_large_url") or data.get("image_url")
        if isinstance(cover, str) and is_allowed_media_url(cover):
            covers.add(cover)
        meta = data.get("metadata")
        duration = data.get("duration") or (
            meta.get("duration") if isinstance(meta, dict) else None
        )
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and math.isfinite(duration)
            and 0 < duration <= 3600
        ):
            durations.append(duration)
    if len(videos) > 1 or len(covers) > 1:
        raise ValueError("Conflicting published clip assets")
    # DOM audio is only a fallback when no own-clip object explains its encoding.
    if not audio and not encoded_audio:
        for item in heads[0].select('meta[property="og:audio"]'):
            add_audio(item.get("content"))
    if not covers:
        images = heads[0].select('meta[property="og:image"]')
        if len(images) == 1:
            image = images[0].get("content")
            if isinstance(image, str) and is_allowed_media_url(image):
                covers.add(image)
    urls = audio + sorted(videos)
    return urls, TrackInfo(
        id=clip_id,
        title="",
        page_url=song_page_url(clip_id),
        audio_url=audio[0] if audio else None,
        media_urls=urls,
        cover_url=next(iter(covers), None),
        duration_sec=math.ceil(max(durations)) if durations else None,
        is_public=public,
    )
