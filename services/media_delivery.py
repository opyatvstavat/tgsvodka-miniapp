import asyncio
import logging
import os
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.responses import StreamingResponse
from services.telethon_pool import (
    download_message_media,
    download_message_preview,
    get_message_with_media,
    iter_message_bytes,
    media_file_size,
    mime_for_message,
)

logger = logging.getLogger(__name__)

CACHE_DIR = Path(os.getenv("MEDIA_CACHE_DIR", "/tmp/tgsvodka_media"))
CHUNK_SIZE = 256 * 1024
CACHE_CONTROL = "public, max-age=86400, immutable"
CACHE_MAX_BYTES = int(os.getenv("MEDIA_CACHE_MAX_MB", "1500")) * 1024 * 1024

# Locks are per (chat_id, message_id); keep the map bounded so a long-lived
# process does not accumulate one Lock per media item ever requested.
LOCK_CACHE_SIZE = 512

_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
_warm_tasks: dict[str, asyncio.Task] = {}

_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "application/x-tgsticker": ".tgs",
    "video/quicktime": ".mov",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
}
_MIME_BY_EXT = {ext: mime for mime, ext in _EXT_BY_MIME.items()}


def _prune_locks(protect: str) -> None:
    """Drop the oldest locks nobody is holding, never the one in use."""
    if len(_locks) <= LOCK_CACHE_SIZE:
        return
    for key in list(_locks):
        if len(_locks) <= LOCK_CACHE_SIZE:
            break
        if key == protect or _locks[key].locked():
            continue
        del _locks[key]


