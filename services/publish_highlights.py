import logging

from config import HIGHLIGHTS_CHANNEL, HIGHLIGHTS_CHAT_ID
from services.message_parser import (
    extract_channel_from_body_html,
    parse_channel_info,
)
from services.telethon_pool import get_client

logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024


def _highlights_target():
    if HIGHLIGHTS_CHANNEL:
        return HIGHLIGHTS_CHANNEL
    if HIGHLIGHTS_CHAT_ID:
        return int(HIGHLIGHTS_CHAT_ID)
    raise RuntimeError("HIGHLIGHTS_CHANNEL or HIGHLIGHTS_CHAT_ID is not configured")


def _resolve_source(post) -> tuple[str | None, str | None, str | None]:
    media_json = post.media_json or {}
    title = post.channel_title or media_json.get("channel_title")
    username = post.channel_username
    link = media_json.get("source_link")

    if not title and media_json.get("raw_header"):
        parsed_title, parsed_username, parsed_link = parse_channel_info(media_json["raw_header"])
        title = title or parsed_title
        username = username or parsed_username
        link = link or parsed_link

    if not title and post.text:
        parsed_title, parsed_username, parsed_link = parse_channel_info(post.text)
        if not parsed_title:
            parsed_title, parsed_username, parsed_link = extract_channel_from_body_html(post.text)
        title = title or parsed_title
        username = username or parsed_username
        link = link or parsed_link

    if not link and username and post.source_msg_id:
        link = f"https://t.me/{username}/{post.source_msg_id}"

    return title, username, link


def _build_header(title: str | None, username: str | None, source_link: str | None) -> str:
    from html import escape

    channel_name = escape(title or username or "Unknown Channel")
    if source_link:
        return f'<a href="{source_link}">👤</a> <code>{channel_name}</code>'
    return f"👤 <code>{channel_name}</code>"


def _build_caption(header: str, body_html: str | None) -> str:
    if not body_html:
        return header[:CAPTION_LIMIT]

    full = f"{header}\n\n<blockquote>{body_html}</blockquote>"
    if len(full) <= CAPTION_LIMIT:
        return full

    budget = CAPTION_LIMIT - len(header) - len("\n\n<blockquote></blockquote>")
    if budget < 32:
        return header[:CAPTION_LIMIT]

    trimmed = body_html[: max(budget - 3, 0)].rstrip()
    if trimmed.endswith("<"):
        trimmed = trimmed[:-1]
    return f"{header}\n\n<blockquote>{trimmed}…</blockquote>"


def _summary_message_ids(post) -> list[int]:
    media_json = post.media_json or {}
    ids = media_json.get("summary_msg_ids")
    if ids:
        return sorted({int(msg_id) for msg_id in ids})

    items = media_json.get("items") or []
    if items:
        return sorted({int(item["summary_msg_id"]) for item in items if item.get("summary_msg_id")})

    return [post.summary_msg_id]


async def _collect_album_messages(client, chat_id: int, anchor_message):
    grouped_id = anchor_message.grouped_id
    if not grouped_id:
        return [anchor_message]

    start_id = anchor_message.id - 15
    end_id = anchor_message.id + 15
    album_messages = []
    async for msg in client.iter_messages(chat_id, min_id=start_id, max_id=end_id):
        if msg.grouped_id == grouped_id:
            album_messages.append(msg)
    album_messages.sort(key=lambda msg: msg.id)
    return album_messages or [anchor_message]


async def _fetch_summary_messages(client, post):
    message_ids = _summary_message_ids(post)
    result = await client.get_messages(post.summary_chat_id, ids=message_ids)
    if not isinstance(result, list):
        result = [result]
    messages = sorted((msg for msg in result if msg), key=lambda msg: msg.id)
    if not messages:
        raise RuntimeError(f"Summary messages not found: {message_ids}")

    anchor = messages[0]
    if len(messages) == 1 and anchor.grouped_id:
        expanded = await _collect_album_messages(client, post.summary_chat_id, anchor)
        if len(expanded) > 1:
            messages = expanded
    elif post.grouped_id and len(messages) > 1:
        expanded = await _collect_album_messages(client, post.summary_chat_id, anchor)
        if len(expanded) > len(messages):
            messages = expanded

    return messages


async def publish_post_to_highlights(post) -> None:
    client = await get_client()
    target = _highlights_target()
    title, username, source_link = _resolve_source(post)
    header = _build_header(title, username, source_link)
    caption = _build_caption(header, post.text)

    messages = await _fetch_summary_messages(client, post)
    media_files = [msg.media for msg in messages if msg.media]

    if media_files:
        await client.send_file(target, media_files, caption=caption, parse_mode="html")
        logger.info(
            "Published %s media item(s) to highlights for post %s (source=%s)",
            len(media_files),
            post.id,
            title or username,
        )
        return

    if caption:
        await client.send_message(target, caption, parse_mode="html", link_preview=False)
        logger.info("Published text post to highlights for post %s", post.id)
        return

    first = messages[0]
    if first.text or first.message:
        await client.send_message(target, caption or header, parse_mode="html", link_preview=False)
        return

    raise RuntimeError("Nothing to publish to highlights")
