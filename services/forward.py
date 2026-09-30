import logging

logger = logging.getLogger(__name__)


async def resolve_forward_channel(message, client) -> tuple[str | None, str | None, str | None]:
    """Resolve original channel from a forwarded summary message."""
    if not message or not message.forward:
        return None, None, None

    fwd = message.forward

    if getattr(fwd, "from_name", None):
        return fwd.from_name, None, None

    post_author = getattr(fwd, "post_author", None)
    if post_author:
        return post_author, None, None

    if fwd.chat:
        chat = fwd.chat
        username = getattr(chat, "username", None)
        title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
        link = None
        if username and getattr(fwd, "channel_post", None):
            link = f"https://t.me/{username}/{fwd.channel_post}"
        return title, username, link

    if not client:
        return None, None, None

    try:
        entity = await message.get_fwd_sender()
        if entity:
            username = getattr(entity, "username", None)
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
            link = None
            channel_post = getattr(fwd, "channel_post", None)
            if username and channel_post:
                link = f"https://t.me/{username}/{channel_post}"
            return title, username, link
    except Exception as exc:
        logger.debug("get_fwd_sender failed: %s", exc)

    return None, None, None


async def resolve_channel_metadata(message, client, raw_text: str | None = None) -> tuple[str | None, str | None, str | None]:
    """Combine header parsing and forward metadata resolution."""
    from services.message_parser import extract_channel_from_body_html, parse_channel_info

    title, username, link = parse_channel_info(raw_text or message.text or message.message or "")

    if client and message.forward:
        fwd_title, fwd_username, fwd_link = await resolve_forward_channel(message, client)
        title = title or fwd_title
        username = username or fwd_username
        link = link or fwd_link

    if not title:
        body_html = raw_text or message.text or message.message or ""
        if body_html and ("<a " in body_html or "<code>" in body_html):
            body_title, body_username, body_link = extract_channel_from_body_html(body_html)
            title = title or body_title
            username = username or body_username
            link = link or body_link

    return title, username, link
