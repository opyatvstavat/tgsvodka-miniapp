import json
import logging
import os
from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import SQLITE_PATH, get_async_database_url
from db.models import Base

logger = logging.getLogger(__name__)

DATABASE_URL = get_async_database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# Bumped when a one-time data rewrite is needed; tracked in PRAGMA user_version.
SCHEMA_VERSION = 1


def _json_serializer(obj) -> str:
    """Store JSON as UTF-8, not \\uXXXX escapes.

    SQLAlchemy defaults to ensure_ascii=True, which turns every Cyrillic
    character into six ASCII ones. On this data that inflated media_json from
    ~35 MB to ~117 MB, and the file is read straight into the page cache the
    container is billed for.
    """
    return json.dumps(obj, ensure_ascii=False)


engine = create_async_engine(DATABASE_URL, echo=False, json_serializer=_json_serializer)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


if IS_SQLITE:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        """WAL lets the feed read while ingest writes; without it they block."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


async def _column_names(conn, table: str) -> set[str]:
    if IS_SQLITE:
        rows = await conn.execute(text(f"PRAGMA table_info({table})"))
        return {row[1] for row in rows}
    rows = await conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
        {"t": table},
    )
    return {row[0] for row in rows}


async def _run_migrations(conn) -> None:
    # SQLite has no ADD COLUMN IF NOT EXISTS, so check the schema either way.
    columns = await _column_names(conn, "posts")
    if "sync_source" not in columns:
        await conn.execute(
            text("ALTER TABLE posts ADD COLUMN sync_source VARCHAR(16) NOT NULL DEFAULT 'live'")
        )
    multi_user_upgrade = "channel_id" not in columns
    if multi_user_upgrade:
        await conn.execute(text("ALTER TABLE posts ADD COLUMN channel_id BIGINT"))
    if "published_at" not in columns:
        await conn.execute(text("ALTER TABLE posts ADD COLUMN published_at TIMESTAMP"))

    # create_all only adds indexes for tables it creates, so an index added to an
    # existing table has to be created explicitly.
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_posts_status_summary_msg "
            "ON posts (status, summary_msg_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_posts_channel_published "
            "ON posts (channel_id, published_at)"
        )
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_posts_published ON posts (published_at)"))

    channel_columns = await _column_names(conn, "channels")
    if "mode" not in channel_columns:
        # Every existing channel was joined, so it stays live.
        await conn.execute(text("ALTER TABLE channels ADD COLUMN mode VARCHAR(8) NOT NULL DEFAULT 'live'"))
    if "poll_interval" not in channel_columns:
        await conn.execute(text("ALTER TABLE channels ADD COLUMN poll_interval INTEGER"))
    if "next_poll_at" not in channel_columns:
        await conn.execute(text("ALTER TABLE channels ADD COLUMN next_poll_at TIMESTAMP"))
    if "last_polled_at" not in channel_columns:
        await conn.execute(text("ALTER TABLE channels ADD COLUMN last_polled_at TIMESTAMP"))

    if multi_user_upgrade:
        await _migrate_to_multi_user(conn)
    await _assign_history_to_owner(conn)


async def _migrate_to_multi_user(conn) -> None:
    """Turn the single-user queue into the owner's subscriptions and history.

    Runs once, when ``posts.channel_id`` is first added. Every channel seen in
    the queue becomes an active channel; ``_assign_history_to_owner`` then
    subscribes the owner to them and copies the per-post status.
    """
    from services.channel_filters import excluded_feed_usernames, ignored_source_usernames

    # The feed pages by published_at, and SQLite compares these as strings: the
    # copy has to be in SQLAlchemy's own format (with microseconds), or a cursor
    # read back from a row would sort after that same row.
    copy = "strftime('%Y-%m-%d %H:%M:%f', created_at) || '000'" if IS_SQLITE else "created_at"
    await conn.execute(text(f"UPDATE posts SET published_at = {copy} WHERE published_at IS NULL"))
    # A live post knows its real source chat; a backfilled one only knows the
    # summary copy (source == summary) and is matched by username below.
    await conn.execute(
        text(
            "UPDATE posts SET channel_id = source_chat_id "
            "WHERE source_chat_id IS NOT NULL AND source_chat_id != summary_chat_id"
        )
    )

    rows = (
        await conn.execute(
            text(
                "SELECT channel_id, channel_username, channel_title FROM posts "
                "WHERE channel_id IS NOT NULL ORDER BY published_at"
            )
        )
    ).all()
    channels: dict[int, dict] = {}
    by_username: dict[str, int] = {}
    for channel_id, username, title in rows:
        entry = channels.setdefault(channel_id, {"username": None, "title": None})
        entry["username"] = username or entry["username"]
        entry["title"] = title or entry["title"]
        if username:
            by_username[username.lower()] = channel_id

    # rowid is SQLite's; production runs SQLite, and on Postgres these few
    # backfilled posts simply stay owner-only legacy rows.
    orphans = (
        await conn.execute(
            text("SELECT rowid, channel_username FROM posts WHERE channel_id IS NULL AND channel_username IS NOT NULL")
        )
    ).all() if IS_SQLITE else []
    updates = [
        {"cid": by_username[username.lower()], "row": rowid}
        for rowid, username in orphans
        if username.lower() in by_username
    ]
    if updates:
        await conn.execute(text("UPDATE posts SET channel_id = :cid WHERE rowid = :row"), updates)

    skipped = excluded_feed_usernames() | ignored_source_usernames()
    channel_rows = [
        {
            "tg_id": tg_id,
            "username": info["username"],
            "title": info["title"],
            "is_private": info["username"] is None,
        }
        for tg_id, info in channels.items()
        if not (info["username"] and info["username"].lower() in skipped)
    ]
    if channel_rows:
        await conn.execute(
            text(
                "INSERT INTO channels (tg_id, username, title, is_private, status, joined_by_service, created_at) "
                "VALUES (:tg_id, :username, :title, :is_private, 'active', false, CURRENT_TIMESTAMP)"
            ),
            channel_rows,
        )

    logger.info(
        "Multi-user migration: %s channels, %s backfilled posts matched by username",
        len(channel_rows),
        len(updates),
    )


async def _assign_history_to_owner(conn) -> None:
    """Give the owner the pre-multi-user channels and read history, once.

    Separate from the upgrade: an open feed may run without OWNER_USER_ID, and
    the history then waits until the owner is configured. Legacy posts keep
    their old status column, so it is still accurate when that happens.
    """
    from config import OWNER_USER_ID

    if not OWNER_USER_ID:
        return
    done = await conn.execute(
        text("SELECT 1 FROM users WHERE id = :id AND plan = 'owner'"), {"id": OWNER_USER_ID}
    )
    if done.first():
        return
    await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": OWNER_USER_ID})
    await conn.execute(
        text("INSERT INTO users (id, plan, created_at) VALUES (:id, 'owner', CURRENT_TIMESTAMP)"),
        {"id": OWNER_USER_ID},
    )
    await conn.execute(
        text("DELETE FROM subscriptions WHERE user_id = :owner"), {"owner": OWNER_USER_ID}
    )
    await conn.execute(
        text("DELETE FROM user_post_states WHERE user_id = :owner AND post_id IN (SELECT id FROM posts WHERE status != 'pending')"),
        {"owner": OWNER_USER_ID},
    )
    # An old feed_since keeps the whole backlog in the owner's feed.
    await conn.execute(
        text(
            "INSERT INTO subscriptions (user_id, channel_id, added_at, feed_since) "
            "SELECT :owner, id, CURRENT_TIMESTAMP, '2000-01-01 00:00:00.000000' FROM channels"
        ),
        {"owner": OWNER_USER_ID},
    )
    await conn.execute(
        text(
            "INSERT INTO user_post_states (user_id, post_id, status, decided_at) "
            "SELECT :owner, id, status, decided_at FROM posts WHERE status != 'pending'"
        ),
        {"owner": OWNER_USER_ID},
    )
    logger.info("Assigned queue history to owner %s", OWNER_USER_ID)


async def _compact_json(conn) -> int:
    """Rewrite media_json that was stored with \\uXXXX escapes.

    Rows written before the json_serializer above carry six ASCII characters per
    Cyrillic one. Rewriting them shrinks the file by roughly a third.
    """
    rows = (
        await conn.execute(text("SELECT rowid, media_json FROM posts WHERE media_json IS NOT NULL"))
    ).all()

    updates = []
    for rowid, raw in rows:
        try:
            compact = json.dumps(json.loads(raw), ensure_ascii=False)
        except (TypeError, ValueError):
            continue
        if compact != raw:
            updates.append({"value": compact, "row": rowid})

    if updates:
        await conn.execute(
            text("UPDATE posts SET media_json = :value WHERE rowid = :row"), updates
        )
    return len(updates)


async def _compact_once() -> None:
    """Run the compaction a single time, tracked by PRAGMA user_version."""
    async with engine.begin() as conn:
        version = (await conn.execute(text("PRAGMA user_version"))).scalar() or 0
        if version >= SCHEMA_VERSION:
            return
        changed = await _compact_json(conn)
        await conn.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))

    logger.info("Compacted %s media_json rows, reclaiming space", changed)
    if changed:
        # VACUUM cannot run inside a transaction.
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text("VACUUM"))
        logger.info("VACUUM complete")


async def _truncate_wal() -> None:
    """Shrink the WAL file at boot.

    SQLite reuses the WAL rather than shrinking it, so one large transaction
    leaves a large file behind for good — the compaction above left a 190 MB
    WAL next to a 129 MB database. Cheap when the WAL is already small, and
    there are no readers to block at startup.
    """
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


def release_sqlite_page_cache() -> None:
    """Tell Linux that recently scanned SQLite pages can be reclaimed.

    A backfill reads a large portion of the queue. The data is durable on the
    mounted volume, while keeping those pages resident makes Railway bill them
    as container memory. SQLite will read a page again if a later request needs
    it. This is deliberately advisory and a no-op on platforms without
    ``posix_fadvise``.
    """
    if not IS_SQLITE or not hasattr(os, "posix_fadvise"):
        return
    for path in (SQLITE_PATH, f"{SQLITE_PATH}-wal", f"{SQLITE_PATH}-shm"):
        try:
            with open(path, "rb") as handle:
                os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            continue


async def init_db() -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _run_migrations(conn)
        if IS_SQLITE:
            await _compact_once()
            await _truncate_wal()
    except ConnectionRefusedError as exc:
        raise ConnectionRefusedError(
            "Cannot connect to the database. For Postgres, start it first:\n"
            "  docker compose up postgres -d"
        ) from exc


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
