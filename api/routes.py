import asyncio
import logging
import os
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.bot_client import BotClient
from config import FREE_CHANNEL_LIMIT
from db.database import get_session
from db.models import PostStatus
from db.repository import ChannelRepository, PostRepository, UserRepository
from services.catalog import catalog_payload, get_pack
from services.channel_registry import (
    ChannelError,
    ChannelRef,
    add_subscription,
    list_user_channels,
    parse_channel_ref,
    remove_subscription,
    request_refresh,
)
from services.embeds import extract_youtube_embeds
from services.channel_social import (
    list_comments,
    fetch_original_post,
    send_comment,
    serve_comment_media,
    serve_channel_avatar,
    serve_custom_emoji,
    serve_user_avatar,
)
from services.media_delivery import serve_media
from services.message_parser import (
    CONTENT_VERSION,
    _body_matches_channel,
    _looks_like_summary_header,
    extract_channel_from_body_html,
    extract_channel_header_label,
    format_post_text,
    parse_channel_info,
    split_summary_channel_and_body,
)
from services.post_share import build_share_result, verify_media_signature
from services.publish_highlights import publish_post_to_highlights

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)
# Fire-and-forget tasks, held so the loop does not drop them mid-flight.
_background: set[asyncio.Task] = set()

# The feed requests stats on every like/skip, and each one costs a full index
# scan of ~95k rows. The client already fires this request concurrently with the
# decision it is making, so the numbers it renders are pre-decision regardless —
# a few seconds of reuse is not visible, and it keeps swiping responsive.
STATS_TTL_SECONDS = 5.0
_stats_cache: tuple[float, dict] | None = None


def _cached_stats() -> dict | None:
    if _stats_cache is None:
        return None
    cached_at, payload = _stats_cache
    if time.monotonic() - cached_at > STATS_TTL_SECONDS:
        return None
    return payload


def _store_stats(payload: dict | None) -> None:
    global _stats_cache
    _stats_cache = (time.monotonic(), payload) if payload is not None else None


class PostOut(BaseModel):
    id: uuid.UUID
    channel_title: str | None
    channel_username: str | None
    text: str | None
    media_type: str
    media_json: dict | None
    summary_chat_id: int
    summary_msg_id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class StatsOut(BaseModel):
    pending: int
    seen: int
    liked: int
    skipped: int
    channel_messages_total: int
    messages_in_db: int
    messages_untracked: int
    messages_added_last_run: int
    last_sync_at: datetime | None
    build: str | None


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    reply_to: int | None = None


class SeenIn(BaseModel):
    dwell_ms: int | None = Field(default=None, ge=0, le=24 * 3600 * 1000)


class ChannelIn(BaseModel):
    ref: str = Field(min_length=1, max_length=256)


def _encode_cursor(post) -> str | None:
    stamp = post.published_at or post.created_at
    return f"{stamp.isoformat()}|{post.id}" if stamp else None


