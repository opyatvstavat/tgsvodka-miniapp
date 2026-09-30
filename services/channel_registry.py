"""Which channels exist, who reads them, and getting the service account in.

A user subscribes to a shared channel row. A channel added by @username or an
invite link waits as ``pending_join`` until ``join_worker`` connects it, one at
a time, because the account gets FloodWait-ed or banned for going fast.

A public channel is not joined: ``poll_worker`` reads it with GetHistory on an
adaptive schedule (mode ``poll``), so it takes none of the account's 500
channel slots. Only private channels, reachable by invite alone, are joined;
their posts arrive through the live handler in ``bot.py`` (mode ``live``).
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import Channel as TLChannel
from telethon.tl.types import ChatInvite, ChatInviteAlready, ChatInvitePeek, InputPeerChannel
from telethon.utils import get_peer_id, resolve_id

from config import (
    FREE_CHANNEL_LIMIT,
    JOIN_INTERVAL,
    LEAVE_AFTER_DAYS,
    MAX_POLLED_CHANNELS,
    MAX_SERVICE_CHANNELS,
    OWNER_USER_ID,
    POLL_GAP,
    POLL_MAX_SECONDS,
    POLL_MIN_SECONDS,
    POLL_START_SECONDS,
    RESOLVE_INTERVAL,
)
from db.database import SessionLocal
from db.ingest import save_direct_post
from db.models import Channel, ChannelMode, ChannelStatus, SyncSource
from db.repository import ChannelRepository
from services.channel_filters import excluded_feed_usernames, ignored_source_usernames

logger = logging.getLogger(__name__)

USERNAME = r"[A-Za-z][A-Za-z0-9_]{3,31}"
INVITE_LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?:\+|joinchat/)([A-Za-z0-9_-]{8,64})", re.I)
PUBLIC_LINK = re.compile(rf"(?:https?://)?(?:t|telegram)\.me/(?:s/)?({USERNAME})(?:/\d+)?/?(?:\?.*)?$", re.I)
RESOLVE_LINK = re.compile(rf"tg://resolve\?domain=({USERNAME})", re.I)
BARE_USERNAME = re.compile(rf"@?({USERNAME})$")
# t.me paths that are not channels.
RESERVED_PATHS = {"joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "iv", "login", "setlanguage", "boost"}

INITIAL_POSTS = 20
MAX_PENDING_PER_USER = 5
CATCH_UP_LIMIT = 100
# Opening the feed re-polls the user's channels unless polled this recently.
REFRESH_STALE = timedelta(minutes=3)

# Set to cut the poll worker's sleep short: a feed was opened or a channel came back.
_poll_wakeup = asyncio.Event()


class ChannelError(Exception):
    """A reason to show the user as is."""


@dataclass(frozen=True)
class ChannelRef:
    username: str | None = None
    invite_hash: str | None = None


def parse_channel_ref(text: str | None) -> ChannelRef | None:
    value = (text or "").strip()
    if not value:
        return None
    if match := INVITE_LINK.search(value):
        return ChannelRef(invite_hash=match.group(1))
    for pattern in (PUBLIC_LINK, RESOLVE_LINK):
        if (match := pattern.search(value)) and match.group(1).lower() not in RESERVED_PATHS:
            return ChannelRef(username=match.group(1))
    if match := BARE_USERNAME.match(value):
        return ChannelRef(username=match.group(1))
    return None


def find_channel_refs(text: str | None) -> list[ChannelRef]:
    """Channel links in free text (a bot message), in order, without repeats."""
    refs: list[ChannelRef] = []
    for token in (text or "").split():
        ref = parse_channel_ref(token if "t.me" in token or token.startswith(("@", "tg://")) else "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def serialize_channel(channel: Channel) -> dict:
    link = None
    if channel.username:
        link = f"https://t.me/{channel.username}"
    elif channel.invite_hash:
        link = f"https://t.me/+{channel.invite_hash}"
    return {
        "id": channel.id,
        "title": channel.title or (f"@{channel.username}" if channel.username else "Приватный канал"),
        "username": channel.username,
        "status": channel.status,
        "is_private": channel.is_private,
        "last_error": channel.last_error,
        "link": link,
    }


def _is_blocked_username(username: str | None) -> bool:
    return bool(username) and username.lower() in (excluded_feed_usernames() | ignored_source_usernames())


async def _check_limits(
    repo: ChannelRepository,
    user_id: int,
    *,
    is_owner: bool,
    new_channel: bool,
    from_catalog: bool,
    private: bool,
) -> None:
    if not is_owner and await repo.count_for_user(user_id) >= FREE_CHANNEL_LIMIT:
        raise ChannelError(f"Можно добавить до {FREE_CHANNEL_LIMIT} каналов. Удали ненужные, чтобы добавить новый.")
    if not new_channel:
        return
    # A private channel has to be joined and takes an account slot; a public
    # one is polled and only counts against the polling budget.
    if private:
        if await repo.count_service_slots() >= MAX_SERVICE_CHANNELS:
            raise ChannelError("Сейчас нельзя подключить приватный канал: кончились места. Попробуй позже.")
    elif await repo.count_polled() >= MAX_POLLED_CHANNELS:
        raise ChannelError("Сейчас нельзя подключить новый канал: кончились места. Попробуй позже.")
    # The pending cap stops one user from flooding the queue with junk names;
    # catalog channels are ours.
    if not is_owner and not from_catalog:
        pending = [
            channel
            for channel, _sub in await repo.list_for_user(user_id)
            if channel.status == ChannelStatus.PENDING_JOIN.value and channel.added_by == user_id
        ]
        if len(pending) >= MAX_PENDING_PER_USER:
            raise ChannelError("Подожди, пока подключатся уже добавленные каналы.")


async def add_subscription(
    user_id: int,
    ref: ChannelRef,
    *,
    is_owner: bool = False,
    tg_id: int | None = None,
    title: str | None = None,
    from_catalog: bool = False,
) -> dict:
    """Subscribe the user; queue the channel for joining if it is new.

    Returns ``{"result": "subscribed" | "queued" | "already", "channel": {...}}``.
    """
    if _is_blocked_username(ref.username):
        raise ChannelError("Этот канал нельзя добавить.")

    async with SessionLocal() as session:
        repo = ChannelRepository(session)
        channel = None
        if tg_id is not None:
            channel = await repo.get_by_tg_id(tg_id)
        if channel is None and ref.username:
            channel = await repo.get_by_username(ref.username)
        if channel is None and ref.invite_hash:
            channel = await repo.get_by_invite(ref.invite_hash)

        if channel is not None and await repo.is_subscribed(user_id, channel.id):
            return {"result": "already", "channel": serialize_channel(channel)}

        retry = channel is not None and channel.status in {ChannelStatus.FAILED.value, ChannelStatus.LEFT.value}
        private = ref.username is None and (channel is None or not channel.username)
        await _check_limits(
            repo,
            user_id,
            is_owner=is_owner,
            new_channel=channel is None or retry,
            from_catalog=from_catalog,
            private=private,
        )

        if channel is None:
            channel = await repo.create(
                tg_id=tg_id,
                username=ref.username,
                invite_hash=ref.invite_hash,
                title=title,
                is_private=ref.username is None,
                status=ChannelStatus.PENDING_JOIN.value,
                added_by=user_id,
            )
        elif retry:
            # A failed join may have been temporary, and a left channel gets
            # rejoined: the new subscriber is reason enough to try again.
            channel.status = ChannelStatus.PENDING_JOIN.value
            channel.last_error = None
            channel.added_by = user_id
            if ref.invite_hash:
                channel.invite_hash = ref.invite_hash
            await repo.save(channel)
        elif ref.invite_hash and channel.status == ChannelStatus.PENDING_JOIN.value and not channel.invite_hash:
            channel.invite_hash = ref.invite_hash
            await repo.save(channel)

        # A polled channel nobody read for a while is stale: fetch it now.
        wake = channel.mode == ChannelMode.POLL.value and channel.status == ChannelStatus.ACTIVE.value
        if wake:
            channel.next_poll_at = datetime.now(timezone.utc)
        await repo.subscribe(user_id, channel)
        if wake:
            _poll_wakeup.set()
        result = "subscribed" if channel.status == ChannelStatus.ACTIVE.value else "queued"
        return {"result": result, "channel": serialize_channel(channel)}


async def remove_subscription(user_id: int, channel_id: int) -> bool:
    async with SessionLocal() as session:
        return await ChannelRepository(session).unsubscribe(user_id, channel_id)


async def list_user_channels(user_id: int) -> list[dict]:
    async with SessionLocal() as session:
        rows = await ChannelRepository(session).list_for_user(user_id)
    return [serialize_channel(channel) for channel, _sub in rows]


async def ensure_channel_for_chat(chat) -> Channel | None:
    """The channel row for a live post; ``None`` if its posts are not wanted.

    A post from an unknown channel means the owner joined it by hand in
    Telegram, so it is registered as theirs.
    """
    tg_id = get_peer_id(chat)
    username = getattr(chat, "username", None)
    async with SessionLocal() as session:
        repo = ChannelRepository(session)
        channel = await repo.get_by_tg_id(tg_id)
        if channel is None and username:
            channel = await repo.get_by_username(username)
        if channel is None:
            channel = await repo.create(
                tg_id=tg_id,
                access_hash=getattr(chat, "access_hash", None),
                username=username,
                title=getattr(chat, "title", None),
                is_private=username is None,
                status=ChannelStatus.ACTIVE.value,
            )
            if OWNER_USER_ID:
                await repo.subscribe(OWNER_USER_ID, channel)
            logger.info("Registered channel %s (%s) for the owner", channel.title, tg_id)
            return channel
        if channel.status == ChannelStatus.LEFT.value:
            return None
        _apply_chat(channel, chat)
        # A live event means the account is in the channel (the owner joined it
        # by hand), so polling it would only duplicate the work.
        channel.mode = ChannelMode.LIVE.value
        if channel.status != ChannelStatus.ACTIVE.value:
            channel.status = ChannelStatus.ACTIVE.value
            channel.last_error = None
        await repo.save(channel)
        return channel


def _apply_chat(channel: Channel, chat) -> None:
    channel.tg_id = get_peer_id(chat)
    channel.access_hash = getattr(chat, "access_hash", None) or channel.access_hash
    channel.title = getattr(chat, "title", None) or channel.title
    channel.username = getattr(chat, "username", None) or channel.username
    channel.is_private = not getattr(chat, "username", None)


def input_peer(channel: Channel):
    """A peer for the channel that needs no request to resolve."""
    if channel.access_hash:
        bare_id, _peer_type = resolve_id(channel.tg_id)
        return InputPeerChannel(bare_id, channel.access_hash)
    return channel.tg_id


async def known_channel_peers() -> list[InputPeerChannel]:
    """Input peers of every channel with a stored access hash, for the session cache."""
    async with SessionLocal() as session:
        channels = await ChannelRepository(session).channels_with_access_hash()
    return [input_peer(channel) for channel in channels]


def _group_batches(messages) -> list[list]:
    """Split messages into posts (albums together), oldest first."""
    batches: dict[object, list] = {}
    for message in sorted(messages, key=lambda m: m.id):
        key = message.grouped_id or ("single", message.id)
        batches.setdefault(key, []).append(message)
    return list(batches.values())


async def _save_recent(client, chat, *, min_id: int = 0, limit: int = INITIAL_POSTS) -> int:
    messages = [
        message
        async for message in client.iter_messages(chat, limit=limit, min_id=min_id)
        if message and (message.message or message.media)
    ]
    saved = 0
    for batch in _group_batches(messages):
        if await save_direct_post(batch, chat, sync_source=SyncSource.BACKFILL.value):
            saved += 1
    return saved


async def _enter_public(client, channel: Channel):
    """Resolve a public channel without joining it: it will be polled."""
    try:
        # One ResolveUsername per channel, ever: the access hash is stored.
        peer = await client.get_input_entity(channel.username)
    except (ValueError, UsernameInvalidError, UsernameNotOccupiedError) as exc:
        raise ChannelError("Канал не найден") from exc
    if not isinstance(peer, InputPeerChannel):
        raise ChannelError("Это не канал")
    chat = await client.get_entity(peer)
    if not getattr(chat, "broadcast", False):
        raise ChannelError("Это группа, а не канал")
    return chat, False


async def _enter_private(client, channel: Channel):
    try:
        invite = await client(CheckChatInviteRequest(channel.invite_hash))
    except (InviteHashExpiredError, InviteHashInvalidError) as exc:
        raise ChannelError("Ссылка-приглашение недействительна") from exc
    if isinstance(invite, ChatInviteAlready):
        return invite.chat, False
    if isinstance(invite, ChatInvite):
        if not invite.broadcast:
            raise ChannelError("Это группа, а не канал")
    elif isinstance(invite, ChatInvitePeek):
        if not getattr(invite.chat, "broadcast", False):
            raise ChannelError("Это группа, а не канал")
    try:
        updates = await client(ImportChatInviteRequest(channel.invite_hash))
    except UserAlreadyParticipantError:
        invite = await client(CheckChatInviteRequest(channel.invite_hash))
        return invite.chat, False
    except InviteRequestSentError as exc:
        raise ChannelError("Вход по заявке: админ канала должен её одобрить, потом добавь канал снова") from exc
    chat = next((c for c in updates.chats if isinstance(c, TLChannel)), None)
    if chat is None:
        raise ChannelError("Не удалось войти в канал")
    return chat, True


async def join_next(client) -> str | None:
    """Connect the oldest queued channel.

    Returns ``"joined"`` when the account entered a channel, ``"resolved"``
    for anything cheaper, and ``None`` when the queue is empty.
    """
    async with SessionLocal() as session:
        channel = await ChannelRepository(session).next_pending_join()
    if channel is None:
        return None

    try:
        if channel.invite_hash and not channel.username:
            chat, joined = await _enter_private(client, channel)
        elif channel.username:
            chat, joined = await _enter_public(client, channel)
        else:
            raise ChannelError("Нужна ссылка-приглашение: канал приватный")
    except ChannelError as exc:
        await _fail(channel.id, str(exc))
        return "resolved"
    except ChannelPrivateError:
        await _fail(channel.id, "Канал закрыт или нас забанили в нём")
        return "resolved"
    except ChannelsTooMuchError:
        # Not this channel's fault: keep it queued and stop joining for a while.
        logger.error("Service account is in too many channels")
        raise
    except FloodWaitError:
        raise
    except Exception as exc:
        logger.exception("Join failed for channel %s", channel.id)
        await _fail(channel.id, "Не удалось подключить канал")
        return "resolved"

    tg_id = get_peer_id(chat)
    # The account may already be in a public channel (the owner's own one).
    live = joined or not getattr(chat, "left", True)
    async with SessionLocal() as session:
        repo = ChannelRepository(session)
        row = await repo.get(channel.id)
        if row is None:
            return "resolved"
        existing = await repo.get_by_tg_id(tg_id)
        if existing is not None and existing.id != row.id:
            # Added once by username and once by invite or forward.
            await repo.merge_into(row, existing)
            row = existing
        _apply_chat(row, chat)
        row.status = ChannelStatus.ACTIVE.value
        row.last_error = None
        row.joined_by_service = row.joined_by_service or joined
        if not live:
            row.mode = ChannelMode.POLL.value
            row.poll_interval = row.poll_interval or POLL_START_SECONDS
            row.next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=row.poll_interval)
        else:
            row.mode = ChannelMode.LIVE.value
        await repo.save(row)
    logger.info(
        "%s channel %s (%s)", "Joined" if joined else "Connected", getattr(chat, "title", None), tg_id
    )

    try:
        saved = await _save_recent(client, chat)
        logger.info("Loaded %s recent posts of %s", saved, tg_id)
    except FloodWaitError:
        raise
    except Exception as exc:
        logger.warning("Could not load recent posts of %s: %s", tg_id, exc)
    return "joined" if joined else "resolved"


async def _fail(channel_id: int, reason: str) -> None:
    async with SessionLocal() as session:
        repo = ChannelRepository(session)
        channel = await repo.get(channel_id)
        if channel is None:
            return
        channel.status = ChannelStatus.FAILED.value
        channel.last_error = reason[:255]
        await repo.save(channel)
    logger.info("Channel %s failed: %s", channel_id, reason)


async def join_worker(client) -> None:
    """Drain the join queue at a pace Telegram tolerates."""
    while True:
        try:
            done = await join_next(client)
            if done == "joined":
                await asyncio.sleep(JOIN_INTERVAL)
            else:
                await asyncio.sleep(RESOLVE_INTERVAL if done else 15)
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            logger.warning("Join FloodWait: sleeping %ss", exc.seconds)
            await asyncio.sleep(exc.seconds + 5)
        except ChannelsTooMuchError:
            await asyncio.sleep(3600)
        except Exception as exc:
            logger.error("Join worker error: %s", exc)
            await asyncio.sleep(60)


async def catch_up_channels(client) -> int:
    """Fetch posts published while the process was down.

    Live events carry new posts; this is the safety net that replaces the
    summary-channel backfill. A channel without a known last message only gets
    its baseline set: its older posts are already in the queue as summary copies.
    """
    async with SessionLocal() as session:
        channels = await ChannelRepository(session).active_channels()
    added = 0
    for channel in channels:
        try:
            peer = input_peer(channel)
            if channel.last_msg_id is None:
                latest = await client.get_messages(peer, limit=1)
                if latest:
                    async with SessionLocal() as session:
                        repo = ChannelRepository(session)
                        row = await repo.get(channel.id)
                        if row and row.last_msg_id is None:
                            row.last_msg_id = latest[0].id
                            await repo.save(row)
            else:
                chat = await client.get_entity(peer)
                added += await _save_recent(client, chat, min_id=channel.last_msg_id, limit=CATCH_UP_LIMIT)
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            logger.warning("Catch-up FloodWait: sleeping %ss", exc.seconds)
            await asyncio.sleep(exc.seconds + 5)
        except Exception as exc:
            logger.warning("Catch-up failed for channel %s: %s", channel.tg_id, exc)
        await asyncio.sleep(1)
    logger.info("Catch-up complete: %s channels, %s new posts", len(channels), added)
    return added


async def leave_orphans(client) -> int:
    """Free account slots held by channels nobody reads any more."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LEAVE_AFTER_DAYS)
    async with SessionLocal() as session:
        channels = await ChannelRepository(session).channels_to_leave(cutoff)
    left = 0
    for channel in channels:
        try:
            await client(LeaveChannelRequest(input_peer(channel)))
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 5)
            continue
        except Exception as exc:
            logger.warning("Could not leave channel %s: %s", channel.tg_id, exc)
            continue
        async with SessionLocal() as session:
            repo = ChannelRepository(session)
            row = await repo.get(channel.id)
            if row:
                row.status = ChannelStatus.LEFT.value
                await repo.save(row)
        left += 1
        await asyncio.sleep(5)
    if left:
        logger.info("Left %s channels without subscribers", left)
    return left


