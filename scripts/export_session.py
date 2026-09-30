"""Export Telethon StringSession for Railway deployment."""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Export Telethon StringSession")
    parser.add_argument(
        "--session-name",
        default=os.getenv("SESSION_NAME", "session_name"),
        help="Session file name. Use a different name for a second device session (web vs worker).",
    )
    args = parser.parse_args()

    client = TelegramClient(args.session_name, API_ID, API_HASH)
    await client.start()
    session_string = StringSession.save(client.session)
    print(f"\nStringSession for '{args.session_name}' (copy to Railway secrets):\n")
    print(session_string)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
