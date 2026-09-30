import logging
from collections.abc import AsyncIterator
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import PhotoSize, PhotoSizeProgressive

from config import API_HASH, API_ID, SESSION_NAME, WEB_SESSION_STRING

logger = logging.getLogger(__name__)

_client: TelegramClient | None = None


def set_client(client: TelegramClient) -> None:
    """Share an already connected client (the worker's) with media delivery.

    One process should hold one Telethon connection: the worker creates the
    client, media downloads reuse it instead of opening a second session.
    """
    global _client
    _client = client


async def get_client() -> TelegramClient:
    global _client
    if _client is None:
        if not API_ID or not API_HASH:
            raise RuntimeError("API_ID and API_HASH are required for media download")
        if WEB_SESSION_STRING:
            _client = TelegramClient(StringSession(WEB_SESSION_STRING), API_ID, API_HASH)
        else:
            _client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        await _client.connect()
        if not await _client.is_user_authorized():
            raise RuntimeError("Telethon session is not authorized")
        logger.info("Telethon media client connected")
    elif not _client.is_connected():
        await _client.connect()
    return _client


async def close_client() -> None:
    global _client
    if _client and _client.is_connected():
        await _client.disconnect()
    _client = None


def mime_for_message(message) -> str:
    if getattr(message, "photo", None):
        return "image/jpeg"
    if getattr(message, "video", None):
        return "video/mp4"
    document = getattr(message, "document", None)
    if document and getattr(document, "mime_type", None):
        return document.mime_type
    return "application/octet-stream"


def media_file_size(message) -> int | None:
    document = getattr(message, "document", None)
    if document and getattr(document, "size", None):
        return document.size

    video = getattr(message, "video", None)
    if video and getattr(video, "size", None):
        return video.size

    photo = getattr(message, "photo", None)
    if not photo:
        return None

    sizes = getattr(photo, "sizes", None) or []
    if not sizes:
        return None

    largest = sizes[-1]
    if isinstance(largest, PhotoSizeProgressive):
        progressive_sizes = getattr(largest, "sizes", None) or []
        return progressive_sizes[-1] if progressive_sizes else None
    if isinstance(largest, PhotoSize):
        return getattr(largest, "size", None)
    return None


async def get_message_with_media(chat_id: int, message_id: int):
    client = await get_client()
    message = await client.get_messages(chat_id, ids=message_id)
    if not message or not message.media:
        raise FileNotFoundError(f"No media in message {message_id}")
    return message, client


async def download_message_media(message, target: Path) -> None:
    client = await get_client()
    target.parent.mkdir(parents=True, exist_ok=True)
    result = await client.download_media(message, file=target)
    if not result or not target.exists() or target.stat().st_size == 0:
        raise FileNotFoundError(f"Failed to download media for message {message.id}")


async def download_message_preview(message, target: Path) -> None:
    client = await get_client()
    target.parent.mkdir(parents=True, exist_ok=True)
    thumb = None
    photo = getattr(message, "photo", None)
    if photo and getattr(photo, "sizes", None):
        for candidate in reversed(photo.sizes):
            if isinstance(candidate, PhotoSize):
                thumb = candidate
                break

    result = await client.download_media(message, file=target, thumb=thumb or -1)
    if not result or not target.exists() or target.stat().st_size == 0:
        await download_message_media(message, target)


async def iter_message_bytes(
    message,
    client: TelegramClient,
    *,
    offset: int = 0,
    limit: int | None = None,
    chunk_size: int = 256 * 1024,
) -> AsyncIterator[bytes]:
    file_size = media_file_size(message)
    async for chunk in client.iter_download(
        message.media,
        file_size=file_size,
        offset=offset,
        limit=limit,
        chunk_size=chunk_size,
    ):
        if chunk:
            yield chunk
