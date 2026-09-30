import os
import asyncio
import ctypes
import gc
import logging

from telethon import TelegramClient, events
from telethon.extensions import html as tg_html
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from dotenv import load_dotenv

from config import ALLOWED_USER_ID, SESSION_STRING, SUMMARY_MIRROR
from db.backfill import backfill_summary_channel
from db.database import SessionLocal, init_db, release_sqlite_page_cache
from db.ingest import save_direct_post, save_ingested_post
from services.catalog import all_catalog_usernames
from services.channel_filters import is_ignored_source_chat
from services.channel_registry import (
    catch_up_channels,
    ensure_channel_for_chat,
    join_worker,
    known_channel_peers,
    leave_orphans,
    poll_worker,
    queue_catalog_channels,
)
from services.telethon_pool import set_client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# SESSION_STRING comes from config so the API's "should ingest run?" check and
# the client built here can never disagree.
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "session_name")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")
HIGHLIGHTS_CHANNEL = os.getenv("HIGHLIGHTS_CHANNEL")

own_channels_str = os.getenv("OWN_CHANNELS", "")
OWN_CHANNELS = [ch.strip().lstrip("@") for ch in own_channels_str.split(",") if ch.strip()]

# Telethon maintains the update stream on its own; this is a slow keepalive that
# reconnects and re-syncs, not the 30s polling loop it replaced.
FORCE_UPDATE_INTERVAL = int(os.getenv("FORCE_UPDATE_INTERVAL", "300"))

# Backfill is a safety net for posts missed while the process was down, not the
# ingest path — live events add new posts. A full pass scans BACKFILL_LIMIT
# messages and takes ~1.5 min, so running it every 10 minutes burned CPU and
# Telegram traffic to find nothing. Startup still backfills immediately, which
# is what actually covers a restart.
BACKFILL_INTERVAL = int(os.getenv("BACKFILL_INTERVAL", "3600"))

client: TelegramClient | None = None


def release_idle_memory() -> None:
    """Return objects and SQLite pages created by a completed backfill.

    Telethon's catch-up and a 1,000-message scan create many short-lived
    objects. Python may retain their allocator arenas indefinitely, and Railway
    bills that retained memory. ``malloc_trim`` is available on production's
    glibc image; macOS and other platforms simply skip it.
    """
    collected = gc.collect()
    release_sqlite_page_cache()
    trimmed = False
    try:
        trimmed = bool(ctypes.CDLL("libc.so.6").malloc_trim(0))
    except OSError:
        pass
    logger.info("Released idle backfill memory (gc=%s, malloc_trim=%s)", collected, trimmed)


async def warm_entity_cache() -> None:
    """Put subscribed chats into the session cache.

    A StringSession forgets entities on restart, so each @username lookup
    became a ResolveUsername request, and Telegram blocks those for hours once
    there are too many. Dialogs bring the same channels with their access
    hashes, and ``get_input_entity`` then answers from the cache.
    """
    count = 0
    try:
        async for _dialog in client.iter_dialogs():
            count += 1
    except Exception as e:
        logger.error("Could not load dialogs: %s", e)
    logger.info("Cached %s dialogs", count)

    # Polled channels were never joined, so dialogs lack them; their stored
    # access hashes let media and comments reach them without a lookup.
    try:
        peers = await known_channel_peers()
        client.session.process_entities(peers)
        logger.info("Cached %s channel peers from the database", len(peers))
    except Exception as e:
        logger.error("Could not cache channel peers: %s", e)


def create_client() -> TelegramClient:
    """Build the worker client and attach handlers.

    Importing this module must stay free of side effects so the API process can
    embed the worker: configuration problems raise instead of calling exit().
    """
    global client
    if client is not None:
        return client

    if not API_ID or not API_HASH:
        raise RuntimeError("API_ID and API_HASH are required to start the worker")

    try:
        api_id = int(API_ID)
    except ValueError as exc:
        raise RuntimeError("API_ID must be an integer") from exc

    if SESSION_STRING:
        client = TelegramClient(StringSession(SESSION_STRING), api_id, API_HASH, catch_up=True)
    else:
        if not os.isatty(0) or os.getenv("DOCKER", "").lower() in {"1", "true", "yes"}:
            raise RuntimeError(
                "SESSION_STRING is required in Docker/non-interactive mode. "
                "Run locally: python bot.py (login once), then python scripts/export_session.py "
                "and paste the result into .env as SESSION_STRING=..."
            )
        client = TelegramClient(SESSION_NAME, api_id, API_HASH, catch_up=True)

    client.add_event_handler(handle_reactions, events.MessageEdited())
    client.add_event_handler(forward_post, events.NewMessage())
    set_client(client)
    return client


albums = {}
album_tasks = {}


class AlbumType:
    PUBLIC = "public"
    PRIVATE = "private"
    REACTION = "reaction"
    # Saved straight from the channel, no summary-channel copy.
    DIRECT = "direct"


