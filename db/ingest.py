import logging

from sqlalchemy.exc import IntegrityError

from db.database import SessionLocal
from db.models import SyncSource
from db.repository import ChannelRepository, PostRepository
from services.channel_filters import is_excluded_feed_channel
from services.forward import resolve_channel_metadata
from services.message_parser import (
    build_media_json,
    extract_channel_from_body_html,
    CONTENT_VERSION,
    extract_display_html,
    format_source_html,
    parse_channel_info,
    resolve_post_media_type,
)

logger = logging.getLogger(__name__)


async def save_post_from_summary(
    summary_messages,
    *,
    source_messages=None,
    chat=None,
    client=None,
    sync_source: str = SyncSource.LIVE.value,
) -> bool:
    if not summary_messages:
        return False

    if not isinstance(summary_messages, list):
        summary_messages = [summary_messages]

    summary_messages = sorted(summary_messages, key=lambda m: m.id)
    source_messages = sorted(source_messages or summary_messages, key=lambda m: m.id)

    first_source = source_messages[0]
    first_summary = summary_messages[0]
    grouped_id = getattr(first_source, "grouped_id", None) or getattr(first_summary, "grouped_id", None)

    media_type = resolve_post_media_type(summary_messages)
    media_json = build_media_json(summary_messages)

    raw_summary = first_summary.text or first_summary.message or ""
    parsed_title, parsed_username, parsed_link = await resolve_channel_metadata(
        first_summary, client, raw_summary
    )

    channel_title = parsed_title or (getattr(chat, "title", None) if chat else None)
    channel_username = parsed_username or (getattr(chat, "username", None) if chat else None)

    if not channel_title and first_source is not first_summary:
        channel_title = getattr(chat, "title", None) if chat else None
        channel_username = channel_username or (getattr(chat, "username", None) if chat else None)

    if parsed_link:
        media_json["source_link"] = parsed_link
    if raw_summary:
        media_json["raw_header"] = raw_summary
    if channel_title:
        media_json["channel_title"] = channel_title

    if is_excluded_feed_channel(channel_username, channel_title):
        logger.info(
            "Skip excluded feed channel summary_msg_id=%s channel=%s",
            first_summary.id,
            channel_title or channel_username,
        )
        return False

    if channel_username and getattr(first_source, "id", None) and not media_json.get("source_link"):
        if first_summary.forward and getattr(first_summary.forward, "channel_post", None):
            media_json["source_link"] = (
                f"https://t.me/{channel_username}/{first_summary.forward.channel_post}"
            )
        elif channel_username:
            media_json["source_link"] = f"https://t.me/{channel_username}/{first_source.id}"

    # A live post still has the original message at hand; a backfilled one
    # only has the tgsvodka copy, which the feed replaces with the original
    # once it is shown.
    from_original = first_source.chat_id != first_summary.chat_id
    text = None
    if from_original:
        text = next((html for html in map(format_source_html, source_messages) if html), None)
    content_source = "original" if text else "summary"
    text = text or extract_display_html(summary_messages)
    if text:
        media_json["content_html"] = text
        media_json["content_version"] = CONTENT_VERSION
        media_json["content_source"] = content_source

    if not channel_title and text:
        body_title, body_username, body_link = parse_channel_info(text)
        if not body_title:
            body_title, body_username, body_link = extract_channel_from_body_html(text)
        channel_title = channel_title or body_title
        channel_username = channel_username or body_username
        if body_link and not media_json.get("source_link"):
            media_json["source_link"] = body_link
        if channel_title:
            media_json["channel_title"] = channel_title

    try:
        async with SessionLocal() as session:
            repo = PostRepository(session)
            if await repo.exists_by_summary(first_summary.chat_id, first_summary.id):
                return False
            await repo.create_post(
                summary_chat_id=first_summary.chat_id,
                summary_msg_id=first_summary.id,
                source_chat_id=first_source.chat_id,
                source_msg_id=first_source.id,
                channel_id=first_source.chat_id if from_original else None,
                published_at=getattr(first_summary, "date", None),
                grouped_id=grouped_id,
                channel_title=channel_title,
                channel_username=channel_username,
                text=text,
                media_type=media_type,
                media_json=media_json,
                sync_source=sync_source,
            )
        logger.info(
            "Saved %s post summary_msg_id=%s channel=%s",
            sync_source,
            first_summary.id,
            channel_title,
        )
        return True
    except IntegrityError:
        return False
    except Exception as exc:
        logger.error("Failed to save post to database: %s", exc)
        return False


async def save_ingested_post(source_messages, summary_messages, chat, client=None) -> None:
    if not isinstance(summary_messages, list):
        summary_messages = [summary_messages]

    await save_post_from_summary(
        summary_messages,
        source_messages=source_messages,
        chat=chat,
        client=client,
        sync_source=SyncSource.LIVE.value,
    )


def _post_link(chat, message_id: int) -> str | None:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    # Private channels only open for members, which is who sees them here.
    chat_id = getattr(chat, "id", None)
    return f"https://t.me/c/{chat_id}/{message_id}" if chat_id else None


async def save_direct_post(messages, chat, *, sync_source: str = SyncSource.LIVE.value) -> bool:
    """Save a post straight from its channel, with no summary-channel copy.

    The post's own message stands in for the summary one: media is downloaded
    from the source channel and (chat_id, msg_id) dedupes it across users.
    """
    if not messages:
        return False
    if not isinstance(messages, list):
        messages = [messages]
    messages = sorted(messages, key=lambda m: m.id)
    first = messages[0]
    title = getattr(chat, "title", None)
    username = getattr(chat, "username", None)
    if is_excluded_feed_channel(username, title):
        return False

    media_json = build_media_json(messages)
    link = _post_link(chat, first.id)
    if link:
        media_json["source_link"] = link
    if title:
        media_json["channel_title"] = title
    text = next((html for html in map(format_source_html, messages) if html), None)
    if text:
        media_json["content_html"] = text
        media_json["content_version"] = CONTENT_VERSION
        media_json["content_source"] = "original"

    try:
        async with SessionLocal() as session:
            repo = PostRepository(session)
            channels = ChannelRepository(session)
            channel = await channels.get_by_tg_id(first.chat_id)
            if channel is not None:
                newest = max(m.id for m in messages)
                if (channel.last_msg_id or 0) < newest:
                    channel.last_msg_id = newest
                channel.title = title or channel.title
                channel.username = username or channel.username
                await session.commit()
            if await repo.exists_by_summary(first.chat_id, first.id):
                return False
            await repo.create_post(
                summary_chat_id=first.chat_id,
                summary_msg_id=first.id,
                source_chat_id=first.chat_id,
                source_msg_id=first.id,
                channel_id=first.chat_id,
                published_at=first.date,
                grouped_id=getattr(first, "grouped_id", None),
                channel_title=title,
                channel_username=username,
                text=text,
                media_type=resolve_post_media_type(messages),
                media_json=media_json,
                sync_source=sync_source,
            )
        logger.info("Saved %s post %s/%s channel=%s", sync_source, first.chat_id, first.id, title)
        return True
    except IntegrityError:
        return False
    except Exception as exc:
        logger.error("Failed to save direct post: %s", exc)
        return False
