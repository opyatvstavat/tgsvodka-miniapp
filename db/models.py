import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PostStatus(str, enum.Enum):
    PENDING = "pending"
    SEEN = "seen"
    LIKED = "liked"
    SKIPPED = "skipped"


class SyncSource(str, enum.Enum):
    LIVE = "live"
    BACKFILL = "backfill"


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("summary_chat_id", "summary_msg_id", name="uq_posts_summary"),
        Index("ix_posts_status_created", "status", "created_at"),
        # The feed filters by status and orders by summary_msg_id; without this
        # the queue query scans every pending row (~5s on SQLite at 77k rows).
        Index("ix_posts_status_summary_msg", "status", "summary_msg_id"),
        Index("ix_posts_channel_published", "channel_id", "published_at"),
        Index("ix_posts_published", "published_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    grouped_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Telegram id of the source channel (-100...). Posts ingested straight from
    # a channel store their own message as the "summary" one, so the unique
    # (summary_chat_id, summary_msg_id) pair dedupes them across users.
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    summary_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    summary_msg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    channel_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    media_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PostStatus.PENDING.value)
    sync_source: Mapped[str] = mapped_column(String(16), nullable=False, default=SyncSource.LIVE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When the channel published it; the feed is ordered by this, since message
    # ids of different channels cannot be compared.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChannelSyncState(Base):
    """Tracks summary channel scan: which messages exist vs which are in DB."""

    __tablename__ = "channel_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    summary_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    channel_messages_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_in_db: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_added_last_run: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_skipped_existing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_untracked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ChannelStatus(str, enum.Enum):
    PENDING_JOIN = "pending_join"
    ACTIVE = "active"
    FAILED = "failed"
    LEFT = "left"


class ChannelMode(str, enum.Enum):
    # The account is in the channel and new posts arrive as live events.
    LIVE = "live"
    # A public channel read with GetHistory on a schedule, without joining:
    # it takes none of the account's 500 channel slots.
    POLL = "poll"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plan: Mapped[str] = mapped_column(String(16), nullable=False, default="free")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Channel(Base):
    """A channel the service account reads, shared by everyone subscribed to it.

    A row exists before its Telegram id is known: a channel added by @username
    or invite link waits in the join queue with ``tg_id`` empty.
    """

    __tablename__ = "channels"
    __table_args__ = (
        Index("ix_channels_username", "username"),
        Index("ix_channels_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    access_hash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invite_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ChannelStatus.PENDING_JOIN.value)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Only channels the join queue entered are ever left again: the service
    # account is also the owner's own account, and its manual subscriptions
    # are not ours to undo.
    joined_by_service: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    added_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    orphaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default=ChannelMode.LIVE.value)
    # Seconds between polls of a ``poll`` channel; shrinks while it posts, grows while quiet.
    poll_interval: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_channel", "channel_id"),)

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Older posts of the channel stay out of this user's feed, so a new
    # subscription brings the last few days instead of the whole history.
    feed_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserPostState(Base):
    """What one user did with one post; no row means the post is still unread."""

    __tablename__ = "user_post_states"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    post_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Time the post stayed on screen: the first signal for recommendations.
    dwell_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
