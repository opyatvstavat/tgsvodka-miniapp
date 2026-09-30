import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional_int(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value else None


API_ID = int(os.getenv("API_ID")) if os.getenv("API_ID") else None
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "session_name")
SESSION_STRING = (
    os.getenv("SESSION_STRING")
    or os.getenv("WORKER_SESSION_STRING")
    # Ingest used to be a separate service with its own SESSION_STRING. Now that
    # it runs inside the API process, a deployment may only carry the web
    # session — same account, works for ingest just as well.
    or os.getenv("WEB_SESSION_STRING")
)
WEB_SESSION_STRING = os.getenv("WEB_SESSION_STRING") or SESSION_STRING
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")
HIGHLIGHTS_CHANNEL = os.getenv("HIGHLIGHTS_CHANNEL")
OWN_CHANNELS = [
    ch.strip().lstrip("@")
    for ch in os.getenv("OWN_CHANNELS", "").split(",")
    if ch.strip()
]
EXCLUDED_FEED_CHANNELS = [
    ch.strip().lstrip("@").lower()
    for ch in os.getenv("EXCLUDED_FEED_CHANNELS", "").split(",")
    if ch.strip()
]

DATABASE_URL = os.getenv("DATABASE_URL", "")
# Where SQLite lives when DATABASE_URL is unset. Must be on the mounted volume,
# and must not sit inside MEDIA_CACHE_DIR — that directory is pruned by size.
SQLITE_PATH = os.getenv("SQLITE_PATH", "/data/tgsvodka.db")
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000")
# When set, only this user gets in; unset, the feed is open to everyone.
ALLOWED_USER_ID = _optional_int("ALLOWED_USER_ID")
# The owner runs the service account: only they may comment from it, and only
# their likes go to the highlights channel. Everyone else reads. Separate from
# ALLOWED_USER_ID so an open feed still has an owner.
OWNER_USER_ID = _optional_int("OWNER_USER_ID") or ALLOWED_USER_ID
# Old single-user pipeline: repost every post into the summary channel and
# ingest the copy. Off by default — posts are saved straight from their channel.
SUMMARY_MIRROR = os.getenv("SUMMARY_MIRROR", "").lower() in {"1", "true", "yes"}
FREE_CHANNEL_LIMIT = int(os.getenv("FREE_CHANNEL_LIMIT", "20"))
# One account can be in 500 channels; leave room for the owner's own chats.
MAX_SERVICE_CHANNELS = int(os.getenv("MAX_SERVICE_CHANNELS", "450"))
# Joining too fast gets the account FloodWait-ed or banned.
JOIN_INTERVAL = int(os.getenv("JOIN_INTERVAL", "120"))
LEAVE_AFTER_DAYS = int(os.getenv("LEAVE_AFTER_DAYS", "7"))
# Public channels are not joined but polled. Resolving a username is cheaper
# than joining, yet still limited, hence a pause of its own.
RESOLVE_INTERVAL = int(os.getenv("RESOLVE_INTERVAL", "20"))
MAX_POLLED_CHANNELS = int(os.getenv("MAX_POLLED_CHANNELS", "2000"))
POLL_MIN_SECONDS = int(os.getenv("POLL_MIN_SECONDS", "300"))
POLL_MAX_SECONDS = int(os.getenv("POLL_MAX_SECONDS", "3600"))
POLL_START_SECONDS = int(os.getenv("POLL_START_SECONDS", "900"))
# Pause between two GetHistory calls: at most ~1800 polls an hour.
POLL_GAP = float(os.getenv("POLL_GAP", "2"))
SUMMARY_CHAT_ID = os.getenv("SUMMARY_CHAT_ID")
HIGHLIGHTS_CHAT_ID = os.getenv("HIGHLIGHTS_CHAT_ID")
BACKFILL_LIMIT = int(os.getenv("BACKFILL_LIMIT", "1000"))


def get_async_database_url() -> str:
    # Default to SQLite on the mounted volume: a Postgres container costs more in
    # idle memory than the whole API process, and this database is ~100 MB.
    url = DATABASE_URL or f"sqlite+aiosqlite:///{SQLITE_PATH}"
    if url.startswith("sqlite:"):
        url = url.replace("sqlite:", "sqlite+aiosqlite:", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url
