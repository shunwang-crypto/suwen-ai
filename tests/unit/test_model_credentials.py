from __future__ import annotations

import base64
import uuid

import pytest


def _crypto():
    from eduagents_api.model_credentials import decrypt_api_key, encrypt_api_key

    return encrypt_api_key, decrypt_api_key


def test_aes_gcm_round_trip_and_aad_binding():
    encrypt, decrypt = _crypto()
    user_id = uuid.uuid4()
    key = b"0123456789abcdef0123456789abcdef"
    ciphertext, nonce = encrypt(
        "sk-test-key-1234567890",
        user_id=user_id,
        provider="deepseek",
        version=7,
        master_key=key,
    )
    assert b"sk-test-key-1234567890" not in ciphertext
    assert decrypt(
        ciphertext,
        nonce,
        user_id=user_id,
        provider="deepseek",
        version=7,
        master_key=key,
    ) == "sk-test-key-1234567890"
    with pytest.raises(ValueError):
        decrypt(
            ciphertext,
            nonce,
            user_id=uuid.uuid4(),
            provider="deepseek",
            version=7,
            master_key=key,
        )


def test_wrong_key_nonce_and_plaintext_are_rejected_without_fallback():
    encrypt, decrypt = _crypto()
    key = b"0123456789abcdef0123456789abcdef"
    ciphertext, nonce = encrypt(
        "sk-test-key-1234567890",
        user_id="user-a",
        provider="deepseek",
        version=1,
        master_key=key,
    )
    with pytest.raises(ValueError):
        decrypt(ciphertext, nonce, user_id="user-a", provider="deepseek", version=1, master_key=b"x" * 32)
    with pytest.raises(ValueError):
        decrypt(b"sk-test-key-1234567890", nonce, user_id="user-a", provider="deepseek", version=1, master_key=key)


def test_master_key_requires_exactly_32_decoded_bytes():
    from eduagents_api.model_credentials import decode_master_key

    encoded = base64.urlsafe_b64encode(b"x" * 32).decode()
    assert decode_master_key(encoded) == b"x" * 32
    with pytest.raises(ValueError):
        decode_master_key(base64.urlsafe_b64encode(b"weak").decode())
