"""P1-13 虚拟时钟护栏: is_enabled 开关矩阵 + 偏移键 30 天 TTL。"""

from __future__ import annotations

import fakeredis.aioredis
from eduagents_api import timectl


def test_is_enabled_defaults_by_app_env(monkeypatch):
    monkeypatch.delenv("ENABLE_TIME_TRAVEL", raising=False)
    monkeypatch.setenv("APP_ENV", "dev")
    assert timectl.is_enabled()
    monkeypatch.setenv("APP_ENV", "test")
    assert timectl.is_enabled()
    monkeypatch.setenv("APP_ENV", "prod")
    assert not timectl.is_enabled()


def test_is_enabled_explicit_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("ENABLE_TIME_TRAVEL", "1")
    assert timectl.is_enabled()
    monkeypatch.setenv("ENABLE_TIME_TRAVEL", "true")
    assert timectl.is_enabled()
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ENABLE_TIME_TRAVEL", "0")
    assert not timectl.is_enabled()
    monkeypatch.setenv("ENABLE_TIME_TRAVEL", "off")
    assert not timectl.is_enabled()


async def test_offset_key_gets_ttl_on_advance_and_set():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    new_offset = await timectl.advance(redis, 3.0)
    assert new_offset == 3.0 * 86400.0
    ttl = await redis.ttl(timectl.OFFSET_KEY)
    assert 0 < ttl <= timectl.OFFSET_TTL_SECONDS

    await timectl.set_offset(redis, 120.0)
    assert await timectl.get_offset(redis) == 120.0
    ttl = await redis.ttl(timectl.OFFSET_KEY)
    assert 0 < ttl <= timectl.OFFSET_TTL_SECONDS
