import re

import bleach

ALLOWED_TAGS = ["a", "b", "strong", "i", "em", "u", "s", "strike", "del", "code", "pre", "blockquote", "br", "p"]
ALLOWED_ATTRS = {"a": ["href", "title", "target", "rel"]}

CHANNEL_LABEL_TAGS = ["tg-emoji", "span"]
CHANNEL_LABEL_ATTRS = {"tg-emoji": ["emoji-id"], "span": ["class"]}


def sanitize_html(html: str | None) -> str | None:
    if not html:
        return None
    cleaned = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
    return cleaned.strip() or None


def sanitize_channel_label(html: str | None) -> str | None:
    if not html:
        return None
    tags = ALLOWED_TAGS + CHANNEL_LABEL_TAGS
    attrs = {**ALLOWED_ATTRS, **CHANNEL_LABEL_ATTRS}
    cleaned = bleach.clean(html, tags=tags, attributes=attrs, strip=True)
    return cleaned.strip() or None
