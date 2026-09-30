import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, Query

from config import ALLOWED_USER_ID, BOT_TOKEN, OWNER_USER_ID
from db.database import SessionLocal
from db.repository import UserRepository

# Users already stored by this process; saves a write on every request.
_KNOWN_USERS: set[int] = set()


def validate_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not configured")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash in initData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid initData signature")

    auth_date = int(parsed.get("auth_date", "0"))
    if time.time() - auth_date > 86400:
        raise HTTPException(status_code=401, detail="initData expired")

    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Missing user in initData")

    user = json.loads(user_raw)
    user["is_owner"] = bool(OWNER_USER_ID) and user.get("id") == OWNER_USER_ID
    if ALLOWED_USER_ID and user.get("id") != ALLOWED_USER_ID:
        raise HTTPException(status_code=403, detail="Access denied")

    return user


async def remember_user(user: dict) -> None:
    user_id = user.get("id")
    if not user_id or user_id in _KNOWN_USERS:
        return
    async with SessionLocal() as session:
        await UserRepository(session).upsert(
            user_id, first_name=user.get("first_name"), username=user.get("username")
        )
    _KNOWN_USERS.add(user_id)


def _extract_init_data(authorization: str | None, init_data: str | None) -> str:
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "tma" and value:
            return value
    if init_data:
        return init_data
    raise HTTPException(status_code=401, detail="Missing Authorization header or init_data query")


async def get_current_user(
    authorization: str | None = Header(default=None),
    init_data: str | None = Query(default=None, alias="init_data"),
) -> dict:
    user = validate_init_data(_extract_init_data(authorization, init_data))
    await remember_user(user)
    return user