def _lock_for(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    _locks.move_to_end(key)
    _prune_locks(key)
    return lock


def _ext_for_mime(mime: str) -> str:
    return _EXT_BY_MIME.get(mime, ".bin")


def _mime_for_ext(ext: str) -> str:
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def _cache_key(chat_id: int, message_id: int, *, preview: bool = False) -> str:
    suffix = "_thumb" if preview else ""
    return f"{chat_id}_{message_id}{suffix}"


def _cache_path(chat_id: int, message_id: int, mime: str, *, preview: bool = False) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{_cache_key(chat_id, message_id, preview=preview)}{_ext_for_mime(mime)}"


def _find_cached(chat_id: int, message_id: int, *, preview: bool = False) -> tuple[Path, str] | None:
    """Locate an already downloaded file without asking Telegram for the message."""
    key = _cache_key(chat_id, message_id, preview=preview)
    try:
        candidates = sorted(CACHE_DIR.glob(f"{key}.*"))
    except OSError:
        return None

    for path in candidates:
        if path.suffix == ".part":
            continue
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path, _mime_for_ext(path.suffix)
        except OSError:
            continue
    return None


def _is_cache_file(path: Path) -> bool:
    """Only ever delete files this module created: <chat_id>_<msg_id>[_thumb].<ext>.

    Eviction must never touch anything else that shares the directory — the
    SQLite database in particular.
    """
    if not re.fullmatch(r"-?\d+_\d+(_thumb)?", path.stem):
        return False
    # ".bin" is what _ext_for_mime falls back to for unrecognised mime types.
    return path.suffix in _MIME_BY_EXT or path.suffix == ".bin"


def _evict_cache() -> None:
    """Keep CACHE_DIR under CACHE_MAX_BYTES, dropping least recently used files."""
    entries = []
    total = 0
    try:
        for path in CACHE_DIR.iterdir():
            if not path.is_file() or path.suffix == ".part" or not _is_cache_file(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            total += stat.st_size
            entries.append((stat.st_atime, stat.st_size, path))
    except OSError:
        return

    if total <= CACHE_MAX_BYTES:
        return

    entries.sort(key=lambda entry: entry[0])
    for _atime, size, path in entries:
        if total <= CACHE_MAX_BYTES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size


def _drop_page_cache(handle) -> None:
    """Release the pages this file just occupied.

    Railway bills container memory including the page cache, so bytes read off
    the volume are charged as if they were resident memory. A media file is read
    when its post is viewed and rarely again, so keeping it cached costs money
    for no benefit. Linux only; a no-op elsewhere.
    """
    if not hasattr(os, "posix_fadvise"):
        return
    try:
        os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    except OSError:
        pass


async def _stream_file(path: Path, start: int = 0, length: int | None = None) -> AsyncIterator[bytes]:
    remaining = length
    with path.open("rb") as handle:
        try:
            if start:
                handle.seek(start)
            while True:
                size = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
                if size <= 0:
                    break
                chunk = await asyncio.to_thread(handle.read, size)
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk
        finally:
            _drop_page_cache(handle)


def parse_range_header(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header or not range_header.startswith("bytes="):
        return None
    match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1
    end = min(end, file_size - 1)
    if start > end or start < 0 or file_size <= 0:
        return None
    return start, end


def _response_headers(mime: str, *, file_size: int | None = None) -> dict[str, str]:
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": CACHE_CONTROL,
    }
    if file_size is not None:
        headers["Content-Length"] = str(file_size)
    return headers


def _serve_from_disk(path: Path, mime: str, range_header: str | None = None):
    """Serve a cached file. Everything goes through _stream_file rather than
    FileResponse so the page cache can be released afterwards."""
    file_size = path.stat().st_size
    byte_range = parse_range_header(range_header, file_size)

    if byte_range:
        start, end = byte_range
        length = end - start + 1
        headers = _response_headers(mime)
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        headers["Content-Length"] = str(length)
        return StreamingResponse(
            _stream_file(path, start, length),
            status_code=206,
            media_type=mime,
            headers=headers,
        )

    return StreamingResponse(
        _stream_file(path),
        media_type=mime,
        headers=_response_headers(mime, file_size=file_size),
    )


async def _ensure_cached(chat_id: int, message_id: int, *, preview: bool = False) -> tuple[Path, str]:
    cached = _find_cached(chat_id, message_id, preview=preview)
    if cached:
        return cached

    message, _client = await get_message_with_media(chat_id, message_id)
    mime = "image/jpeg" if preview else mime_for_message(message)
    path = _cache_path(chat_id, message_id, mime, preview=preview)

    async with _lock_for(_cache_key(chat_id, message_id, preview=preview)):
        if path.exists() and path.stat().st_size > 0:
            return path, mime

        tmp = path.with_suffix(path.suffix + ".part")
        try:
            if preview:
                await download_message_preview(message, tmp)
            else:
                await download_message_media(message, tmp)
            tmp.replace(path)
        finally:
            if tmp.exists() and not path.exists():
                tmp.unlink(missing_ok=True)

    await asyncio.to_thread(_evict_cache)
    return path, mime


async def cache_media(chat_id: int, message_id: int, *, preview: bool = False) -> None:
    """Download a media item to disk ahead of a request that must be served fast."""
    await _ensure_cached(chat_id, message_id, preview=preview)


def _schedule_cache_warm(chat_id: int, message_id: int) -> None:
    """Fill the cache once per media item, however many range requests arrive."""
    key = _cache_key(chat_id, message_id)
    if key in _warm_tasks:
        return

    async def _warm() -> None:
        try:
            await _ensure_cached(chat_id, message_id)
        except Exception as exc:
            logger.debug("Background cache warm failed %s/%s: %s", chat_id, message_id, exc)
        finally:
            _warm_tasks.pop(key, None)

    _warm_tasks[key] = asyncio.create_task(_warm())


async def serve_media(
    chat_id: int,
    message_id: int,
    *,
    preview: bool = False,
    range_header: str | None = None,
):
    cached = _find_cached(chat_id, message_id, preview=preview)
    if cached:
        return _serve_from_disk(cached[0], cached[1], range_header)

    if preview:
        path, mime = await _ensure_cached(chat_id, message_id, preview=True)
        return _serve_from_disk(path, mime, range_header)

    try:
        message, client = await get_message_with_media(chat_id, message_id)
        mime = mime_for_message(message)
        path = _cache_path(chat_id, message_id, mime)

        file_size = media_file_size(message)
        byte_range = parse_range_header(range_header, file_size) if file_size else None

        if byte_range and file_size:
            start, end = byte_range
            length = end - start + 1
            _schedule_cache_warm(chat_id, message_id)

            async def range_stream() -> AsyncIterator[bytes]:
                async for chunk in iter_message_bytes(message, client, offset=start, limit=length):
                    yield chunk

            headers = _response_headers(mime)
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            headers["Content-Length"] = str(length)
            return StreamingResponse(
                range_stream(),
                status_code=206,
                media_type=mime,
                headers=headers,
            )

        async def stream_to_cache() -> AsyncIterator[bytes]:
            async with _lock_for(_cache_key(chat_id, message_id)):
                if path.exists() and path.stat().st_size > 0:
                    async for chunk in _stream_file(path):
                        yield chunk
                    return

                tmp = path.with_suffix(path.suffix + ".part")
                try:
                    with tmp.open("wb") as handle:
                        async for chunk in iter_message_bytes(message, client):
                            handle.write(chunk)
                            yield chunk
                    tmp.replace(path)
                finally:
                    if tmp.exists() and not path.exists():
                        tmp.unlink(missing_ok=True)

            await asyncio.to_thread(_evict_cache)

        headers = _response_headers(mime)
        if file_size:
            headers["Content-Length"] = str(file_size)
        return StreamingResponse(stream_to_cache(), media_type=mime, headers=headers)
    except FileNotFoundError:
        raise
    except Exception as exc:
        logger.error("Media delivery failed %s/%s: %s", chat_id, message_id, exc)
        raise RuntimeError("Failed to deliver media") from exc
