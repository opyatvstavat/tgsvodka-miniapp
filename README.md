# TGSvodka

Telethon ingest collects channel posts into a summary channel and a local queue. The Telegram Mini App is a full-screen swipe feed: showing a post marks it viewed for future sessions, while Like copies it to highlights. Summary channel messages are never deleted.

## Architecture

One service, one process: FastAPI API, Mini App UI, Bot `/start` polling,
Telethon ingest and reactions, and a SQLite post queue (`pending` / `seen` /
`liked` / `skipped`) on a mounted volume.

It used to be three Railway services — web, worker and Postgres. Railway bills
for memory held, not work done, so three idle containers cost more than the
work they did. Ingest moved into the web process (`api.main.worker_supervisor`
restarts it if it dies) and the queue moved to SQLite on the volume.

## Local setup

1. Copy `.env.example` to `.env` and fill values.
2. Start the service:

```bash
docker compose up --build
```

3. First-time Telethon login (local):

```bash
pip install -r requirements.txt
python bot.py
```

4. Export session for Railway:

```bash
python scripts/export_session.py
```

Copy the session string into Railway as `WEB_SESSION_STRING`.

5. Build frontend only (optional):

```bash
cd web && npm install && npm run build
```

6. Run API locally:

```bash
uvicorn api.main:app --reload --port 8000
```

## Telegram setup

1. Create bot via @BotFather, set `BOT_TOKEN`.
2. Add bot as **admin** to summary and highlights channels.
3. Register Mini App URL (`WEBAPP_URL`) via `/newapp` in BotFather.
4. Set `ALLOWED_USER_ID` to your Telegram user id.
5. Set `SUMMARY_CHAT_ID` and `HIGHLIGHTS_CHAT_ID` (numeric, e.g. `-1001234567890`).

## Railway deployment

See [DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md) for the full walkthrough. One service:
`Dockerfile.web`, an HTTPS domain, and a volume mounted at `/data` holding both
the SQLite database and the media cache.

Variables:

```
BOT_TOKEN
WEBAPP_URL=https://<web-service-domain>
ALLOWED_USER_ID
SUMMARY_CHAT_ID
HIGHLIGHTS_CHAT_ID
TARGET_CHANNEL
HIGHLIGHTS_CHANNEL
OWN_CHANNELS
API_ID
API_HASH
WEB_SESSION_STRING
SQLITE_PATH=/data/tgsvodka.db
MEDIA_CACHE_DIR=/data/media
MEDIA_CACHE_MAX_MB=1500
CHANNEL_AVATAR_CACHE_DIR=/data/avatars
CHANNEL_AVATAR_CACHE_MAX_MB=25
```

Leave `DATABASE_URL` unset to use SQLite; set it to a `postgresql://` URL to use
Postgres instead.

The session is resolved as `SESSION_STRING` → `WORKER_SESSION_STRING` →
`WEB_SESSION_STRING`, so any one of them is enough.

**Replicas = 1.** Two replicas mean two Telethon clients, which would duplicate
every forward into the summary channel.

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/queue?limit=8&before=<summary_msg_id>` | Stable page of unseen posts and cursor for the next page |
| POST | `/api/posts/{id}/seen` | Mark a displayed post seen; it remains available until this Mini App session closes |
| POST | `/api/posts/{id}/like` | Copy a pending or seen post to highlights + mark liked |
| GET | `/api/posts/{id}/avatar` | Cached avatar of the source channel |
| GET | `/api/posts/{id}/comments?before=<comment_id>&limit=20` | Page of comments on the original Telegram post |
| POST | `/api/posts/{id}/comments` | Send `{ "text": "..." }` into the original discussion as the connected Telegram account |
| GET | `/api/stats` | Queue counts |

Auth header: `Authorization: tma <Telegram.WebApp.initData>`

## Notes

- Summary channel messages are never deleted.
- Reactions on `OWN_CHANNELS` still forward to highlights (unchanged).
- **Backfill**: imports up to `BACKFILL_LIMIT` (default 1000) newest summary posts on startup and every `BACKFILL_INTERVAL` seconds (default 3600). All **new** posts are always added via live ingest.
- **Media**: photos/videos shown inline via Telethon proxy (`/api/posts/{id}/media/{index}`).
  Files are cached on disk under `MEDIA_CACHE_DIR`, streamed in chunks rather than
  read into memory, and evicted least-recently-used once the directory passes
  `MEDIA_CACHE_MAX_MB`. A cached file is served without contacting Telegram at all.
- **Prefetch**: the feed warms only photo thumbnails for the next few cards;
  full videos, voice notes and original photos load when a card is opened.
- **Comments and avatars**: neither is stored in SQLite. Avatars use a bounded
  disk cache; comments are requested from Telegram only when the panel opens.
- **Sync stats**: header shows `pending · X/Y in DB` (Y = last scanned window, up to BACKFILL_LIMIT).

- **Queue counts** use `COUNT(*)`, not `COUNT(id)`: counting the primary key
  forces a table lookup per row because `id` is not in the index, which took
  seconds over ~95k rows. `/api/stats` also caches its result for a few seconds,
  since the feed requests it on every like and skip.

Manual backfill:

```bash
python scripts/backfill.py
```

Migrating an existing Postgres database into SQLite:

```bash
SOURCE_DATABASE_URL=postgresql://… python scripts/migrate_to_sqlite.py tgsvodka.db
```
