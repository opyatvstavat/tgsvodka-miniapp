"""Small, on-demand Telegram enrichments for a feed post.

Nothing from comments is persisted.  Avatar files are deliberately small and
bounded because the service runs on a memory-priced Railway container.
"""

import asyncio
import logging
import os
import re
from collections import OrderedDict
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest, GetDiscussionMessageRequest
from telethon.tl.types import DocumentAttributeCustomEmoji, ReactionCustomEmoji, ReactionEmoji, ReactionPaid
from telethon.utils import get_peer_id
from services.media_delivery import serve_media
from services.message_parser import format_source_html, strip_html
from services.telethon_pool import get_client

logger = logging.getLogger(__name__)

AVATAR_CACHE_DIR = Path(os.getenv("CHANNEL_AVATAR_CACHE_DIR", "/data/avatars"))
AVATAR_CACHE_MAX_BYTES = int(os.getenv("CHANNEL_AVATAR_CACHE_MAX_MB", "25")) * 1024 * 1024
AVATAR_LOCKS: OrderedDict[str, asyncio.Lock] = OrderedDict()
AVATAR_LOCK_LIMIT = 128
PUBLIC_POST_LINK = re.compile(r"https?://t\.me/([A-Za-z0-9_]{3,})/(\d+)")
# post id -> id of its channel's discussion group, where comments live.
DISCUSSION_CHATS: OrderedDict[object, int] = OrderedDict()
DISCUSSION_CHAT_LIMIT = 512
CUSTOM_EMOJI_CACHE_DIR = Path(os.getenv("CUSTOM_EMOJI_CACHE_DIR", "/tmp/tgsvodka_emoji"))
CUSTOM_EMOJI_CACHE_MAX_BYTES = 20 * 1024 * 1024
# document id -> Document of a custom emoji reaction; needed to download it.
CUSTOM_EMOJI_DOCS: OrderedDict[int, object] = OrderedDict()
CUSTOM_EMOJI_DOC_LIMIT = 256
CUSTOM_EMOJI_FORMATS = {
    "image/webp": ("static", ".webp"),
    "video/webm": ("video", ".webm"),
    "application/x-tgsticker": ("lottie", ".tgs"),
}
TOP_REACTIONS = 5


def _avatar_key(post) -> str | None:
    if post.channel_username:
        return f"username_{post.channel_username.lower()}"
    if post.source_chat_id:
        return f"chat_{post.source_chat_id}"
    return None


