import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, exists, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Channel,
    ChannelMode,
    ChannelStatus,
    ChannelSyncState,
    Post,
    PostStatus,
    Subscription,
    SyncSource,
    User,
    UserPostState,
)
from services.channel_filters import excluded_feed_usernames, is_excluded_feed_channel


def _feed_source_exclusion():
    match_clauses = []
    for username in excluded_feed_usernames():
        match_clauses.append(
            or_(
                and_(Post.channel_username.isnot(None), func.lower(Post.channel_username) == username),
                and_(Post.channel_title.isnot(None), func.lower(Post.channel_title) == username),
                and_(Post.channel_title.isnot(None), Post.channel_title == f"/{username}/"),
            )
        )
    if not match_clauses:
        return None
    return not_(or_(*match_clauses))


class PostRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_post(
        self,
        *,
        summary_chat_id: int,
        summary_msg_id: int,
        source_chat_id: int | None = None,
        source_msg_id: int | None = None,
        grouped_id: int | None = None,
        channel_id: int | None = None,
        published_at: datetime | None = None,
        channel_title: str | None = None,
        channel_username: str | None = None,
        text: str | None = None,
        media_type: str = "text",
        media_json: dict | None = None,
        sync_source: str = SyncSource.LIVE.value,
    ) -> Post:
        post = Post(
            summary_chat_id=summary_chat_id,
            summary_msg_id=summary_msg_id,
            source_chat_id=source_chat_id,
            source_msg_id=source_msg_id,
            grouped_id=grouped_id,
            channel_id=channel_id,
            published_at=published_at,
            channel_title=channel_title,
            channel_username=channel_username,
            text=text,
            media_type=media_type,
            media_json=media_json,
            status=PostStatus.PENDING.value,
            sync_source=sync_source,
        )
        self.session.add(post)
        await self.session.commit()
        await self.session.refresh(post)
        return post

    async def update_post_metadata(
        self,
        post: Post,
        *,
        channel_title: str | None = None,
        channel_username: str | None = None,
        media_json: dict | None = None,
        media_type: str | None = None,
    ) -> Post:
        if channel_title:
            post.channel_title = channel_title
        if channel_username:
            post.channel_username = channel_username
        if media_json is not None:
            post.media_json = media_json
        if media_type:
            post.media_type = media_type
        await self.session.commit()
        await self.session.refresh(post)
        return post

    async def update_post_content(self, post: Post, *, text: str, media_json: dict) -> Post:
        post.text = text
        post.media_json = media_json
        await self.session.commit()
        await self.session.refresh(post)
        return post

    async def get_by_summary(self, summary_chat_id: int, summary_msg_id: int) -> Post | None:
        result = await self.session.execute(
            select(Post).where(
                Post.summary_chat_id == summary_chat_id,
                Post.summary_msg_id == summary_msg_id,
            )
        )
        return result.scalar_one_or_none()

    async def exists_by_summary(self, summary_chat_id: int, summary_msg_id: int) -> bool:
        result = await self.session.execute(
            select(Post.id).where(
                Post.summary_chat_id == summary_chat_id,
                Post.summary_msg_id == summary_msg_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def count_distinct_summary(self, summary_chat_id: int) -> int:
        result = await self.session.execute(
            select(func.count()).where(Post.summary_chat_id == summary_chat_id)
        )
        return result.scalar_one() or 0

    def _feed_query(self, before_summary_msg_id: int | None = None):
        query = select(Post).where(Post.status == PostStatus.PENDING.value)
        if before_summary_msg_id is not None:
            query = query.where(Post.summary_msg_id < before_summary_msg_id)
        exclusion = _feed_source_exclusion()
        if exclusion is not None:
            query = query.where(exclusion)
        return query.order_by(Post.summary_msg_id.desc())

    async def get_next_pending(self) -> Post | None:
        result = await self.session.execute(self._feed_query().limit(1))
        return result.scalar_one_or_none()

    async def get_feed_page(
        self, *, limit: int = 10, before_summary_msg_id: int | None = None
    ) -> list[Post]:
        limit = max(1, min(limit, 20))
        result = await self.session.execute(
            self._feed_query(before_summary_msg_id).limit(limit)
        )
        return list(result.scalars().all())

    async def get_pending_queue(self, limit: int = 10) -> list[Post]:
        """Compatibility wrapper for callers that have not moved to cursor paging."""
        return await self.get_feed_page(limit=limit)

    async def skip_excluded_pending(self) -> int:
        result = await self.session.execute(
            select(Post).where(Post.status == PostStatus.PENDING.value)
        )
        posts = list(result.scalars().all())
        skipped = 0
        for post in posts:
            if is_excluded_feed_channel(post.channel_username, post.channel_title):
                post.status = PostStatus.SKIPPED.value
                post.decided_at = datetime.now(timezone.utc)
                skipped += 1
        if skipped:
            await self.session.commit()
        return skipped

    def _subscribed(self, user_id: int):
        return exists().where(
            Subscription.user_id == user_id,
            Subscription.channel_id == Channel.id,
            Channel.tg_id == Post.channel_id,
            or_(Subscription.feed_since.is_(None), Post.published_at >= Subscription.feed_since),
        )

    def _visible(self, user_id: int, is_owner: bool):
        subscribed = self._subscribed(user_id)
        # Rows from before channels were tracked have no channel; they were
        # only ever the owner's.
        return or_(subscribed, Post.channel_id.is_(None)) if is_owner else subscribed

    async def get_user_feed_page(
        self,
        user_id: int,
        *,
        is_owner: bool = False,
        limit: int = 10,
        cursor: tuple[datetime, uuid.UUID] | None = None,
    ) -> list[Post]:
        """Unread posts of the user's channels, newest first."""
        limit = max(1, min(limit, 20))
        read = exists().where(UserPostState.user_id == user_id, UserPostState.post_id == Post.id)
        query = select(Post).where(self._visible(user_id, is_owner), not_(read))
        if cursor is not None:
            published_at, post_id = cursor
            query = query.where(
                or_(
                    Post.published_at < published_at,
                    and_(Post.published_at == published_at, Post.id < post_id),
                )
            )
        exclusion = _feed_source_exclusion()
        if exclusion is not None:
            query = query.where(exclusion)
        query = query.order_by(Post.published_at.desc(), Post.id.desc()).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def user_can_see(self, user_id: int, post: Post, *, is_owner: bool = False) -> bool:
        if is_owner:
            return True
        if post.channel_id is None:
            return False
        result = await self.session.execute(
            select(Subscription.user_id)
            .join(Channel, Channel.id == Subscription.channel_id)
            .where(Subscription.user_id == user_id, Channel.tg_id == post.channel_id)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def get_state(self, user_id: int, post_id: uuid.UUID) -> UserPostState | None:
        return await self.session.get(UserPostState, (user_id, post_id))

    async def set_state(
        self, user_id: int, post: Post, status: str, *, dwell_ms: int | None = None
    ) -> UserPostState:
        state = await self.get_state(user_id, post.id)
        if state is None:
            state = UserPostState(user_id=user_id, post_id=post.id, status=status)
            self.session.add(state)
        elif state.status != PostStatus.LIKED.value:
            # A like outranks a later "seen" of the same post.
            state.status = status
        if dwell_ms is not None:
            state.dwell_ms = max(state.dwell_ms or 0, dwell_ms)
        state.decided_at = datetime.now(timezone.utc)
        await self.session.commit()
        return state

    async def get_by_id(self, post_id: uuid.UUID) -> Post | None:
        result = await self.session.execute(select(Post).where(Post.id == post_id))
        return result.scalar_one_or_none()

    async def mark_liked(self, post: Post) -> Post:
        post.status = PostStatus.LIKED.value
        post.decided_at = datetime.now(timezone.utc)
        await self.session.commit()
        await self.session.refresh(post)
        return post

    async def mark_seen(self, post: Post) -> Post:
        if post.status == PostStatus.PENDING.value:
            post.status = PostStatus.SEEN.value
            post.decided_at = datetime.now(timezone.utc)
            await self.session.commit()
            await self.session.refresh(post)
        return post

    async def mark_skipped(self, post: Post) -> Post:
        post.status = PostStatus.SKIPPED.value
        post.decided_at = datetime.now(timezone.utc)
        await self.session.commit()
        await self.session.refresh(post)
        return post

    async def count_feed_pending(self) -> int:
        query = select(func.count()).where(Post.status == PostStatus.PENDING.value)
        exclusion = _feed_source_exclusion()
        if exclusion is not None:
            query = query.where(exclusion)
        result = await self.session.execute(query)
        return result.scalar_one() or 0

    async def get_stats(self) -> dict[str, int]:
        """Counts for the feed header.

        One grouped query answers all of them — the totals and the row count are
        derived from it, and the filtered pending count only runs its own query
        when a channel exclusion is actually configured.

        Note the counts are COUNT(*), not COUNT(id): counting the primary key
        forces a table lookup per row because id is not in the index, which cost
        seconds over ~95k rows. COUNT(*) is answered from the index alone.
        """
        result = await self.session.execute(
            select(Post.status, func.count()).group_by(Post.status)
        )
        counts = {status: count for status, count in result.all()}
        pending_total = counts.get(PostStatus.PENDING.value, 0)

        pending = pending_total
        if _feed_source_exclusion() is not None:
            pending = await self.count_feed_pending()

        return {
            "pending": pending,
            "pending_total": pending_total,
            "seen": counts.get(PostStatus.SEEN.value, 0),
            "liked": counts.get(PostStatus.LIKED.value, 0),
            "skipped": counts.get(PostStatus.SKIPPED.value, 0),
            "total": sum(counts.values()),
        }

    async def get_sync_state(self) -> ChannelSyncState | None:
        result = await self.session.execute(select(ChannelSyncState).where(ChannelSyncState.id == 1))
        return result.scalar_one_or_none()

    async def upsert_sync_state(
        self,
        *,
        summary_chat_id: int,
        channel_messages_total: int,
        messages_in_db: int,
        messages_added_last_run: int,
        messages_skipped_existing: int,
    ) -> ChannelSyncState:
        state = await self.get_sync_state()
        messages_untracked = max(channel_messages_total - messages_in_db, 0)

        if state is None:
            state = ChannelSyncState(id=1)
            self.session.add(state)

        state.summary_chat_id = summary_chat_id
        state.last_sync_at = datetime.now(timezone.utc)
        state.channel_messages_total = channel_messages_total
        state.messages_in_db = messages_in_db
        state.messages_added_last_run = messages_added_last_run
        state.messages_skipped_existing = messages_skipped_existing
        state.messages_untracked = messages_untracked

        await self.session.commit()
        await self.session.refresh(state)
        return state


class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, user_id: int, *, first_name: str | None, username: str | None) -> User:
        user = await self.session.get(User, user_id)
        if user is None:
            user = User(id=user_id, first_name=first_name, username=username)
            self.session.add(user)
        else:
            user.first_name = first_name or user.first_name
            user.username = username or user.username
        await self.session.commit()
        return user

    async def get(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)


class ChannelRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, channel_id: int) -> Channel | None:
        return await self.session.get(Channel, channel_id)

    async def get_by_tg_id(self, tg_id: int) -> Channel | None:
        result = await self.session.execute(select(Channel).where(Channel.tg_id == tg_id))
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> Channel | None:
        result = await self.session.execute(
            select(Channel).where(func.lower(Channel.username) == username.lower()).limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_invite(self, invite_hash: str) -> Channel | None:
        result = await self.session.execute(
            select(Channel).where(Channel.invite_hash == invite_hash).limit(1)
        )
        return result.scalar_one_or_none()

    async def create(self, **fields) -> Channel:
        channel = Channel(**fields)
        self.session.add(channel)
        await self.session.commit()
        await self.session.refresh(channel)
        return channel

    async def save(self, channel: Channel) -> Channel:
        await self.session.commit()
        await self.session.refresh(channel)
        return channel

    async def list_for_user(self, user_id: int) -> list[tuple[Channel, Subscription]]:
        result = await self.session.execute(
            select(Channel, Subscription)
            .join(Subscription, Subscription.channel_id == Channel.id)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.added_at.desc())
        )
        return [(channel, sub) for channel, sub in result.all()]

    async def count_for_user(self, user_id: int) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Subscription).where(Subscription.user_id == user_id)
        )
        return result.scalar_one() or 0

    async def is_subscribed(self, user_id: int, channel_id: int) -> bool:
        return await self.session.get(Subscription, (user_id, channel_id)) is not None

    async def subscribe(self, user_id: int, channel: Channel, *, backlog_days: int = 3) -> bool:
        """Subscribe; returns False when the user already was."""
        if await self.is_subscribed(user_id, channel.id):
            return False
        now = datetime.now(timezone.utc)
        self.session.add(
            Subscription(
                user_id=user_id,
                channel_id=channel.id,
                added_at=now,
                feed_since=now - timedelta(days=backlog_days),
            )
        )
        channel.orphaned_at = None
        await self.session.commit()
        return True

    async def unsubscribe(self, user_id: int, channel_id: int) -> bool:
        result = await self.session.execute(
            delete(Subscription).where(
                Subscription.user_id == user_id, Subscription.channel_id == channel_id
            )
        )
        if not result.rowcount:
            await self.session.commit()
            return False
        remaining = await self.session.execute(
            select(func.count()).select_from(Subscription).where(Subscription.channel_id == channel_id)
        )
        if not remaining.scalar_one():
            channel = await self.get(channel_id)
            if channel:
                channel.orphaned_at = datetime.now(timezone.utc)
        await self.session.commit()
        return True

    async def merge_into(self, duplicate: Channel, target: Channel) -> None:
        """Move subscriptions of a queued duplicate onto the known channel."""
        subs = await self.session.execute(
            select(Subscription).where(Subscription.channel_id == duplicate.id)
        )
        for sub in subs.scalars().all():
            if await self.session.get(Subscription, (sub.user_id, target.id)) is None:
                self.session.add(
                    Subscription(
                        user_id=sub.user_id,
                        channel_id=target.id,
                        added_at=sub.added_at,
                        feed_since=sub.feed_since,
                    )
                )
            await self.session.delete(sub)
        target.orphaned_at = None
        await self.session.delete(duplicate)
        await self.session.commit()

    async def count_service_slots(self) -> int:
        """Channels the account is in or about to join: live ones and queued invites.

        Polled public channels are read without joining and take no slot.
        """
        result = await self.session.execute(
            select(func.count())
            .select_from(Channel)
            .where(
                or_(
                    and_(
                        Channel.status == ChannelStatus.ACTIVE.value,
                        Channel.mode == ChannelMode.LIVE.value,
                    ),
                    and_(
                        Channel.status == ChannelStatus.PENDING_JOIN.value,
                        Channel.invite_hash.isnot(None),
                        Channel.username.is_(None),
                    ),
                )
            )
        )
        return result.scalar_one() or 0

    async def count_polled(self) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(Channel)
            .where(
                or_(
                    and_(
                        Channel.status == ChannelStatus.ACTIVE.value,
                        Channel.mode == ChannelMode.POLL.value,
                    ),
                    and_(Channel.status == ChannelStatus.PENDING_JOIN.value, Channel.username.isnot(None)),
                )
            )
        )
        return result.scalar_one() or 0

    def _polled_with_subscribers(self):
        return (
            select(Channel)
            .where(
                Channel.status == ChannelStatus.ACTIVE.value,
                Channel.mode == ChannelMode.POLL.value,
                Channel.tg_id.isnot(None),
                exists().where(Subscription.channel_id == Channel.id),
            )
        )

    async def next_channel_to_poll(self) -> Channel | None:
        """The polled channel with subscribers that is due soonest (due or not)."""
        result = await self.session.execute(
            self._polled_with_subscribers()
            .order_by(Channel.next_poll_at.is_(None).desc(), Channel.next_poll_at, Channel.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def poll_now(self, user_id: int, stale_before: datetime) -> int:
        """Make the user's polled channels due now unless polled after ``stale_before``."""
        result = await self.session.execute(
            self._polled_with_subscribers().where(
                exists().where(
                    Subscription.channel_id == Channel.id, Subscription.user_id == user_id
                ),
                or_(Channel.last_polled_at.is_(None), Channel.last_polled_at < stale_before),
            )
        )
        channels = list(result.scalars().all())
        now = datetime.now(timezone.utc)
        for channel in channels:
            channel.next_poll_at = now
        if channels:
            await self.session.commit()
        return len(channels)

    async def next_pending_join(self) -> Channel | None:
        result = await self.session.execute(
            select(Channel)
            .where(Channel.status == ChannelStatus.PENDING_JOIN.value)
            .order_by(Channel.created_at, Channel.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def channels_with_access_hash(self) -> list[Channel]:
        result = await self.session.execute(
            select(Channel).where(Channel.tg_id.isnot(None), Channel.access_hash.isnot(None))
        )
        return list(result.scalars().all())

    async def active_channels(self) -> list[Channel]:
        result = await self.session.execute(
            select(Channel).where(
                Channel.status == ChannelStatus.ACTIVE.value,
                Channel.mode == ChannelMode.LIVE.value,
                Channel.tg_id.isnot(None),
            )
        )
        return list(result.scalars().all())

    async def channels_to_leave(self, older_than: datetime) -> list[Channel]:
        result = await self.session.execute(
            select(Channel).where(
                Channel.status == ChannelStatus.ACTIVE.value,
                Channel.mode == ChannelMode.LIVE.value,
                Channel.joined_by_service.is_(True),
                Channel.orphaned_at.isnot(None),
                Channel.orphaned_at < older_than,
            )
        )
        return list(result.scalars().all())
