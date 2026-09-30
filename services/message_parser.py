import re
from html import escape, unescape

from telethon.helpers import add_surrogate, del_surrogate, within_surrogate
from telethon.tl.types import (
    DocumentAttributeAudio,
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityEmail,
    MessageEntityItalic,
    MessageEntityMentionName,
    MessageEntityPre,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
    MessageEntityUrl,
)

from services.html_sanitize import sanitize_channel_label, sanitize_html


# Bump when stored ``content_html`` was produced by a formatter with a bug, so
# the feed stops trusting it and fetches the original post again.
CONTENT_VERSION = 2


def strip_html(text: str | None) -> str | None:
    if not text:
        return None
    # Preserve visible structure when this is used to recover a broken
    # forwarded post.  Removing <br> outright merged all paragraphs.
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|blockquote|pre)\s*>", "\n", text, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", "", text)
    return unescape(cleaned).strip() or None


def _entity_tags(entity, text: str) -> tuple[str, str] | None:
    """HTML for a Telegram entity without overlapping-tag corruption."""
    if isinstance(entity, MessageEntityBold):
        return "<strong>", "</strong>"
    if isinstance(entity, MessageEntityItalic):
        return "<em>", "</em>"
    if isinstance(entity, MessageEntityUnderline):
        return "<u>", "</u>"
    if isinstance(entity, MessageEntityStrike):
        return "<del>", "</del>"
    if isinstance(entity, MessageEntityCode):
        return "<code>", "</code>"
    if isinstance(entity, MessageEntityPre):
        return "<pre>", "</pre>"
    if isinstance(entity, MessageEntityBlockquote):
        return "<blockquote>", "</blockquote>"
    if isinstance(entity, MessageEntityTextUrl):
        return f'<a href="{escape(entity.url, quote=True)}" target="_blank" rel="noopener">', "</a>"
    if isinstance(entity, MessageEntityUrl):
        return f'<a href="{escape(text, quote=True)}" target="_blank" rel="noopener">', "</a>"
    if isinstance(entity, MessageEntityEmail):
        return f'<a href="mailto:{escape(text, quote=True)}">', "</a>"
    if isinstance(entity, MessageEntityMentionName):
        return f'<a href="tg://user?id={entity.user_id}">', "</a>"
    return None


def render_telegram_entities(text: str | None, entities=None) -> str | None:
    """Render source entities once while preserving nested links and styles."""
    if not text:
        return None
    entities = list(entities or [])
    if not entities:
        return escape(text).replace("\n", "<br>")

    surrogate = add_surrogate(text)
    openings: dict[int, list[tuple[int, str]]] = {}
    closings: dict[int, list[tuple[int, str]]] = {}
    for entity in entities:
        start = max(0, min(entity.offset, len(surrogate)))
        end = max(start, min(entity.offset + entity.length, len(surrogate)))
        while within_surrogate(surrogate, start):
            start += 1
        while within_surrogate(surrogate, end):
            end += 1
        tags = _entity_tags(entity, del_surrogate(surrogate[start:end]))
        if not tags or start == end:
            continue
        opening, closing = tags
        openings.setdefault(start, []).append((end, opening))
        closings.setdefault(end, []).append((start, closing))

    positions = sorted(set([0, len(surrogate), *openings, *closings]))
    result: list[str] = []
    for index, position in enumerate(positions):
        for _start, closing in sorted(closings.get(position, []), reverse=True):
            result.append(closing)
        for _end, opening in sorted(openings.get(position, []), reverse=True):
            result.append(opening)
        if index + 1 < len(positions):
            next_position = positions[index + 1]
            result.append(escape(del_surrogate(surrogate[position:next_position])).replace("\n", "<br>"))
    return "".join(result)


