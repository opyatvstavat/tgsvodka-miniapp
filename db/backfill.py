import logging

from telethon.utils import get_peer_id

from config import BACKFILL_LIMIT
from db.database import SessionLocal
from db.models import SyncSource
from db.ingest import save_post_from_summary
from db.repository import PostRepository
from services.forward import resolve_channel_metadata
from services.telegram_ids import normalize_chat_id
from services.message_parser import (
    build_media_json,
    resolve_post_media_type,
)

logger = logging.getLogger(__name__)


async def _collect_album_messages(client, chat, anchor_message):
    grouped_id = anchor_message.grouped_id
    if not grouped_id:
        return [anchor_message]

    start_id = anchor_message.id - 15
    end_id = anchor_message.id + 15
    album_messages = []
    async for msg in client.iter_messages(chat, min_id=start_id, max_id=end_id):
        if msg.grouped_id == grouped_id:
            album_messages.append(msg)
    album_messages.sort(key=lambda m: m.id)
    return album_messages or [anchor_message]


async def _refresh_existing_post(repo: PostRepository, post, batch, client) -> None:
    first = batch[0]
    raw = first.text or first.message or ""
    title, username, link = await resolve_channel_metadata(first, client, raw)
    if not title and post.text:
        from services.message_parser import parse_channel_info

        parsed_title, parsed_username, parsed_link = parse_channel_info(post.text)
        title = title or parsed_title
        username = username or parsed_username
        link = link or parsed_link
    media_json = dict(post.media_json or {})
    media_type = resolve_post_media_type(batch)
    built = build_media_json(batch)

    if link:
        media_json["source_link"] = link
    if title:
        media_json["channel_title"] = title
    if raw:
        media_json["raw_header"] = raw
    if built.get("items"):
        media_json["items"] = built["items"]
        media_json["summary_msg_ids"] = built["summary_msg_ids"]

    needs_update = (
        (title and not post.channel_title)
        or (username and not post.channel_username)
        or (not post.channel_title)
        or (built.get("items") and not (post.media_json or {}).get("items"))
        or (media_type != "text" and post.media_type == "text")
        or (media_type != post.media_type and media_type in {"voice", "audio"})
    )
    if not needs_update:
        return

    await repo.update_post_metadata(
        post,
        channel_title=title or post.channel_title,
        channel_username=username or post.channel_username,
        media_json=media_json,
        media_type=media_type if media_type != "text" else post.media_type,
    )
    logger.info("Refreshed metadata for summary_msg_id=%s title=%s", first.id, title)


async def backfill_summary_channel(client, target_channel: str, limit: int | None = None) -> dict:
    scan_limit = limit if limit is not None else BACKFILL_LIMIT
    # get_input_entity answers from the session cache; get_entity would send
    # ResolveUsername on every hourly run.
    chat = await client.get_input_entity(target_channel)
    summary_chat_id = normalize_chat_id(get_peer_id(chat))

    seen_grouped: set[int] = set()
    scanned = 0
    added = 0
    skipped = 0
    refreshed = 0

    async for message in client.iter_messages(chat):
        if not message:
            continue

        if message.grouped_id:
            if message.grouped_id in seen_grouped:
                continue
            seen_grouped.add(message.grouped_id)
            batch = await _collect_album_messages(client, chat, message)
        else:
            batch = [message]

        scanned += 1
        first = batch[0]

        async with SessionLocal() as session:
            repo = PostRepository(session)
            existing = await repo.get_by_summary(summary_chat_id, first.id)

        if existing:
            async with SessionLocal() as session:
                repo = PostRepository(session)
                post = await repo.get_by_summary(summary_chat_id, first.id)
                if post:
                    before_title = post.channel_title
                    await _refresh_existing_post(repo, post, batch, client)
                    if not before_title and post.channel_title:
                        refreshed += 1
            skipped += 1
        elif await save_post_from_summary(
            batch, sync_source=SyncSource.BACKFILL.value, client=client
        ):
            added += 1

        if scanned >= scan_limit:
            break

    async with SessionLocal() as session:
        repo = PostRepository(session)
        messages_in_db = await repo.count_distinct_summary(summary_chat_id)
        await repo.upsert_sync_state(
            summary_chat_id=summary_chat_id,
            channel_messages_total=scanned,
            messages_in_db=messages_in_db,
            messages_added_last_run=added,
            messages_skipped_existing=skipped,
        )

    result = {
        "backfill_limit": scan_limit,
        "scanned": scanned,
        "messages_in_db": messages_in_db,
        "added": added,
        "skipped_existing": skipped,
        "refreshed": refreshed,
        "untracked": max(scanned - (added + skipped), 0),
    }
    logger.info("Backfill complete (last %s posts): %s", scan_limit, result)
    return result