async def queue_catalog_channels(usernames: list[str]) -> int:
    """Put catalog channels in the join queue ahead of the first subscriber."""
    queued = 0
    async with SessionLocal() as session:
        repo = ChannelRepository(session)
        for username in usernames:
            if await repo.get_by_username(username) is not None:
                continue
            if await repo.count_polled() >= MAX_POLLED_CHANNELS:
                break
            await repo.create(
                username=username,
                is_private=False,
                status=ChannelStatus.PENDING_JOIN.value,
            )
            queued += 1
    return queued


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands timestamps back naive; they are stored in UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def _poll_channel(client, channel: Channel) -> int:
    """Fetch posts newer than the last seen one. Returns how many were saved."""
    peer = input_peer(channel)
    if channel.last_msg_id is None:
        # No baseline yet: remember where the channel is, save nothing.
        latest = await client.get_messages(peer, limit=1)
        if latest:
            async with SessionLocal() as session:
                repo = ChannelRepository(session)
                row = await repo.get(channel.id)
                if row and row.last_msg_id is None:
                    row.last_msg_id = latest[0].id
                    await repo.save(row)
        return 0
    chat = await client.get_entity(peer)
    return await _save_recent(client, chat, min_id=channel.last_msg_id, limit=CATCH_UP_LIMIT)


