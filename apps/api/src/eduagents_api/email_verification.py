"""Short-lived, single-use email ownership codes backed by Redis."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from eduagents_core.config import Settings

from .email_utils import normalize_email

EMAIL_CODE_KEY_PREFIX = "email_verify:code:"
EMAIL_CODE_COOLDOWN_PREFIX = "email_verify:cooldown:"
EMAIL_CODE_DAILY_EMAIL_PREFIX = "email_verify:daily:email:"
EMAIL_CODE_DAILY_IP_PREFIX = "email_verify:daily:ip:"

_CONSUME_CODE_LUA = """
local key = KEYS[1]
local expected = ARGV[1]
local max_attempts = tonumber(ARGV[2])
local stored = redis.call('HGET', key, 'digest')
if not stored then
  return 0
end
if stored == expected then
  redis.call('DEL', key)
  return 1
end
local attempts = redis.call('HINCRBY', key, 'attempts', 1)
if attempts >= max_attempts then
  redis.call('DEL', key)
end
return -attempts
"""


def email_hash(email: str) -> str:
    """Stable non-reversible identifier for Redis keys and logs."""

    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


def ip_hash(ip: str) -> str:
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def generate_email_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_email_code(code: str, settings: Settings) -> str:
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        f"eduagents:email-verification:{code}".encode(),
        hashlib.sha256,
    ).hexdigest()


def code_key(email: str) -> str:
    return f"{EMAIL_CODE_KEY_PREFIX}{email_hash(email)}"


def cooldown_key(email: str) -> str:
    return f"{EMAIL_CODE_COOLDOWN_PREFIX}{email_hash(email)}"


def daily_key(prefix: str, value_hash: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"{prefix}{value_hash}:{now.date().isoformat()}"


async def store_email_code(redis, email: str, code: str, settings: Settings) -> None:
    key = code_key(email)
    await redis.hset(
        key,
        mapping={"digest": hash_email_code(code, settings), "attempts": "0"},
    )
    await redis.expire(key, settings.email_code_ttl_seconds)


async def consume_email_code(redis, email: str, code: str, settings: Settings) -> bool:
    result = await redis.eval(
        _CONSUME_CODE_LUA,
        1,
        code_key(email),
        hash_email_code(code, settings),
        settings.email_code_max_attempts,
    )
    return int(result) == 1
