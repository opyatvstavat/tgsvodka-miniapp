def normalize_chat_id(chat_id: int) -> int:
    """Normalize channel id to Telethon message chat_id form (-100...)."""
    if chat_id < 0:
        return chat_id
    return int(f"-100{chat_id}")