def _looks_like_summary_header(fragment: str) -> bool:
    if not fragment or not fragment.strip():
        return False
    s = fragment.strip()
    if "👤" in s:
        return True
    if re.search(r"<code>[^<]+</code>", s, re.I):
        return True
    if re.search(r'href="https://t\.me/', s, re.I):
        return True
    if re.match(r"^\[👤\]\(https://t\.me/", s):
        return True
    if re.match(r"^`[^`]{2,120}`\s*$", s):
        return True
    return False


def _visible_text(text: str | None) -> str:
    if not text:
        return ""
    return (strip_html(text) or text).strip().strip("`")


def _body_matches_channel(body: str | None, title: str | None, username: str | None = None) -> bool:
    visible = _visible_text(body)
    if not visible:
        return True
    for candidate in (title, username):
        if candidate and visible.casefold() == candidate.strip().casefold():
            return True
    return False


def _parse_header_only(text: str) -> tuple[str | None, str | None, str | None]:
    """True when the entire message is only source attribution (no post body)."""
    stripped = text.strip()
    if not stripped or "<blockquote" in stripped.lower():
        return None, None, None

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) > 1:
        return None, None, None

    one_line = lines[0] if lines else stripped
    if not _looks_like_summary_header(one_line):
        return None, None, None

    title, username, link = parse_channel_info(one_line)
    if not title:
        return None, None, None

    remainder = one_line
    for pattern in (
        r"^(?:<a[^>]*>👤</a>\s*)+",
        r"^👤\s*",
        r"^<code>[^<]+</code>\s*",
        r"^`[^`]+`\s*",
        r"^\[👤\]\([^\)]+\)\s*",
        r"^<a[^>]+href=\"https://t\.me/[^\"]+\"[^>]*>👤</a>\s*",
    ):
        remainder = re.sub(pattern, "", remainder, count=1, flags=re.I)

    if _body_matches_channel(remainder, title, username):
        return title, username, link
    return None, None, None


def _is_channel_trailer(html: str) -> bool:
    """Emoji / symbols after </code> on the header line (not post body)."""
    if not html or len(html) > 64:
        return False
    plain = strip_html(html) or ""
    if not plain:
        return False
    if re.search(r"[a-zA-Z\u0400-\u04FF]{4,}", plain):
        return False
    return True


def _title_from_code_html(text: str) -> tuple[str | None, str | None]:
    """Parse channel title from summary header HTML, including nested emoji tags."""
    code_match = re.search(r"<code>([\s\S]*?)</code>", text, re.I)
    if not code_match:
        return None, None

    inner_html = code_match.group(1)
    title = (strip_html(inner_html) or "").strip()
    label_html = inner_html

    after_html = text[code_match.end() :].strip()
    after_html = re.split(r"<br", after_html, maxsplit=1, flags=re.I)[0].strip()
    if after_html and _is_channel_trailer(after_html):
        extra = (strip_html(after_html) or "").strip()
        if extra:
            title = f"{title} {extra}".strip()
            label_html = f"{inner_html}{after_html}"

    return title or None, sanitize_channel_label(label_html)


def extract_channel_header_label(raw_header: str | None) -> tuple[str | None, str | None]:
    """Plain title and safe HTML label for «Переслано от» (flags/emoji only, never post body)."""
    if not raw_header or not raw_header.strip():
        return None, None

    header_part = raw_header.strip()
    blockquote = re.search(r"<blockquote", header_part, re.I)
    if blockquote:
        header_part = header_part[: blockquote.start()].strip()

    br_parts = re.split(r"<br\s*/?>\s*", header_part, maxsplit=1, flags=re.I)
    if len(br_parts) == 2 and _looks_like_summary_header(br_parts[0]):
        header_part = br_parts[0].strip()
    elif "\n" in header_part:
        first_line, _, rest = header_part.partition("\n")
        if _looks_like_summary_header(first_line) and not rest.strip():
            header_part = first_line.strip()
        elif _looks_like_summary_header(first_line):
            header_part = first_line.strip()

    header_part = re.sub(r"^(?:<a[^>]*>👤</a>\s*)+", "", header_part, flags=re.I).strip()
    header_part = re.sub(r"^👤\s*", "", header_part).strip()

    title, username, link = parse_channel_info(header_part)
    if not title:
        return None, None

    _, label_html = _title_from_code_html(header_part)
    if not label_html:
        label_html = sanitize_channel_label(title)
    return title, label_html


