from config import EXCLUDED_FEED_CHANNELS, HIGHLIGHTS_CHANNEL, OWN_CHANNELS, TARGET_CHANNEL


def normalize_channel(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lstrip("@").lower()
    return cleaned or None


def ignored_source_usernames() -> set[str]:
    ignored = set(OWN_CHANNELS)
    for channel in (TARGET_CHANNEL, HIGHLIGHTS_CHANNEL):
        normalized = normalize_channel(channel)
        if normalized:
            ignored.add(normalized)
    return ignored


def excluded_feed_usernames() -> set[str]:
    excluded = set(EXCLUDED_FEED_CHANNELS)
    highlights = normalize_channel(HIGHLIGHTS_CHANNEL)
    if highlights:
        excluded.add(highlights)
    return excluded


def is_ignored_source_chat(chat) -> bool:
    username = normalize_channel(getattr(chat, "username", None))
    if username and username in ignored_source_usernames():
        return True
    return False


def is_excluded_feed_channel(channel_username: str | None, channel_title: str | None) -> bool:
    username = normalize_channel(channel_username)
    if username and username in excluded_feed_usernames():
        return True

    title = (channel_title or "").strip().lower()
    for excluded in excluded_feed_usernames():
        if title in {excluded, f"/{excluded}/", f"@{excluded}"}:
            return True
    return False
