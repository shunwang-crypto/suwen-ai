import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from eduagents_api.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from eduagents_core.config import Settings

settings = Settings(
    _env_file=None, jwt_secret="test-secret-with-at-least-32-bytes-length!"
)


def test_password_hash_roundtrip():
    hashed = hash_password("s3cret-pass", rounds=4)  # low rounds: keep test fast
    assert hashed != "s3cret-pass"
    assert verify_password("s3cret-pass", hashed)
    assert not verify_password("wrong-pass", hashed)


def test_password_verify_garbage_hash_is_false():
    assert not verify_password("x", "not-a-bcrypt-hash")


def test_access_token_roundtrip_claims():
    uid, tid = uuid.uuid4(), uuid.uuid4()
    token, jti, expires_at = create_access_token(
        settings, user_id=uid, tenant_id=tid, role="student"
    )
    payload = decode_access_token(settings, token)
    assert payload["sub"] == str(uid)
    assert payload["tid"] == str(tid)
    assert payload["role"] == "student"
    assert payload["jti"] == jti
    assert expires_at > datetime.now(UTC)


def test_expired_token_rejected():
    token, _, _ = create_access_token(
        settings,
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role="student",
        now=datetime.now(UTC) - timedelta(days=1),
    )
    with pytest.raises(pyjwt.ExpiredSignatureError):
        decode_access_token(settings, token)


def test_tampered_token_rejected():
    token, _, _ = create_access_token(
        settings, user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="student"
    )
    with pytest.raises(pyjwt.PyJWTError):
        decode_access_token(settings, token + "x")


def test_refresh_token_hash_is_stable_and_raw_unstored():
    raw, hashed = new_refresh_token()
    assert hashed == hash_refresh_token(raw)
    assert raw not in hashed
    assert len(hashed) == 64