async def _schedule_next_poll(channel_id: int, saved: int | None) -> None:
    """Poll a posting channel more often and a quiet one less.

    ``saved=None`` means the poll failed: try again at the slowest pace.
    """
    async with SessionLocal() as session:
        repo = ChannelRepository(session)
        row = await repo.get(channel_id)
        if row is None:
            return
        interval = row.poll_interval or POLL_START_SECONDS
        if saved is None:
            interval = POLL_MAX_SECONDS
        elif saved:
            interval = max(POLL_MIN_SECONDS, interval // 2)
        else:
            interval = min(POLL_MAX_SECONDS, int(interval * 1.5))
        now = datetime.now(timezone.utc)
        row.poll_interval = interval
        row.last_polled_at = now
        row.next_poll_at = now + timedelta(seconds=interval)
        await repo.save(row)


async def poll_next(client) -> float:
    """Poll the channel that is due soonest. Returns seconds until the next one is due."""
    async with SessionLocal() as session:
        channel = await ChannelRepository(session).next_channel_to_poll()
    if channel is None:
        return float(POLL_MAX_SECONDS)
    due = _aware(channel.next_poll_at)
    wait = (due - datetime.now(timezone.utc)).total_seconds() if due else 0
    if wait > 0:
        return wait

    try:
        saved = await _poll_channel(client, channel)
    except FloodWaitError:
        raise
    except ChannelPrivateError:
        await _fail(channel.id, "Канал стал закрытым или нас забанили в нём")
        return 0
    except Exception as exc:
        logger.warning("Poll failed for channel %s: %s", channel.tg_id, exc)
        await _schedule_next_poll(channel.id, None)
        return 0
    await _schedule_next_poll(channel.id, saved)
    if saved:
        logger.info("Polled %s new posts of %s", saved, channel.tg_id)
    return 0


async def poll_worker(client) -> None:
    """Read public channels the account has not joined, one request at a time."""
    while True:
        try:
            # Cleared before looking at the queue, so a refresh requested while
            # polling still ends the wait below.
            _poll_wakeup.clear()
            wait = await poll_next(client)
            if wait > 0:
                try:
                    await asyncio.wait_for(_poll_wakeup.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(POLL_GAP)
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            logger.warning("Poll FloodWait: sleeping %ss", exc.seconds)
            await asyncio.sleep(exc.seconds + 5)
        except Exception as exc:
            logger.error("Poll worker error: %s", exc)
            await asyncio.sleep(60)


async def request_refresh(user_id: int) -> None:
    """The user opened the feed: poll their channels now unless polled just now."""
    try:
        async with SessionLocal() as session:
            due = await ChannelRepository(session).poll_now(
                user_id, datetime.now(timezone.utc) - REFRESH_STALE
            )
    except Exception as exc:
        logger.warning("Feed refresh request failed: %s", exc)
        return
    if due:
        _poll_wakeup.set()
