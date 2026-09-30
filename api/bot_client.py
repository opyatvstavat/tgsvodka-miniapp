import logging

import httpx

from config import BOT_TOKEN, HIGHLIGHTS_CHAT_ID, HIGHLIGHTS_CHANNEL

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class BotClient:
    def __init__(self, token: str | None = BOT_TOKEN):
        self.token = token
        self.base_url = f"{TELEGRAM_API}/bot{token}" if token else None

    async def _post(self, method: str, payload: dict) -> dict:
        if not self.base_url:
            raise RuntimeError("BOT_TOKEN is not configured")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{self.base_url}/{method}", json=payload)
            data = response.json()

        if not data.get("ok"):
            description = data.get("description", "Unknown Telegram API error")
            logger.error("Telegram API error on %s: %s", method, description)
            raise RuntimeError(description)

        return data["result"]

    async def copy_to_highlights(self, from_chat_id: int, message_id: int) -> dict:
        chat_id = HIGHLIGHTS_CHAT_ID or HIGHLIGHTS_CHANNEL
        return await self._post(
            "copyMessage",
            {
                "chat_id": chat_id,
                "from_chat_id": from_chat_id,
                "message_id": message_id,
            },
        )

    async def save_prepared_inline_message(self, user_id: int, result: dict) -> str:
        prepared = await self._post(
            "savePreparedInlineMessage",
            {
                "user_id": user_id,
                "result": result,
                "allow_user_chats": True,
                "allow_bot_chats": True,
                "allow_group_chats": True,
                "allow_channel_chats": True,
            },
        )
        return prepared["id"]

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list:
        payload: dict = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        result = await self._post("getUpdates", payload)
        return result if isinstance(result, list) else []

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self._post("sendMessage", payload)

    async def answer_callback_query(self, callback_query_id: str) -> None:
        await self._post("answerCallbackQuery", {"callback_query_id": callback_query_id})