def _decode_cursor(value: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not value:
        return None
    stamp, _, post_id = value.partition("|")
    try:
        return datetime.fromisoformat(stamp), uuid.UUID(post_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc


async def _visible_post(session: AsyncSession, post_id: uuid.UUID, user: dict):
    """The post, if this user reads its channel; 404 otherwise, so ids do not leak."""
    repo = PostRepository(session)
    post = await repo.get_by_id(post_id)
    if not post or not await repo.user_can_see(user["id"], post, is_owner=user.get("is_owner", False)):
        raise HTTPException(status_code=404, detail="Post not found")
    return post


def _require_owner(user: dict) -> None:
    if not user.get("is_owner"):
        raise HTTPException(status_code=403, detail="Доступно только владельцу")


def _summary_link(post) -> str | None:
    chat_id = post.summary_chat_id
    if chat_id < 0:
        internal_id = str(chat_id).replace("-100", "", 1)
        return f"https://t.me/c/{internal_id}/{post.summary_msg_id}"
    return None


def _normalize_media_item(item: dict) -> dict:
    normalized = dict(item)
    if normalized.get("type") == "document":
        normalized["type"] = "voice"
    return normalized


def _media_items(post) -> list[dict]:
    media_json = post.media_json or {}
    items = media_json.get("items") or []
    if items:
        return [_normalize_media_item(item) for item in items]

    if post.media_type in {"photo", "video", "document", "voice", "audio"}:
        item_type = post.media_type
        if item_type == "document":
            item_type = "voice"
        return [{"type": item_type, "summary_msg_id": post.summary_msg_id}]
    return []


def _usable_saved_content(post) -> str | None:
    """Use a clean stored body while an old card is being restored.

    Some historical rows contain literal HTML attributes from a damaged
    forwarded message (for example ``&quot; target=&quot;``).  Showing those is
    worse than briefly using the summary fallback, so let the original-post
    request replace them instead.
    """
    saved = post.text or ""
    if not saved or "&quot;" in saved or "&gt;" in saved or "&lt;" in saved:
        return None
    return format_post_text(saved)


def _serialize_post(post) -> dict:
    data = PostOut.model_validate(post).model_dump()
    data["summary_link"] = _summary_link(post)
    media_json = dict(post.media_json or {})
    data["source_link"] = media_json.get("source_link")

    if not data.get("channel_title") and media_json.get("channel_title"):
        data["channel_title"] = media_json["channel_title"]

    if not data.get("channel_title") and media_json.get("raw_header"):
        title, username, link = parse_channel_info(media_json["raw_header"])
        data["channel_title"] = title
        data["channel_username"] = data.get("channel_username") or username
        data["source_link"] = data["source_link"] or link

    raw_header = media_json.get("raw_header") or ""

    parsed_title = parsed_username = parsed_link = None
    body_raw = None

    if raw_header:
        parsed_title, parsed_username, parsed_link, body_raw = split_summary_channel_and_body(raw_header)

    if body_raw is None and post.text:
        t2, u2, l2, b2 = split_summary_channel_and_body(post.text)
        parsed_title = parsed_title or t2
        parsed_username = parsed_username or u2
        parsed_link = parsed_link or l2
        if b2:
            body_raw = b2
        elif post.text and not _looks_like_summary_header(post.text.strip().splitlines()[0]):
            body_raw = post.text

    if not parsed_title and raw_header:
        parsed_title, parsed_username, parsed_link = parse_channel_info(raw_header)

    if not parsed_title and post.text:
        parsed_title, parsed_username, parsed_link = extract_channel_from_body_html(post.text)

    data["channel_title"] = data.get("channel_title") or parsed_title
    data["channel_username"] = data.get("channel_username") or parsed_username
    data["source_link"] = data["source_link"] or parsed_link

    if body_raw and _body_matches_channel(
        body_raw, data.get("channel_title"), data.get("channel_username")
    ):
        body_raw = None

    if raw_header:
        label_title, label_html = extract_channel_header_label(raw_header)
        if label_title:
            data["channel_title"] = label_title
        data["channel_label_html"] = label_html

    # Content stored by an older formatter is broken beyond repair; the client
    # asks /content for the original post as soon as the card is shown.
    content_is_current = media_json.get("content_version") == CONTENT_VERSION
    content_html = (media_json.get("content_html") or _usable_saved_content(post)) if content_is_current else None
    data["media_items"] = _media_items(post)
    data["text"] = content_html or format_post_text(body_raw)
    data["embeds"] = extract_youtube_embeds(content_html or body_raw or post.text)
    return data


@router.get("/next")
async def get_next_post(
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    repo = PostRepository(session)
    posts = await repo.get_user_feed_page(user["id"], is_owner=user.get("is_owner", False), limit=1)
    if not posts:
        return Response(status_code=204)
    return _serialize_post(posts[0])


@router.get("/queue")
async def get_post_queue(
    limit: int = 8,
    before: str | None = None,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if before is None:
        # Opening the feed pulls fresh posts of polled channels; this page does
        # not wait for them, the next one or the next open shows them.
        task = asyncio.create_task(request_refresh(user["id"]))
        _background.add(task)
        task.add_done_callback(_background.discard)
    repo = PostRepository(session)
    posts = await repo.get_user_feed_page(
        user["id"],
        is_owner=user.get("is_owner", False),
        limit=max(1, min(limit, 20)),
        cursor=_decode_cursor(before),
    )
    return {
        "items": [_serialize_post(post) for post in posts],
        "next_cursor": _encode_cursor(posts[-1]) if posts else None,
    }


@router.get("/me")
async def get_me(
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    stored = await UserRepository(session).get(user["id"])
    channels = await ChannelRepository(session).count_for_user(user["id"])
    is_owner = user.get("is_owner", False)
    return {
        "id": user["id"],
        "is_owner": is_owner,
        "plan": stored.plan if stored else "free",
        "channels_count": channels,
        "channel_limit": None if is_owner else FREE_CHANNEL_LIMIT,
        # Comments are sent from the service account, which only its owner may speak for.
        "can_comment": is_owner,
    }


@router.get("/channels")
async def get_channels(user: dict = Depends(get_current_user)):
    return {"items": await list_user_channels(user["id"])}


async def _subscribe(user: dict, ref: ChannelRef) -> dict:
    try:
        return await add_subscription(user["id"], ref, is_owner=user.get("is_owner", False))
    except ChannelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/channels")
async def create_channel(payload: ChannelIn, user: dict = Depends(get_current_user)):
    ref = parse_channel_ref(payload.ref)
    if ref is None:
        raise HTTPException(status_code=422, detail="Не похоже на канал. Пришли @username или ссылку t.me/…")
    return await _subscribe(user, ref)


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: int, user: dict = Depends(get_current_user)):
    if not await remove_subscription(user["id"], channel_id):
        raise HTTPException(status_code=404, detail="Канал не найден")
    return {"status": "removed", "id": channel_id}


@router.get("/catalog")
async def get_catalog(_: dict = Depends(get_current_user)):
    return {"items": catalog_payload()}


@router.post("/catalog/{pack_id}/subscribe")
async def subscribe_pack(pack_id: str, user: dict = Depends(get_current_user)):
    pack = get_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="Подборка не найдена")
    results = []
    error = None
    for username in pack["channels"]:
        try:
            results.append(
                await add_subscription(
                    user["id"], ChannelRef(username=username), is_owner=user.get("is_owner", False), from_catalog=True
                )
            )
        except ChannelError as exc:
            # The limit stops the pack part-way; keep what was added.
            error = str(exc)
            break
    return {"items": results, "error": error}


async def _serve_post_media(session: AsyncSession, post, index: int, preview: bool, range_header: str | None):
    items = _media_items(post)
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="Media not found")

    message_id = items[index].get("summary_msg_id", post.summary_msg_id)
    try:
        return await serve_media(
            post.summary_chat_id,
            message_id,
            preview=preview,
            range_header=range_header,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/posts/{post_id}/media/{index}")
async def get_post_media(
    request: Request,
    post_id: uuid.UUID,
    index: int,
    preview: bool = False,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    post = await _visible_post(session, post_id, user)
    return await _serve_post_media(session, post, index, preview, request.headers.get("range"))


@router.get("/share-media/{post_id}/{index}")
async def get_shared_media(
    request: Request,
    post_id: uuid.UUID,
    index: int,
    exp: int,
    sig: str,
    preview: bool = False,
    session: AsyncSession = Depends(get_session),
):
    # Telegram downloads the media of a shared post from here, without initData.
    if not verify_media_signature(post_id, index, preview, exp, sig):
        raise HTTPException(status_code=403, detail="Invalid or expired link")
    post = await PostRepository(session).get_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return await _serve_post_media(session, post, index, preview, request.headers.get("range"))


@router.post("/posts/{post_id}/share")
async def share_post(
    post_id: uuid.UUID,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    post = await _visible_post(session, post_id, user)
    data = _serialize_post(post)
    params = {"title": data.get("channel_title"), "link": data.get("source_link"), "items": data["media_items"]}
    bot = BotClient()
    result = await build_share_result(post, **params)
    try:
        prepared_id = await bot.save_prepared_inline_message(user["id"], result)
    except RuntimeError as exc:
        if result.get("type") != "video":
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # Telegram could not take the video by URL; its preview frame still carries the post.
        logger.warning("Shared video rejected for %s: %s", post_id, exc)
        result = await build_share_result(post, **params, allow_video=False)
        try:
            prepared_id = await bot.save_prepared_inline_message(user["id"], result)
        except RuntimeError as retry_exc:
            raise HTTPException(status_code=502, detail=str(retry_exc)) from retry_exc
    return {"prepared_id": prepared_id}


@router.get("/posts/{post_id}/avatar")
async def get_post_avatar(
    post_id: uuid.UUID,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    post = await _visible_post(session, post_id, user)
    return await serve_channel_avatar(post)


@router.get("/users/{user_id}/avatar")
async def get_user_avatar(
    user_id: int,
    _: dict = Depends(get_current_user),
):
    return await serve_user_avatar(user_id)


@router.get("/custom-emoji/{document_id}")
async def get_custom_emoji(
    document_id: int,
    _: dict = Depends(get_current_user),
):
    return await serve_custom_emoji(document_id)


@router.get("/posts/{post_id}/content")
async def get_post_content(
    post_id: uuid.UUID,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    repo = PostRepository(session)
    post = await _visible_post(session, post_id, user)
    media_json = dict(post.media_json or {})
    cached = media_json.get("content_html") if media_json.get("content_version") == CONTENT_VERSION else None
    try:
        # Fetched on every view: one GetMessages call also brings the live
        # comment count, and a post edited in its channel shows its new text.
        text, comments_count, reactions, forwards = await fetch_original_post(post)
    except HTTPException:
        if not cached:
            raise
        # A deleted or private original keeps its last good copy.
        return {"id": str(post.id), "text": cached, "embeds": extract_youtube_embeds(cached), "comments_count": None, "reactions": None, "forwards": None}
    if (
        text != media_json.get("content_html")
        or media_json.get("content_version") != CONTENT_VERSION
        or media_json.get("content_source") != "original"
    ):
        media_json.update(content_html=text, content_version=CONTENT_VERSION, content_source="original")
        await repo.update_post_content(post, text=text, media_json=media_json)
    return {"id": str(post.id), "text": text, "embeds": extract_youtube_embeds(text), "comments_count": comments_count, "reactions": reactions, "forwards": forwards}


@router.get("/posts/{post_id}/comments")
async def get_post_comments(
    post_id: uuid.UUID,
    after: int | None = None,
    limit: int = 20,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    post = await _visible_post(session, post_id, user)
    return await list_comments(post, after=after, limit=max(1, min(limit, 20)))


@router.get("/posts/{post_id}/comments/{comment_id}/media")
async def get_comment_media(
    request: Request,
    post_id: uuid.UUID,
    comment_id: int,
    preview: bool = False,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    post = await _visible_post(session, post_id, user)
    return await serve_comment_media(post, comment_id, preview=preview, range_header=request.headers.get("range"))


@router.post("/posts/{post_id}/comments")
async def create_post_comment(
    post_id: uuid.UUID,
    payload: CommentIn,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_owner(user)
    post = await _visible_post(session, post_id, user)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Комментарий не может быть пустым")
    return await send_comment(post, text=text, reply_to=payload.reply_to)


@router.post("/posts/{post_id}/like")
async def like_post(
    post_id: uuid.UUID,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    repo = PostRepository(session)
    post = await _visible_post(session, post_id, user)
    state = await repo.get_state(user["id"], post.id)
    if state and state.status in {PostStatus.LIKED.value, PostStatus.SKIPPED.value}:
        raise HTTPException(status_code=409, detail="Post already processed")

    if user.get("is_owner"):
        # The highlights channel is the owner's; other users' likes stay a signal.
        try:
            await publish_post_to_highlights(post)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    await repo.set_state(user["id"], post, PostStatus.LIKED.value)
    _store_stats(None)
    return {"status": "liked", "id": str(post.id)}


@router.post("/posts/{post_id}/seen")
async def seen_post(
    post_id: uuid.UUID,
    payload: SeenIn | None = None,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Mark a displayed card as read; ``dwell_ms`` is how long it was on screen."""
    repo = PostRepository(session)
    post = await _visible_post(session, post_id, user)
    await repo.set_state(
        user["id"], post, PostStatus.SEEN.value, dwell_ms=payload.dwell_ms if payload else None
    )
    _store_stats(None)
    return {"status": "seen", "id": str(post.id)}


@router.post("/posts/{post_id}/skip")
async def skip_post(
    post_id: uuid.UUID,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    repo = PostRepository(session)
    post = await _visible_post(session, post_id, user)
    state = await repo.get_state(user["id"], post.id)
    if state and state.status != PostStatus.SEEN.value:
        raise HTTPException(status_code=409, detail="Post already processed")

    await repo.set_state(user["id"], post, PostStatus.SKIPPED.value)
    _store_stats(None)
    return {"status": "skipped", "id": str(post.id)}


@router.get("/stats", response_model=StatsOut)
async def get_stats(
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # Queue-wide counters of the service, not of one reader.
    _require_owner(user)
    cached = _cached_stats()
    if cached is not None:
        return cached

    repo = PostRepository(session)
    stats = await repo.get_stats()
    sync = await repo.get_sync_state()
    # Same number the grouped query already produced; a separate COUNT(*) here
    # was a third full scan of the table on every like/skip.
    messages_in_db = stats.pop("total", 0)
    build = (
        os.getenv("RAILWAY_GIT_COMMIT_SHA")
        or os.getenv("RAILWAY_GIT_SHA")
        or os.getenv("GIT_COMMIT_SHA")
        or os.getenv("COMMIT_SHA")
    )
    if build:
        build = build[:8]

    payload = {
        **stats,
        "channel_messages_total": sync.channel_messages_total if sync else 0,
        "messages_in_db": messages_in_db,
        "messages_untracked": sync.messages_untracked if sync else 0,
        "messages_added_last_run": sync.messages_added_last_run if sync else 0,
        "last_sync_at": sync.last_sync_at if sync else None,
        "build": build,
    }
    _store_stats(payload)
    return payload
