import re

import pytest
from eduagents_api.email_utils import normalize_email
from eduagents_api.email_verification import (
    code_key,
    consume_email_code,
    generate_email_code,
    hash_email_code,
    store_email_code,
)
from eduagents_core.config import Settings
from fakeredis import FakeAsyncRedis


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def settings():
    return Settings(_env_file=None, jwt_secret="test-secret-for-email-code")


def test_code_is_six_digits_and_cryptographic_digest_is_not_plaintext(settings):
    code = generate_email_code()
    assert re.fullmatch(r"\d{6}", code)
    assert hash_email_code(code, settings) != code


def test_email_normalization_is_shared_and_provider_neutral():
    assert normalize_email("  User@Example.COM ") == "user@example.com"
    assert normalize_email("User+tag@Example.COM") == "user+tag@example.com"


async def test_code_is_single_use_and_redis_does_not_store_plaintext(redis, settings):
    await store_email_code(redis, "User@Example.com", "123456", settings)
    assert await redis.hget(code_key("user@example.com"), "digest") != "123456"
    assert await consume_email_code(redis, " user@example.com ", "123456", settings)
    assert not await consume_email_code(redis, "USER@example.com", "123456", settings)


async def test_wrong_code_expires_after_max_attempts(redis, settings):
    await store_email_code(redis, "u@example.com", "123456", settings)
    for _ in range(settings.email_code_max_attempts):
        assert not await consume_email_code(redis, "u@example.com", "000000", settings)
    assert not await consume_email_code(redis, "u@example.com", "123456", settings)


async def test_new_code_replaces_old_code_and_has_ttl(redis, settings):
    await store_email_code(redis, "u@example.com", "123456", settings)
    await store_email_code(redis, "u@example.com", "654321", settings)
    assert not await consume_email_code(redis, "u@example.com", "123456", settings)
    assert await consume_email_code(redis, "u@example.com", "654321", settings)
    await store_email_code(redis, "u@example.com", "123456", settings)
    assert 0 < await redis.ttl(code_key("u@example.com")) <= settings.email_code_ttl_seconds
