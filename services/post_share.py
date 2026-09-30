"""A feed post as a ready Telegram message for WebApp.shareMessage.

The Mini App cannot forward a channel post, so the bot prepares a message with
the post's first media and its original text; Telegram then lets the user pick
a chat for it.  Telegram downloads the media itself, through short-lived
signed links that need no initData.
"""

import asyncio
import hashlib
import hmac
import logging
import time
import uuid

from telethon.tl import types

from config import BOT_TOKEN, WEBAPP_URL
from services.channel_social import source_context
from services.media_delivery import cache_media
from services.telethon_pool import get_message_with_media, media_file_size, mime_for_message

logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
# Telegram fetches media by URL only up to these sizes.
VIDEO_URL_LIMIT = 20 * 1024 * 1024
PHOTO_URL_LIMIT = 5 * 1024 * 1024
SIGNED_URL_TTL = 3600

_SIMPLE_ENTITIES = {
    types.MessageEntityBold: "bold",
    types.MessageEntityItalic: "italic",
    types.MessageEntityUnderline: "underline",
    types.MessageEntityStrike: "strikethrough",
    types.MessageEntitySpoiler: "spoiler",
    types.MessageEntityCode: "code",
    types.MessageEntityUrl: "url",
    types.MessageEntityMention: "mention",
    types.MessageEntityHashtag: "hashtag",
    types.MessageEntityCashtag: "cashtag",
    types.MessageEntityBotCommand: "bot_command",
    types.MessageEntityEmail: "email",
    types.MessageEntityPhone: "phone_number",
}


def _signing_key() -> bytes:
    return hmac.new(b"ShareMedia", (BOT_TOKEN or "").encode(), hashlib.sha256).digest()


def _signature(post_id, index: int, preview: bool, expires: int) -> str:
    payload = f"{post_id}:{index}:{int(preview)}:{expires}".encode()
    return hmac.new(_signing_key(), payload, hashlib.sha256).hexdigest()


def signed_media_url(post_id, index: int, *, preview: bool) -> str:
    expires = int(time.time()) + SIGNED_URL_TTL
    signature = _signature(post_id, index, preview, expires)
    base = WEBAPP_URL.rstrip("/")
    return f"{base}/api/share-media/{post_id}/{index}?preview={int(preview)}&exp={expires}&sig={signature}"


def verify_media_signature(post_id, index: int, preview: bool, expires: int, signature: str) -> bool:
    if expires < time.time():
        return False
    return hmac.compare_digest(_signature(post_id, index, preview, expires), signature)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _utf16_cut(text: str, limit: int) -> str:
    # Dropping a half surrogate pair keeps the cut on a whole character.
    return text.encode("utf-16-le")[: limit * 2].decode("utf-16-le", errors="ignore")


def bot_entities(entities) -> list[dict]:
    """Telethon entities in Bot API form; both count offsets in UTF-16 units.

    Custom emoji are dropped: a bot may only send those with a Fragment
    username, and the entity's text is already the plain emoji.
    """
    result = []
    for entity in entities or []:
        base = {"offset": entity.offset, "length": entity.length}
        kind = _SIMPLE_ENTITIES.get(type(entity))
        if kind:
            result.append({"type": kind, **base})
        elif isinstance(entity, types.MessageEntityPre):
            item = {"type": "pre", **base}
            if entity.language:
                item["language"] = entity.language
            result.append(item)
        elif isinstance(entity, types.MessageEntityTextUrl):
            result.append({"type": "text_link", "url": entity.url, **base})
        elif isinstance(entity, types.MessageEntityBlockquote):
            result.append({"type": "expandable_blockquote" if entity.collapsed else "blockquote", **base})
    return result