def _is_target_channel(channel) -> bool:
    return channel == TARGET_CHANNEL


async def force_update_task():
    """Keepalive: reconnect if the link dropped, then pull anything missed.

    The previous version ran every 30s and additionally issued get_me,
    GetState/GetDifference and a get_entity per own channel — work Telethon's
    own update loop already does, which kept the process busy around the clock.
    """
    while True:
        try:
            await asyncio.sleep(FORCE_UPDATE_INTERVAL)

            if not client.is_connected():
                logger.warning("Telethon disconnected, reconnecting...")
                await client.connect()

            try:
                await client.catch_up()
            except Exception as catch_error:
                logger.warning("catch_up error: %s", catch_error)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("force-update error: %s", e)


async def process_album(grouped_id, album_type, target_channel, delay=2):
    source_messages = None
    try:
        await asyncio.sleep(delay)

        source_messages = albums.get(grouped_id)
        if not source_messages:
            return
        # Album parts arrive as separate updates, not necessarily in order.
        source_messages.sort(key=lambda msg: msg.id)

        sent_messages = None
        chat = await source_messages[0].get_chat()

        if album_type == AlbumType.DIRECT:
            await save_direct_post(source_messages, chat)
            return

        if album_type == AlbumType.PUBLIC:
            sent_messages = await send_public_album(source_messages, target_channel)
        elif album_type == AlbumType.REACTION:
            first_message = source_messages[0]
            if first_message.forward:
                sent_messages = await client.forward_messages(
                    target_channel, source_messages, drop_author=False
                )
            else:
                sent_messages = await client.forward_messages(
                    target_channel, source_messages, drop_author=True
                )
        else:
            sent_messages = await client.forward_messages(target_channel, source_messages)

        if _is_target_channel(target_channel) and album_type in (AlbumType.PUBLIC, AlbumType.PRIVATE):
            await save_ingested_post(source_messages, sent_messages, chat, client=client)

    except Exception as e:
        logger.error("Album processing error %s: %s", grouped_id, e)
    finally:
        albums.pop(grouped_id, None)
        album_tasks.pop(grouped_id, None)


async def send_public_album(messages, target_channel):
    first_message = messages[0]
    chat = await first_message.get_chat()

    media_files = [msg.media for msg in messages if msg.media]
    text_message = next((msg for msg in messages if msg.message), first_message)
    # Entity offsets refer to the raw ``message.message``; ``message.text`` is
    # Markdown and shifts every tag after the first formatted fragment.
    text_content = (
        tg_html.unparse(text_message.message, text_message.entities)
        if text_message.message
        else ""
    )

    channel_name = getattr(chat, "title", "Unknown Channel")

    if hasattr(chat, "username") and chat.username:
        post_link = f"https://t.me/{chat.username}/{first_message.id}"
        header = f'<a href="{post_link}">👤</a> <code>{channel_name}</code>'
    else:
        header = f"👤 <code>{channel_name}</code>"

    caption = f"{header}\n\n<blockquote>{text_content}</blockquote>" if text_content else header

    if media_files:
        return await client.send_file(
            target_channel, media_files, caption=caption, parse_mode="html"
        )
    return await client.send_message(target_channel, caption, parse_mode="html", link_preview=False)


def add_to_album(grouped_id, message, album_type, target_channel, delay=2):
    if grouped_id not in albums:
        albums[grouped_id] = []

    if album_type == AlbumType.REACTION:
        if any(msg.id == message.id for msg in albums[grouped_id]):
            return

    albums[grouped_id].append(message)

    if grouped_id not in album_tasks:
        album_tasks[grouped_id] = asyncio.create_task(
            process_album(grouped_id, album_type, target_channel, delay)
        )


async def create_custom_post(message, chat, target_channel):
    try:
        channel_name = getattr(chat, "title", "Unknown Channel")

        if hasattr(chat, "username") and chat.username:
            post_link = f"https://t.me/{chat.username}/{message.id}"
            header = f'<a href="{post_link}">👤</a> <code>{channel_name}</code>'
        else:
            header = f"👤 <code>{channel_name}</code>"

        body = tg_html.unparse(message.message, message.entities) if message.message else ""
        text = f"{header}\n\n<blockquote>{body}</blockquote>" if body else header

        if message.media and isinstance(message.media, (MessageMediaPhoto, MessageMediaDocument)):
            sent = await client.send_file(target_channel, message.media, caption=text, parse_mode="html")
        else:
            sent = await client.send_message(target_channel, text, parse_mode="html", link_preview=False)

        if _is_target_channel(target_channel):
            await save_ingested_post([message], sent, chat, client=client)

    except Exception as e:
        logger.error("create_custom_post error: %s", e)