def parse_channel_info(text: str | None) -> tuple[str | None, str | None, str | None]:
    """Parse summary post header. Returns (channel_title, username, source_link)."""
    if not text:
        return None, None, None

    plain = strip_html(text) or text

    emoji_md = re.search(
        r"\[👤\]\(https://t\.me/([^/)]+)/(\d+)\)\s*`([^`]+)`",
        plain,
    )
    if emoji_md:
        username = emoji_md.group(1).strip()
        msg_id = emoji_md.group(2).strip()
        title = emoji_md.group(3).strip()
        return title, username, f"https://t.me/{username}/{msg_id}"

    paren = re.search(
        r"👤\s*\(https://t\.me/([^/)]+)/(\d+)\)\s*(.+?)\s*(?:\n|$)",
        plain,
    )
    if paren:
        username = paren.group(1).strip()
        msg_id = paren.group(2).strip()
        title = paren.group(3).strip()
        return title, username, f"https://t.me/{username}/{msg_id}"

    md_link = re.search(
        r"👤\s*\[.*?\]\(https://t\.me/([^/)]+)/(\d+)\)\s*(.+?)\s*(?:\n|$)",
        plain,
    )
    if md_link:
        username = md_link.group(1).strip()
        msg_id = md_link.group(2).strip()
        title = md_link.group(3).strip()
        return title, username, f"https://t.me/{username}/{msg_id}"

    code_match = re.search(r"<code>([\s\S]*?)</code>", text, re.I)
    link_match = re.search(r'href="https://t\.me/([^/"]+)/(\d+)"', text)
    if code_match and link_match:
        title = (strip_html(code_match.group(1)) or "").strip()
        after_html = text[code_match.end() :].strip()
        after_html = re.split(r"<br", after_html, maxsplit=1, flags=re.I)[0].strip()
        if after_html and _is_channel_trailer(after_html):
            extra = (strip_html(after_html) or "").strip()
            if extra:
                title = f"{title} {extra}".strip()
        return (
            title or None,
            link_match.group(1).strip(),
            f"https://t.me/{link_match.group(1)}/{link_match.group(2)}",
        )

    if code_match and _looks_like_summary_header(text[: code_match.end() + 1]):
        title, _ = _title_from_code_html(text)
        return title, None, None

    plain_code = re.search(r"^`([^`]{2,120})`", plain)
    if plain_code and _looks_like_summary_header(plain[: plain_code.end()]):
        return plain_code.group(1).strip(), None, None

    if link_match:
        username = link_match.group(1).strip()
        after = re.sub(r"^.*?href=\"[^\"]+\"[^>]*>👤</a>\s*", "", text, count=1)
        after_plain = strip_html(after.split("\n", 1)[0]) or ""
        title = after_plain.strip() if after_plain and after_plain != "👤" else None
        return title, username, f"https://t.me/{username}/{link_match.group(2)}"

    return None, None, None


def extract_channel_from_body_html(html: str | None) -> tuple[str | None, str | None, str | None]:
    """Extract channel attribution embedded in forwarded message HTML (footer link)."""
    if not html:
        return None, None, None

    best = None
    for match in re.finditer(
        r'<a[^>]+href="https://t\.me/c/(\d+)/(\d+)"[^>]*>([^<]+)</a>',
        html,
        re.I,
    ):
        title = unescape(match.group(3)).strip()
        if title and title != "👤":
            best = (title, None, f"https://t.me/c/{match.group(1)}/{match.group(2)}")

    for match in re.finditer(
        r'<a[^>]+href="https://t\.me/([^/"]+)/(\d+)"[^>]*>([^<]+)</a>',
        html,
        re.I,
    ):
        username = match.group(1).strip()
        if username in {"joinchat", "addstickers", "share", "iv"}:
            continue
        title = unescape(match.group(3)).strip()
        if title and title != "👤":
            best = (title, username, f"https://t.me/{username}/{match.group(2)}")

    if best:
        return best

    code_match = re.search(r"<code>([\s\S]*?)</code>", html, re.I)
    if code_match:
        title, _ = _title_from_code_html(html)
        return title, None, None

    return None, None, None


