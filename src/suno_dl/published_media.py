"""Published own-clip assets only; never manufacture a CDN URL.

Producer identity remains independently verified from the canonical document
head. Video sources are additionally limited to the official, exact UUID path:
even a clip-shaped string inside lyrics cannot select another clip or host.
"""

from __future__ import annotations

import math
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .models import TrackInfo
from .parser import _clip_objects, _extract_next_f_blob
from .urls import UUID_RE, is_allowed_media_url, song_page_url


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
    own = [
        d
        for d in _clip_objects(_extract_next_f_blob(html), reject_duplicate_keys=True)
        if d.get("id") == clip_id
    ]
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
            ):
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