async def handle_reactions(event):
    try:
        chat = await event.get_chat()

        if not (hasattr(chat, "username") and chat.username in OWN_CHANNELS):
            return

        if hasattr(event.message, "reactions") and event.message.reactions:
            for reaction in event.message.reactions.results:
                if reaction.count > 0:
                    grouped_id = event.message.grouped_id

                    if grouped_id:
                        album_messages = []
                        start_id = event.message.id - 10
                        end_id = event.message.id + 10

                        async for msg in client.iter_messages(chat, min_id=start_id, max_id=end_id):
                            if msg.grouped_id == grouped_id:
                                album_messages.append(msg)

                        album_messages.sort(key=lambda x: x.id)

                        if album_messages:
                            if grouped_id in albums:
                                albums[grouped_id] = []

                            for msg in album_messages:
                                add_to_album(grouped_id, msg, AlbumType.REACTION, HIGHLIGHTS_CHANNEL)
                    else:
                        if event.message.forward:
                            await client.forward_messages(
                                HIGHLIGHTS_CHANNEL, event.message, drop_author=False
                            )
                        else:
                            if event.message.media:
                                await client.forward_messages(
                                    HIGHLIGHTS_CHANNEL, event.message, drop_author=True
                                )
                            elif event.message.text:
                                await client.send_message(
                                    HIGHLIGHTS_CHANNEL, event.message.text, link_preview=False
                                )
                    break

    except Exception as e:
        logger.error("handle_reactions error: %s", e)


async def forward_post(event):
    try:
        logger.info("New message received")

        chat = await event.get_chat()

        if is_ignored_source_chat(chat):
            return

        if hasattr(chat, "username") and chat.username in OWN_CHANNELS:
            return

        if not hasattr(chat, "broadcast") or not chat.broadcast:
            logger.info("Not a channel message, skipping")
            return

        grouped_id = event.message.grouped_id

        if not SUMMARY_MIRROR:
            if await ensure_channel_for_chat(chat) is None:
                return
            if grouped_id:
                add_to_album(grouped_id, event.message, AlbumType.DIRECT, None, delay=2)
            else:
                await save_direct_post([event.message], chat)
            return

        is_private = not hasattr(chat, "username") or chat.username is None

        if is_private:
            if grouped_id:
                add_to_album(grouped_id, event.message, AlbumType.PRIVATE, TARGET_CHANNEL, delay=3)
            else:
                sent = await client.forward_messages(TARGET_CHANNEL, event.message)
                await save_ingested_post([event.message], sent, chat, client=client)
        else:
            if grouped_id:
                add_to_album(grouped_id, event.message, AlbumType.PUBLIC, TARGET_CHANNEL, delay=1)
            else:
                await create_custom_post(event.message, chat, TARGET_CHANNEL)

    except Exception as e:
        logger.error("forward_post error: %s", e)


async def backfill_task():
    while True:
        try:
            await asyncio.sleep(BACKFILL_INTERVAL)
            if SUMMARY_MIRROR and TARGET_CHANNEL:
                await backfill_summary_channel(client, TARGET_CHANNEL)
            elif not SUMMARY_MIRROR:
                await catch_up_channels(client)
                await leave_orphans(client)
            release_idle_memory()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Backfill task error: %s", e)


async def _initial_catch_up():
    try:
        await catch_up_channels(client)
        release_idle_memory()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("Initial catch-up error: %s", e)


async def run_worker():
    """Start Telethon ingest and stay running until the connection ends."""
    create_client()
    logger.info("Starting worker...")

    await client.start()
    logger.info("Telethon client started")

    me = await client.get_me()
    logger.info("Authorized as %s (@%s)", me.first_name, me.username)

    await init_db()
    logger.info("Database initialized")

    async with SessionLocal() as session:
        from db.repository import PostRepository

        skipped = await PostRepository(session).skip_excluded_pending()
        if skipped:
            logger.info("Auto-skipped %s pending posts from excluded channels", skipped)

    await client.catch_up()
    await warm_entity_cache()

    for channel in OWN_CHANNELS:
        try:
            await client.get_input_entity(f"@{channel}")
            logger.info("Channel @%s found", channel)
        except Exception as e:
            logger.error("Channel @%s error: %s", channel, e)

    if SUMMARY_MIRROR and TARGET_CHANNEL:
        try:
            await backfill_summary_channel(client, TARGET_CHANNEL)
            release_idle_memory()
        except Exception as e:
            logger.error("Initial backfill error: %s", e)

    background = [
        asyncio.create_task(force_update_task()),
        asyncio.create_task(backfill_task()),
    ]
    if not SUMMARY_MIRROR:
        if not ALLOWED_USER_ID:
            queued = await queue_catalog_channels(all_catalog_usernames())
            if queued:
                logger.info("Queued %s catalog channels for joining", queued)
        background.append(asyncio.create_task(join_worker(client)))
        background.append(asyncio.create_task(poll_worker(client)))
        background.append(asyncio.create_task(_initial_catch_up()))
    logger.info("Worker ready")

    try:
        await client.run_until_disconnected()
    finally:
        for task in background:
            task.cancel()


if __name__ == "__main__":
    create_client()
    with client:
        client.loop.run_until_complete(run_worker())