def _split_inline_channel_prefix(text: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Header and body on one line: HTML or markdown summary header + rest."""
    m = re.match(
        r"^\[👤\]\(https://t\.me/([^/)]+)/(\d+)\)\s*`([^`]+)`(?:\s+([\s\S]+))?$",
        text.strip(),
    )
    if m:
        username = m.group(1).strip()
        msg_id = m.group(2).strip()
        title = m.group(3).strip()
        body = m.group(4).strip() if m.group(4) else None
        return title, username, f"https://t.me/{username}/{msg_id}", body or None

    m = re.match(
        r"^(?:<a[^>]*>👤</a>\s*)?<code>([\s\S]*?)</code>\s*(.+)$",
        text,
        flags=re.I | re.S,
    )
    if m:
        title = (strip_html(m.group(1)) or "").strip()
        body = m.group(2).strip()
        _, username, link = parse_channel_info(text)
        return title, username, link, body or None

    m = re.match(r"^`([^`]{2,120})`\s+(.+)$", text, flags=re.S)
    if m:
        return m.group(1).strip(), None, None, m.group(2).strip() or None

    return None, None, None, None


def split_summary_channel_and_body(raw: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    """
    tgsvodka summary posts: first line is always source link + channel name (from worker),
  the rest is the actual post body. Returns (channel_title, username, source_link, body_raw).
    """
    if not raw or not raw.strip():
        return None, None, None, None

    text = raw.strip()
    header_only = _parse_header_only(text)
    if header_only[0]:
        return header_only[0], header_only[1], header_only[2], None

    header_part = text
    body_part: str | None = None

    blockquote = re.search(r"<blockquote>([\s\S]*?)</blockquote>", text, re.I)
    if blockquote:
        body_part = blockquote.group(1).strip() or None
        header_part = text[: blockquote.start()].strip()

    if body_part is None:
        br_parts = re.split(r"<br\s*/?>\s*", text, maxsplit=1, flags=re.I)
        if len(br_parts) == 2 and _looks_like_summary_header(br_parts[0]):
            header_part, body_part = br_parts[0].strip(), br_parts[1].strip() or None

    if body_part is None and "\n" in text:
        first_line, _, rest = text.partition("\n")
        if _looks_like_summary_header(first_line):
            header_part = first_line.strip()
            body_part = rest.strip() or None

    if body_part is None:
        inline = _split_inline_channel_prefix(text)
        if inline[0] is not None or inline[3]:
            return inline

    title, username, link = parse_channel_info(header_part)
    if not title:
        title, username, link = parse_channel_info(text)

    if body_part is None and not blockquote:
        if not title:
            header_only = _parse_header_only(text)
            if header_only[0]:
                return header_only[0], header_only[1], header_only[2], None
            if not _looks_like_summary_header(text):
                body_part = text
        elif not _looks_like_summary_header(text):
            body_part = text

    if body_part and title and _body_matches_channel(body_part, title, username):
        body_part = None

    return title, username, link, body_part


def strip_legacy_header(text: str) -> str:
    _, _, _, body = split_summary_channel_and_body(text)
    if body is not None:
        return body

    stripped = text.strip()
    if _looks_like_summary_header(stripped.splitlines()[0] if stripped else ""):
        inline = _split_inline_channel_prefix(stripped)
        if inline[3]:
            return inline[3]
        if inline[0] and not inline[3]:
            return ""

    lines = text.splitlines()
    while lines:
        line = lines[0].strip()
        if not line:
            lines.pop(0)
            continue
        if re.match(r"^\[👤\]\(https://t\.me/[^)]+\)", line):
            lines.pop(0)
            continue
        if re.match(r"^👤\s*[\[(]", line) or line.startswith("👤"):
            lines.pop(0)
            continue
        if line.startswith("`") and line.endswith("`") and len(line) > 2:
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def markdown_to_html(text: str) -> str:
    text = strip_legacy_header(text)
    text = re.sub(r"\]\s*\n\s*\((https?://[^)\s]+)\)", r"](\1)", text)
    # Some forwarded posts contain a Markdown link inside the text of another
    # Markdown link. Telegram accepts the original entity, but a second parser
    # pass turns it into fragments such as ``[name</a>s](url)``. Keep the outer
    # destination and make the visible label readable before rendering HTML.
    nested_link = re.compile(
        r"\[\[([^\]]+)\]\((https?://[^)]+)\)([^\]]*)\]\((https?://[^)]+)\)"
    )
    while nested_link.search(text):
        text = nested_link.sub(lambda match: f"[{match.group(1)}{match.group(3)}]({match.group(4)})", text)
    result = escape(text)
    result = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", result)
    result = re.sub(r"__(.+?)__", r"<strong>\1</strong>", result)
    result = re.sub(r"\*(.+?)\*", r"<em>\1</em>", result)
    result = re.sub(r"_([^_]+)_", r"<em>\1</em>", result)
    result = re.sub(r"`([^`]+)`", r"<code>\1</code>", result)
    result = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        result,
    )
    result = result.replace("\n", "<br>")
    return result


def cleanup_telegram_html(html: str) -> str:
    """Remove markdown artifacts left after Telethon entity → HTML conversion."""
    if not html:
        return html

    html = re.sub(
        r"<strong>\s*\*\*(.*?)\*\*\s*</strong>",
        r"<strong>\1</strong>",
        html,
        flags=re.S,
    )
    html = re.sub(r"<strong>\s*\*\*(.*?)</strong>", r"<strong>\1</strong>", html, flags=re.S)
    html = re.sub(r"<strong>(.*?)\*\*\s*</strong>", r"<strong>\1</strong>", html, flags=re.S)

    def _clean_em(match: re.Match) -> str:
        inner = re.sub(r"__+", "", match.group(1))
        inner = inner.replace("**", "")
        return f"<em>{inner}</em>"

    html = re.sub(r"<em>\s*(.*?)\s*</em>", _clean_em, html, flags=re.S)
    html = re.sub(r"</em>\s*<em>", "", html)

    html = re.sub(r"^\s*\*\*\s*$", "", html, flags=re.M)
    html = re.sub(r"^\s*__+\s*$", "", html, flags=re.M)
    html = re.sub(r"(?<![\w*])\*\*(?!\*)", "", html)
    html = re.sub(r"__+", "", html)

    # Convert markdown links that may appear inside HTML (e.g. Habr posts).
    # Previously we stripped `](...)` tails which broke links and left odd punctuation.
    html = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        html,
    )

    # If there are still dangling `](...)` sequences after partial corruption,
    # strip only the URL tail but keep readable text.
    html = re.sub(r"\]\((https?://[^)]+)\)", ")", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def is_broken_telegram_html(html: str) -> bool:
    if not html:
        return False
    if "**" in html or "__" in html:
        return True
    if "[</a>" in html or "](http" in html:
        return True
    if re.search(r"\w<(?:em|strong|b|i|a)\b", html):
        return True
    if re.search(r"</(?:em|strong|b|i|a)>\w", html):
        return True
    return False


def html_to_plain_post(text: str) -> str:
    plain = strip_html(text) or text
    plain = re.sub(r"\*\*", "", plain)
    plain = re.sub(r"__+", " ", plain)
    plain = re.sub(r"\n\s*(\[)?Зад[^\n]*", "", plain, flags=re.I)
    plain = re.sub(r"\n\s*\|[^\n]*(Patreon|suggest_bot|svtvnews_bot)[^\n]*", "", plain, flags=re.I)
    return plain.strip()


def format_post_text(text: str | None) -> str | None:
    if not text:
        return None
    text = strip_legacy_header(text)
    if not text:
        return None
    if "<" in text and ">" in text:
        cleaned = cleanup_telegram_html(text)
        if is_broken_telegram_html(cleaned):
            formatted = sanitize_html(markdown_to_html(html_to_plain_post(text)))
        else:
            formatted = sanitize_html(cleaned)
    else:
        formatted = sanitize_html(markdown_to_html(text))

    # Safety net: sometimes Telegram/Telethon yields hybrid markup where '**'/'__'
    # survive the conversions. If they are still present in the final HTML, fall
    # back to plain text → markdown conversion to avoid showing raw markers.
    if formatted and ("**" in formatted or "__" in formatted or "](" in formatted):
        formatted = sanitize_html(markdown_to_html(html_to_plain_post(text)))

    return formatted


def _message_to_raw_html(message) -> str | None:
    # ``message.text`` is already Markdown from Telethon's default parse mode,
    # while entity offsets point into the raw ``message.message``. Mixing the
    # two cut tags in the wrong places and produced fragments such as
    # ``" target="_blank" rel="noopener">`` in the feed.
    text = message.message
    if not text:
        return None

    return render_telegram_entities(text, getattr(message, "entities", None))


def format_source_html(message) -> str | None:
    """HTML of an original channel post or comment.

    Only summary copies carry a «👤 channel» header and Markdown leftovers, so
    an original message is rendered from its entities as is.
    """
    return sanitize_html(_message_to_raw_html(message))


def _extract_body_from_summary_html(full_html: str) -> str:
    _, _, _, body = split_summary_channel_and_body(full_html)
    return body if body is not None else full_html


def format_message_html(message) -> str | None:
    raw_html = _message_to_raw_html(message)
    if not raw_html:
        return None
    _, _, _, body = split_summary_channel_and_body(raw_html)
    if not body:
        return None
    return format_post_text(body)


def extract_display_html(messages) -> str | None:
    for message in messages:
        html = format_message_html(message)
        if html:
            return html
    return None


def is_voice_message(message) -> bool:
    document = getattr(message, "document", None)
    if not document:
        return False
    for attr in getattr(document, "attributes", None) or []:
        if isinstance(attr, DocumentAttributeAudio) and getattr(attr, "voice", False):
            return True
    return False


def media_type_from_telethon_message(message) -> str:
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    document = getattr(message, "document", None)
    if document:
        mime = getattr(document, "mime_type", "") or ""
        if is_voice_message(message):
            return "voice"
        if mime.startswith("video"):
            return "video"
        if mime.startswith("image"):
            return "photo"
        if mime.startswith("audio"):
            return "audio"
        return "document"
    if getattr(message, "media", None):
        cls_name = type(message.media).__name__
        if "Photo" in cls_name:
            return "photo"
        if "Voice" in cls_name:
            return "voice"
        if "Document" in cls_name:
            return "video" if "video" in cls_name.lower() else "document"
    return "text"


def build_media_json(summary_messages) -> dict:
    summary_messages = sorted(summary_messages, key=lambda m: m.id)
    items = []
    for msg in summary_messages:
        media_type = media_type_from_telethon_message(msg)
        if media_type in {"photo", "video", "document", "voice", "audio"}:
            items.append({"type": media_type, "summary_msg_id": msg.id})

    return {
        "summary_msg_ids": [msg.id for msg in summary_messages],
        "items": items,
    }


def resolve_post_media_type(summary_messages) -> str:
    summary_messages = sorted(summary_messages, key=lambda m: m.id)
    if len(summary_messages) > 1:
        return "album"
    return media_type_from_telethon_message(summary_messages[0])