def compose_text(title: str | None, link: str | None, body: str, entities: list[dict], limit: int) -> tuple[str, list[dict]]:
    """Channel title line, then the post body cut to fit ``limit``."""
    header = f"{title}\n\n" if title else ""
    header_entities = []
    if title:
        header_entities.append({"type": "bold", "offset": 0, "length": _utf16_len(title)})
        if link:
            header_entities.append({"type": "text_link", "offset": 0, "length": _utf16_len(title), "url": link})

    body = body.rstrip()
    budget = limit - _utf16_len(header)
    if _utf16_len(body) > budget:
        body = _utf16_cut(body, budget - 1).rstrip() + "…"
    body_end = _utf16_len(body)

    shift = _utf16_len(header)
    shifted = []
    for entity in entities:
        start = entity["offset"]
        end = min(start + entity["length"], body_end)
        if start >= body_end or end <= start:
            continue
        shifted.append({**entity, "offset": start + shift, "length": end - start})
    text = (header + body).rstrip()
    return text, header_entities + shifted


async def _original_message(post):
    try:
        client, entity, message_id = await source_context(post)
        return await client.get_messages(entity, ids=message_id)
    except Exception as exc:
        logger.info("Share without original text for %s: %s", post.id, exc)
        return None


async def _media_size(post, item: dict) -> tuple[int | None, str | None]:
    message_id = item.get("summary_msg_id", post.summary_msg_id)
    try:
        message, _client = await get_message_with_media(post.summary_chat_id, message_id)
    except Exception as exc:
        logger.info("Share media lookup failed for %s: %s", post.id, exc)
        return None, None
    return media_file_size(message), mime_for_message(message)


async def _warm_media(post, item: dict, *, full: bool) -> None:
    """Put the shared files in the cache before Telegram asks for them.

    Telegram fetches them while the message is being prepared and gives up on a
    slow download, sending the message without media; streaming a video from
    Telegram through Telethon is that slow.
    """
    message_id = item.get("summary_msg_id", post.summary_msg_id)
    jobs = [cache_media(post.summary_chat_id, message_id, preview=True)]
    if full:
        jobs.append(cache_media(post.summary_chat_id, message_id))
    for outcome in await asyncio.gather(*jobs, return_exceptions=True):
        if isinstance(outcome, Exception):
            logger.warning("Share media warm failed for %s: %s", post.id, outcome)


async def build_share_result(post, *, title: str | None, link: str | None, items: list[dict], allow_video: bool = True) -> dict:
    """An InlineQueryResult for savePreparedInlineMessage."""
    message = await _original_message(post)
    body = (message.message or "") if message else ""
    entities = bot_entities(message.entities) if message else []

    result: dict = {"id": uuid.uuid4().hex}
    if link:
        result["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть пост", "url": link}]]}

    first = items[0] if items else None
    kind = first and first.get("type")
    if kind in {"photo", "video"}:
        size, mime = await _media_size(post, first)
        if kind == "video" and (not allow_video or mime != "video/mp4" or not size or size > VIDEO_URL_LIMIT):
            kind = "preview"
        if kind == "photo" and size and size > PHOTO_URL_LIMIT:
            kind = "preview"
    else:
        kind = None

    if kind is None:
        text, text_entities = compose_text(title, link, body, entities, TEXT_LIMIT)
        result.update(
            type="article",
            title=title or "Пост",
            input_message_content={
                "message_text": text or link or "Пост",
                "entities": text_entities,
                "link_preview_options": {"is_disabled": True},
            },
        )
        return result

    await _warm_media(post, first, full=kind != "preview")
    caption, caption_entities = compose_text(title, link, body, entities, CAPTION_LIMIT)
    thumbnail = signed_media_url(post.id, 0, preview=True)
    result.update(caption=caption, caption_entities=caption_entities, thumbnail_url=thumbnail)
    if kind == "video":
        result.update(
            type="video",
            video_url=signed_media_url(post.id, 0, preview=False),
            mime_type="video/mp4",
            title=title or "Видео",
        )
    else:
        # A video too big to send goes out as its preview frame.
        result.update(type="photo", photo_url=signed_media_url(post.id, 0, preview=kind == "preview"))
    return result
