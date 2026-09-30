"""Bot chat: /start, and adding channels by forwarded post or link."""

import logging

from api.auth import remember_user
from api.bot_client import BotClient
from config import ALLOWED_USER_ID, OWNER_USER_ID, WEBAPP_URL
from db.database import SessionLocal
from db.repository import ChannelRepository
from services.channel_registry import ChannelError, ChannelRef, add_subscription, find_channel_refs, parse_channel_ref

logger = logging.getLogger(__name__)

MAX_REFS_PER_MESSAGE = 5

HELP_TEXT = (
    "Чтобы добавить канал в ленту:\n"
    "• перешли сюда любой пост из канала,\n"
    "• или пришли @username / ссылку t.me/…,\n"
    "• для приватного канала — ссылку-приглашение t.me/+…"
)


def webapp_url_is_valid() -> bool:
    return WEBAPP_URL.startswith("https://")


def start_reply_markup() -> dict | None:
    if not webapp_url_is_valid():
        return None
    return {"inline_keyboard": [[{"text": "Открыть ленту", "web_app": {"url": WEBAPP_URL}}]]}


def start_message() -> str:
    if webapp_url_is_valid():
        return f"Лента постов из твоих Telegram-каналов — листай как в TikTok.\n\n{HELP_TEXT}"
    return (
        "Mini App requires HTTPS.\n\n"
        "For local testing:\n"
        "1. Run: ngrok http 8000\n"
        "2. Set WEBAPP_URL=https://YOUR-NGROK-URL in .env\n"
        "3. Set the same URL in @BotFather (/newapp)\n"
        "4. Restart: docker compose up --build"
    )


def _describe(result: dict) -> str:
    channel = result["channel"]
    title = channel["title"]
    if result["result"] == "already":
        return f"«{title}» уже в твоей ленте."
    if result["result"] == "subscribed":
        return f"Добавил «{title}». Посты уже в ленте."
    return f"«{title}» подключается — посты появятся через несколько минут."


async def _subscribe(user_id: int, ref: ChannelRef, **kwargs) -> str:
    try:
        result = await add_subscription(user_id, ref, is_owner=user_id == OWNER_USER_ID, **kwargs)
    except ChannelError as exc:
        return str(exc)
    return _describe(result)


async def _from_forward(user_id: int, origin: dict) -> str:
    chat = origin.get("chat") or {}
    tg_id = chat.get("id")
    username = chat.get("username")
    title = chat.get("title")
    if username:
        return await _subscribe(user_id, ChannelRef(username=username), tg_id=tg_id, title=title)
    # A private channel can only be entered by invite, unless we are already in it.
    async with SessionLocal() as session:
        known = await ChannelRepository(session).get_by_tg_id(tg_id) if tg_id else None
    if known is None:
        return f"«{title or 'Этот канал'}» приватный. Пришли ссылку-приглашение (t.me/+…), и я его подключу."
    return await _subscribe(user_id, ChannelRef(), tg_id=tg_id, title=title)


async def handle_message(bot: BotClient, message: dict) -> None:
    sender = message.get("from") or {}
    chat_id = message["chat"]["id"]
    user_id = sender.get("id")
    if not user_id or message["chat"].get("type") != "private":
        return

    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        await bot.send_message(chat_id, "Бот пока в закрытом тесте.")
        return
    await remember_user(sender)

    text = message.get("text") or message.get("caption") or ""
    origin = message.get("forward_origin") or {}
    replies: list[str] = []

    if text.startswith("/start"):
        payload = text.partition(" ")[2].strip()
        if payload.startswith("add_") and (ref := parse_channel_ref(payload[4:])):
            replies.append(await _subscribe(user_id, ref))
        else:
            await bot.send_message(chat_id, start_message(), reply_markup=start_reply_markup())
            return
    elif origin.get("type") == "channel":
        replies.append(await _from_forward(user_id, origin))
    else:
        refs = find_channel_refs(text)
        if not refs and (ref := parse_channel_ref(text)):
            refs = [ref]
        for ref in refs[:MAX_REFS_PER_MESSAGE]:
            replies.append(await _subscribe(user_id, ref))

    if not replies:
        await bot.send_message(chat_id, HELP_TEXT, reply_markup=start_reply_markup())
        return
    await bot.send_message(chat_id, "\n".join(replies), reply_markup=start_reply_markup())
