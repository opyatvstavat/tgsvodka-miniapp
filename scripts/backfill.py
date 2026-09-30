"""One-off backfill of summary channel into the post queue."""

import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import API_HASH, API_ID, SESSION_NAME, SESSION_STRING, TARGET_CHANNEL
from db.backfill import backfill_summary_channel
from db.database import init_db

load_dotenv()


async def main() -> None:
    if not TARGET_CHANNEL:
        raise SystemExit("TARGET_CHANNEL is not set")

    await init_db()

    if SESSION_STRING:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    else:
        client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    async with client:
        await client.start()
        result = await backfill_summary_channel(client, TARGET_CHANNEL)
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
