"""Starter packs for the first screen, so a new user never sees an empty feed.

Hand-picked public channels; edit freely. Every username here is joined by the
service account ahead of time while the feed is open to everyone (see ``queue_catalog_channels``).
"""

CATALOG = [
    {"id": "news", "title": "Новости", "emoji": "📰", "channels": ["rian_ru", "tass_agency", "meduzalive", "bbcrussian"]},
    {"id": "tech", "title": "Технологии", "emoji": "💻", "channels": ["tginfo", "habr_com", "vcnews", "durov"]},
    {"id": "business", "title": "Бизнес и деньги", "emoji": "💼", "channels": ["rbc_news", "banksta", "thebell_io", "forbesrussia"]},
    {"id": "science", "title": "Наука", "emoji": "🔭", "channels": ["naukaru", "nplusone", "techno_yandex", "popmech"]},
]


def catalog_payload() -> list[dict]:
    return [{key: pack[key] for key in ("id", "title", "emoji", "channels")} for pack in CATALOG]


def get_pack(pack_id: str) -> dict | None:
    return next((pack for pack in CATALOG if pack["id"] == pack_id), None)


def all_catalog_usernames() -> list[str]:
    seen: list[str] = []
    for pack in CATALOG:
        for username in pack["channels"]:
            if username not in seen:
                seen.append(username)
    return seen
