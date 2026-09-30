import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.bot_client import BotClient
from api.bot_handlers import handle_message, webapp_url_is_valid
from api.routes import router
from bot import run_worker
from config import BOT_TOKEN, SESSION_STRING, WEBAPP_URL
from db.database import init_db
from services.telethon_pool import close_client

logger = logging.getLogger(__name__)

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
POLLING_TASK: asyncio.Task | None = None
WORKER_TASK: asyncio.Task | None = None

WORKER_RESTART_MIN_DELAY = 5
WORKER_RESTART_MAX_DELAY = 300


async def bot_polling_loop() -> None:
    bot = BotClient()
    offset = None
    logger.info("Bot polling started")

    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=25)
            for update in updates:
                offset = update["update_id"] + 1

                message = update.get("message")
                if not message:
                    continue
                try:
                    await handle_message(bot, message)
                except Exception as exc:
                    # One bad message must not stall the offset for everyone.
                    logger.error("Bot message error: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Bot polling error: %s", exc)
            await asyncio.sleep(5)


async def worker_supervisor() -> None:
    """Run Telethon ingest inside this process; restart it if it ever stops.

    Ingest used to be its own Railway service. Sharing the API process removes a
    container's worth of idle memory, but it also means a silent Telethon death
    would stop the feed while HTTP kept answering — hence the supervision.
    """
    delay = WORKER_RESTART_MIN_DELAY
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await run_worker()
            logger.warning("Telethon worker stopped, restarting in %ss", delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Telethon worker crashed (%s), restarting in %ss", exc, delay)

        if asyncio.get_running_loop().time() - started > 60:
            delay = WORKER_RESTART_MIN_DELAY

        await asyncio.sleep(delay)
        delay = min(delay * 2, WORKER_RESTART_MAX_DELAY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global POLLING_TASK, WORKER_TASK
    await init_db()

    if SESSION_STRING:
        WORKER_TASK = asyncio.create_task(worker_supervisor())
        logger.info("Telethon ingest running in-process")
    else:
        logger.warning("SESSION_STRING not set, Telethon ingest disabled")

    if BOT_TOKEN:
        if webapp_url_is_valid():
            logger.info("Bot polling enabled, WEBAPP_URL=%s", WEBAPP_URL)
        else:
            logger.warning(
                "WEBAPP_URL is not HTTPS (%s). /start will work without Mini App button. "
                "Use ngrok for local testing or deploy to Railway.",
                WEBAPP_URL,
            )
        POLLING_TASK = asyncio.create_task(bot_polling_loop())
    else:
        logger.warning("BOT_TOKEN not set, bot polling disabled")
    yield
    for task in (POLLING_TASK, WORKER_TASK):
        if not task:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await close_client()


app = FastAPI(title="TGSvodka Mini App", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(
            WEB_DIST / "index.html",
            headers={"Cache-Control": "no-store, max-age=0, must-revalidate"},
        )

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        file_path = WEB_DIST / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(
            WEB_DIST / "index.html",
            headers={"Cache-Control": "no-store, max-age=0, must-revalidate"},
        )