async def _serve_avatar(key: str, entity):
    AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = AVATAR_CACHE_DIR / f"{key}.jpg"
    if not path.exists() or path.stat().st_size == 0:
        async with _avatar_lock(key):
            if not path.exists() or path.stat().st_size == 0:
                if entity is None:
                    # Evicted between the cache check and this request.
                    raise HTTPException(status_code=404, detail="Аватар недоступен")
                client = await get_client()
                temporary = path.with_suffix(".part")
                try:
                    result = await client.download_profile_photo(entity, file=temporary)
                    if not result or not temporary.exists() or temporary.stat().st_size == 0:
                        raise HTTPException(status_code=404, detail="Аватар недоступен")
                    temporary.replace(path)
                finally:
                    if temporary.exists() and not path.exists():
                        temporary.unlink(missing_ok=True)
        await asyncio.to_thread(_evict_avatars)

    async def content():
        with path.open("rb") as avatar:
            while chunk := await asyncio.to_thread(avatar.read, 64 * 1024):
                yield chunk

    return StreamingResponse(content(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def _avatar_lock(key: str) -> asyncio.Lock:
    lock = AVATAR_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        AVATAR_LOCKS[key] = lock
    AVATAR_LOCKS.move_to_end(key)
    while len(AVATAR_LOCKS) > AVATAR_LOCK_LIMIT:
        old_key, old_lock = next(iter(AVATAR_LOCKS.items()))
        if old_lock.locked():
            break
        del AVATAR_LOCKS[old_key]
    return lock


def _evict_avatars() -> None:
    _evict_dir(AVATAR_CACHE_DIR, "*.jpg", AVATAR_CACHE_MAX_BYTES)


def _evict_dir(directory: Path, pattern: str, max_bytes: int) -> None:
    entries: list[tuple[float, int, Path]] = []
    total = 0
    try:
        for path in directory.glob(pattern):
            if path.suffix == ".part":
                continue
            stat = path.stat()
            total += stat.st_size
            entries.append((stat.st_atime, stat.st_size, path))
    except OSError:
        return
    for _atime, size, path in sorted(entries):
        if total <= max_bytes:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            continue


def _is_summary_copy(post) -> bool:
    """A backfilled post only knows the tgsvodka copy as its "source".

    A post saved straight from its channel also has source == summary, but
    there the summary chat is the channel itself.
    """
    return post.source_chat_id == post.summary_chat_id and post.channel_id != post.summary_chat_id


def _post_link_target(post) -> tuple[str | None, int | None]:
    link = (post.media_json or {}).get("source_link") or ""
    match = PUBLIC_POST_LINK.search(link)
    if match:
        return match.group(1), int(match.group(2))
    if _is_summary_copy(post):
        # Its source_msg_id is the copy's id, which points at an unrelated
        # message in the original channel.
        return post.channel_username, None
    return post.channel_username, post.source_msg_id


def _source_candidates(post, username):
    # The numeric id is tried first: after the worker loads dialogs it is in
    # the session cache and needs no request at all.
    if post.source_chat_id is not None and not _is_summary_copy(post):
        yield post.source_chat_id
    if username:
        yield username


async def _cached_entity(client, candidate):
    # get_input_entity reads the session cache before asking Telegram;
    # get_entity always sends ResolveUsername for an @name, and Telegram
    # blocks those for hours once there are too many.
    return await client.get_input_entity(candidate)


async def source_context(post):
    """Return the original Telegram entity and message id, or a 404 response."""
    client = await get_client()
    username, message_id = _post_link_target(post)
    if message_id:
        for candidate in _source_candidates(post, username):
            try:
                return client, await _cached_entity(client, candidate), int(message_id)
            except Exception as exc:
                logger.debug("Could not resolve source %s: %s", candidate, exc)
    raise HTTPException(status_code=404, detail="Оригинальный пост недоступен")


async def source_entity(post):
    """Resolve a channel for its avatar even when its post ID was not saved."""
    client = await get_client()
    username, _message_id = _post_link_target(post)
    for candidate in _source_candidates(post, username):
        try:
            return client, await _cached_entity(client, candidate)
        except Exception as exc:
            logger.debug("Could not resolve avatar source %s: %s", candidate, exc)
    raise HTTPException(status_code=404, detail="У канала нет аватара")


def _cached_avatar(key: str) -> Path | None:
    path = AVATAR_CACHE_DIR / f"{key}.jpg"
    return path if path.exists() and path.stat().st_size else None


async def serve_channel_avatar(post):
    key = _avatar_key(post)
    if not key:
        raise HTTPException(status_code=404, detail="У канала нет аватара")
    # A cached file needs no Telegram lookup at all.
    entity = None
    if not _cached_avatar(key):
        _client, entity = await source_entity(post)
    return await _serve_avatar(key, entity)


async def serve_user_avatar(user_id: int):
    key = f"user_{user_id}"
    entity = None
    if not _cached_avatar(key):
        client = await get_client()
        try:
            entity = await client.get_input_entity(user_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Аватар недоступен") from exc
    return await _serve_avatar(key, entity)


def _comment_html(message) -> str:
    return format_source_html(message) or ""


def _file_attr(message, name):
    try:
        return getattr(message.file, name, None) if message.file else None
    except Exception:
        return None


def _media_kind(message) -> str | None:
    if message.sticker:
        mime = _file_attr(message, "mime_type") or ""
        if mime == "application/x-tgsticker":
            return "sticker_lottie"
        return "sticker_video" if mime.startswith("video/") else "sticker"
    if message.gif:
        return "gif"
    if message.video_note:
        return "round"
    if message.video:
        return "video"
    if message.photo:
        return "photo"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.document:
        return "file"
    return None


def _other_media_label(message) -> str | None:
    media = getattr(message, "media", None)
    name = type(media).__name__ if media else ""
    if name == "MessageMediaPoll":
        question = getattr(getattr(media, "poll", None), "question", None)
        question = getattr(question, "text", question)
        return f"📊 Опрос: {question}" if question else "📊 Опрос"
    if name in {"MessageMediaGeo", "MessageMediaGeoLive", "MessageMediaVenue"}:
        return "📍 Геопозиция"
    if name == "MessageMediaContact":
        return "👤 Контакт"
    if name == "MessageMediaDice":
        return getattr(media, "emoticon", None) or "🎲"
    return None


def _comment_media(message) -> dict | None:
    kind = _media_kind(message)
    if kind is None:
        label = _other_media_label(message)
        return {"type": "other", "id": message.id, "label": label} if label else None
    item = {"type": kind, "id": message.id}
    for field in ("width", "height", "duration", "size", "emoji", "title", "performer"):
        value = _file_attr(message, field)
        if value:
            item[field] = value
    if kind == "file":
        item["name"] = _file_attr(message, "name") or "Файл"
    return item


MEDIA_LABELS = {
    "photo": "Фото",
    "sticker": "Стикер",
    "sticker_video": "Стикер",
    "sticker_lottie": "Стикер",
    "gif": "GIF",
    "round": "Видеосообщение",
    "video": "Видео",
    "voice": "Голосовое сообщение",
    "audio": "Аудио",
    "file": "Файл",
}


def _media_label(media: dict | None) -> str:
    if not media:
        return ""
    if media["type"] == "other":
        return media.get("label") or ""
    label = MEDIA_LABELS.get(media["type"], "")
    if media["type"].startswith("sticker") and media.get("emoji"):
        label = f"{label} {media['emoji']}"
    return label


def _remember_discussion_chat(post, chat_id: int | None) -> None:
    if not chat_id:
        return
    DISCUSSION_CHATS[post.id] = chat_id
    DISCUSSION_CHATS.move_to_end(post.id)
    while len(DISCUSSION_CHATS) > DISCUSSION_CHAT_LIMIT:
        DISCUSSION_CHATS.popitem(last=False)


async def _discussion_chat(post):
    """The discussion group of the post's channel, as an input entity."""
    client, entity, message_id = await source_context(post)
    discussion = await client(GetDiscussionMessageRequest(peer=entity, msg_id=message_id))
    peer = discussion.messages[0].peer_id
    _remember_discussion_chat(post, get_peer_id(peer))
    return await client.get_input_entity(peer)


async def serve_comment_media(post, comment_id: int, *, preview: bool, range_header: str | None):
    chat_id = DISCUSSION_CHATS.get(post.id)
    if chat_id is None:
        try:
            await _discussion_chat(post)
        except HTTPException:
            raise
        except Exception as exc:  # the post has no discussion group
            raise HTTPException(status_code=404, detail="Обсуждение недоступно") from exc
        chat_id = DISCUSSION_CHATS[post.id]
    try:
        return await serve_media(chat_id, comment_id, preview=preview, range_header=range_header)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _comment_sender_name(sender) -> str:
    return getattr(sender, "title", None) or " ".join(
        part for part in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if part
    ) or getattr(sender, "username", None) or "Пользователь Telegram"


def _reply_parent_id(message) -> int | None:
    """Return only a comment-to-comment reply, never the discussion root.

    A comment on the post itself replies to the root without a top id; a
    reply inside the thread always carries the root as its top id.
    """
    reply = getattr(message, "reply_to", None)
    parent_id = getattr(reply, "reply_to_msg_id", None)
    top_id = getattr(reply, "reply_to_top_id", None)
    if not parent_id or not top_id or parent_id == top_id:
        return None
    return int(parent_id)


async def _comment_reply_targets(client, messages) -> dict[int, object]:
    """Parents of comment replies, fetched from the discussion group.

    Comment ids belong to the channel's discussion group, not to the channel,
    so the parents must be looked up in the chat the comments came from.
    """
    loaded = {message.id: message for message in messages}
    parent_ids = {_reply_parent_id(message) for message in messages}
    parent_ids.discard(None)
    parents = {parent_id: loaded[parent_id] for parent_id in parent_ids if parent_id in loaded}
    missing = [parent_id for parent_id in parent_ids if parent_id not in parents]
    if missing:
        try:
            result = await client.get_messages(await messages[0].get_input_chat(), ids=missing)
        except Exception:
            result = []
        if not isinstance(result, list):
            result = [result]
        parents.update({message.id: message for message in result if message})
    # A top-level discussion post is the source post, not a user comment.
    return {parent_id: message for parent_id, message in parents.items() if not getattr(message, "post", False)}


async def _reply_preview(parent) -> dict | None:
    if not parent:
        return None
    try:
        parent_sender = await parent.get_sender()
    except Exception:
        parent_sender = None
    parent_text = (strip_html(_comment_html(parent)) or "").replace("\n", " ").strip()
    parent_text = parent_text or _media_label(_comment_media(parent))
    return {
        "id": parent.id,
        "author": _comment_sender_name(parent_sender),
        "text": parent_text[:180],
    }


def _comments_count(message) -> int:
    replies = getattr(message, "replies", None)
    if not replies or not getattr(replies, "comments", False):
        return 0
    return int(getattr(replies, "replies", 0) or 0)


def _remember_custom_emoji(document) -> None:
    CUSTOM_EMOJI_DOCS[document.id] = document
    CUSTOM_EMOJI_DOCS.move_to_end(document.id)
    while len(CUSTOM_EMOJI_DOCS) > CUSTOM_EMOJI_DOC_LIMIT:
        CUSTOM_EMOJI_DOCS.popitem(last=False)


async def _custom_emoji_docs(client, document_ids: list[int]) -> dict[int, object]:
    missing = [doc_id for doc_id in document_ids if doc_id not in CUSTOM_EMOJI_DOCS]
    if missing:
        for document in await client(GetCustomEmojiDocumentsRequest(document_id=missing)):
            _remember_custom_emoji(document)
    return {doc_id: CUSTOM_EMOJI_DOCS[doc_id] for doc_id in document_ids if doc_id in CUSTOM_EMOJI_DOCS}


def _custom_emoji_alt(document) -> str:
    for attribute in getattr(document, "attributes", None) or []:
        if isinstance(attribute, DocumentAttributeCustomEmoji):
            return attribute.alt or ""
    return ""


def _reaction_summary(message) -> dict:
    """Total reaction count and the most popular reactions, custom emoji unresolved."""
    results = getattr(getattr(message, "reactions", None), "results", None) or []
    counted = sorted((result for result in results if result.count > 0), key=lambda result: -result.count)
    top = []
    for result in counted[:TOP_REACTIONS]:
        reaction = result.reaction
        if isinstance(reaction, ReactionEmoji):
            top.append({"type": "emoji", "emoji": reaction.emoticon, "count": result.count})
        elif isinstance(reaction, ReactionCustomEmoji):
            # A string: the id is an int64, beyond what a JS number holds exactly.
            top.append({"type": "custom", "id": str(reaction.document_id), "count": result.count})
        elif isinstance(reaction, ReactionPaid):
            top.append({"type": "paid", "count": result.count})
    return {"total": sum(result.count for result in counted), "top": top}


async def _reactions(client, message) -> dict:
    """Total reaction count and the most popular reactions of a post."""
    summary = _reaction_summary(message)
    await _resolve_custom_emoji(client, summary["top"])
    return summary


async def _resolve_custom_emoji(client, reactions: list[dict]) -> None:
    """Add the alt emoji and file format to custom emoji reactions, in one request."""
    custom = [item for item in reactions if item["type"] == "custom"]
    if custom:
        try:
            documents = await _custom_emoji_docs(client, list({int(item["id"]) for item in custom}))
        except Exception as exc:
            # Without the documents the client shows a placeholder.
            logger.warning("Custom emoji lookup failed: %s", exc)
            documents = {}
        for item in custom:
            document = documents.get(int(item["id"]))
            if document is None:
                continue
            item["emoji"] = _custom_emoji_alt(document)
            kind = CUSTOM_EMOJI_FORMATS.get(document.mime_type)
            if kind:
                item["format"] = kind[0]


async def serve_custom_emoji(document_id: int):
    document = CUSTOM_EMOJI_DOCS.get(document_id)
    if document is None:
        client = await get_client()
        try:
            document = (await _custom_emoji_docs(client, [document_id])).get(document_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Эмодзи недоступен") from exc
    kind = CUSTOM_EMOJI_FORMATS.get(getattr(document, "mime_type", None)) if document else None
    if not kind:
        raise HTTPException(status_code=404, detail="Эмодзи недоступен")
    CUSTOM_EMOJI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CUSTOM_EMOJI_CACHE_DIR / f"{document_id}{kind[1]}"
    if not path.exists() or path.stat().st_size == 0:
        async with _avatar_lock(f"emoji_{document_id}"):
            if not path.exists() or path.stat().st_size == 0:
                client = await get_client()
                temporary = path.with_suffix(".part")
                try:
                    await client.download_media(document, file=temporary)
                    if not temporary.exists() or temporary.stat().st_size == 0:
                        raise HTTPException(status_code=404, detail="Эмодзи недоступен")
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        await asyncio.to_thread(_evict_dir, CUSTOM_EMOJI_CACHE_DIR, "*.*", CUSTOM_EMOJI_CACHE_MAX_BYTES)

    async def content():
        with path.open("rb") as emoji:
            while chunk := await asyncio.to_thread(emoji.read, 64 * 1024):
                yield chunk

    # A custom emoji never changes under its id.
    return StreamingResponse(content(), media_type=document.mime_type, headers={"Cache-Control": "private, max-age=604800"})


async def fetch_original_post(post) -> tuple[str | None, int, dict, int]:
    """Text, comment, reaction and forward counts of the post in its own channel.

    The text is None when the original has no caption, for example a bare
    photo; the tgsvodka copy's header is not a caption.
    """
    client, entity, message_id = await source_context(post)
    try:
        message = await client.get_messages(entity, ids=message_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Оригинальный пост недоступен") from exc
    if not message:
        raise HTTPException(status_code=404, detail="Оригинальный пост удалён")
    carrier = await _album_carrier(client, entity, message)
    # Every part of a forwarded album counts the forward, so either part will do.
    forwards = getattr(carrier, "forwards", None) or getattr(message, "forwards", None) or 0
    return format_source_html(message), _comments_count(carrier), await _reactions(client, carrier), forwards


async def _album_carrier(client, entity, message):
    """The album message that holds the reactions and comments.

    Telegram keeps them on a single message of an album, which is not
    necessarily the one the post links to.
    """
    if not message.grouped_id or _has_social(message):
        return message
    # An album has at most 10 items, so its other messages are within 9 ids.
    ids = [message_id for message_id in range(message.id - 9, message.id + 10) if message_id > 0 and message_id != message.id]
    try:
        neighbours = await client.get_messages(entity, ids=ids)
    except Exception as exc:
        logger.warning("Album lookup failed: %s", exc)
        return message
    album = [item for item in neighbours if item and item.grouped_id == message.grouped_id]
    return next((item for item in album if _has_social(item)), message)


def _has_social(message) -> bool:
    return bool(getattr(getattr(message, "reactions", None), "results", None) or _comments_count(message))


async def list_comments(post, *, after: int | None, limit: int) -> dict:
    try:
        client, entity, message_id = await source_context(post)
        messages = []
        # Telegram discussion order is chronological.  Fetch each page from
        # the oldest unread message toward the newer ones, as a normal chat.
        async for message in client.iter_messages(
            entity, reply_to=message_id, limit=limit, min_id=after or 0, reverse=True
        ):
            messages.append(message)
    except HTTPException as exc:
        return {"available": False, "message": exc.detail, "items": [], "next_cursor": None}
    except Exception as exc:
        logger.info("Comments unavailable for post %s: %s", post.id, exc)
        return {"available": False, "message": "У этого поста нет доступного обсуждения", "items": [], "next_cursor": None}

    if messages:
        _remember_discussion_chat(post, messages[0].chat_id)
    me = await client.get_me()
    parents = await _comment_reply_targets(client, messages)
    items = []
    for album in _albums(messages):
        # An album arrives as one message per file; its caption sits on one of them.
        message = next((part for part in album if part.message), album[0])
        try:
            sender = await message.get_sender()
        except Exception:
            sender = None
        items.append({
            "id": message.id,
            "author_id": getattr(sender, "id", None),
            "author": _comment_sender_name(sender),
            "mine": bool(me and getattr(sender, "id", None) == me.id),
            "text": _comment_html(message),
            "media": [media for part in album if (media := _comment_media(part))],
            "date": message.date.isoformat() if message.date else None,
            "reply_to_id": _reply_parent_id(message),
            "reply_to": await _reply_preview(parents.get(_reply_parent_id(message))),
            # Like a channel album, a comment album keeps reactions on one of its parts.
            "reactions": _reaction_summary(next((part for part in album if _reaction_summary(part)["total"]), message)),
        })
    await _resolve_custom_emoji(client, [reaction for item in items for reaction in item["reactions"]["top"]])
    return {
        "available": True,
        "message": None,
        "items": items,
        "next_cursor": messages[-1].id if len(messages) == limit else None,
    }


def _albums(messages) -> list[list]:
    albums: list[list] = []
    for message in messages:
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id and albums and getattr(albums[-1][0], "grouped_id", None) == grouped_id:
            albums[-1].append(message)
        else:
            albums.append([message])
    return albums


async def send_comment(post, *, text: str, reply_to: int | None = None) -> dict:
    """Post a comment from the server's Telegram account, whoever writes it."""
    client, entity, message_id = await source_context(post)
    me = await client.get_me()
    try:
        if reply_to:
            # A reply to a comment is sent to the discussion group itself;
            # Telegram keeps it in the post's thread because its parent is.
            message = await client.send_message(await _discussion_chat(post), text, reply_to=reply_to, link_preview=False)
        else:
            message = await client.send_message(entity, text, comment_to=message_id, link_preview=False)
    except Exception as exc:
        logger.info("Could not send comment for post %s: %s", post.id, exc)
        raise HTTPException(status_code=409, detail="Не удалось отправить комментарий в это обсуждение") from exc
    parents = await _comment_reply_targets(client, [message])
    return {
        "id": message.id,
        "author_id": me.id,
        "author": getattr(me, "first_name", None) or getattr(me, "username", None) or "Вы",
        "mine": True,
        "text": _comment_html(message),
        "media": [media for media in [_comment_media(message)] if media],
        "date": message.date.isoformat() if message.date else None,
        "reply_to_id": _reply_parent_id(message),
        "reply_to": await _reply_preview(parents.get(_reply_parent_id(message))),
    }
