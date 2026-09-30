import re

from services.message_parser import strip_html

YOUTUBE_PATTERNS = (
    re.compile(r"https?://(?:www\.)?youtube\.com/watch\?[^\s\"'<>]*v=([\w-]{11})", re.I),
    re.compile(r"https?://(?:www\.)?youtube\.com/shorts/([\w-]{11})", re.I),
    re.compile(r"https?://(?:www\.)?youtu\.be/([\w-]{11})", re.I),
    re.compile(r"https?://(?:www\.)?youtube\.com/embed/([\w-]{11})", re.I),
)


def extract_youtube_embeds(text: str | None) -> list[dict]:
    if not text:
        return []

    haystack = f"{text}\n{strip_html(text) or ''}"
    found: list[dict] = []
    seen: set[str] = set()

    for pattern in YOUTUBE_PATTERNS:
        for match in pattern.finditer(haystack):
            video_id = match.group(1)
            if video_id in seen:
                continue
            seen.add(video_id)
            found.append(
                {
                    "type": "youtube",
                    "video_id": video_id,
                    "url": match.group(0),
                }
            )

    return found
