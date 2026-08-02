"""BYOK credential encryption, validation and request-scoped resolution.

This module is the only API-side code allowed to turn the encrypted database
value back into provider authentication material.  AES-GCM authenticates both
the ciphertext and the stable user/provider binding (AAD), so a row copied to
another account cannot be decrypted successfully.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eduagents_core.config import Settings
from eduagents_core.gateway import (
    ChatRequest,
    CredentialSource,
    GatewayError,
    ResolvedCredential,
)
from sqlalchemy import select

from .db import tenant_session
from .models import UserModelCredential

_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_-]{16,256}$")
_NONCE_BYTES = 12
_KEY_BYTES = 32
_PROVIDER = "deepseek"


class CredentialConfigError(ValueError):
    """The server-side encryption configuration is missing or malformed."""


def decode_master_key(value: str) -> bytes:
    if not value:
        raise CredentialConfigError("USER_MODEL_CREDENTIALS_ENCRYPTION_KEY is required")
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise CredentialConfigError("BYOK encryption key must be base64") from exc
    if len(decoded) != _KEY_BYTES:
        raise CredentialConfigError("BYOK encryption key must decode to 32 bytes")
    return decoded


def _aad(user_id: uuid.UUID | str, provider: str, version: int) -> bytes:
    return f"eduagents:user-model-credential:{user_id}:{provider}:v{version}".encode()


def encrypt_api_key(
    api_key: str,
    *,
    user_id: uuid.UUID | str,
    provider: str,
    version: int,
    master_key: bytes,
) -> tuple[bytes, bytes]:
    if len(master_key) != _KEY_BYTES:
        raise CredentialConfigError("BYOK encryption key must be 32 bytes")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ciphertext = AESGCM(master_key).encrypt(nonce, api_key.encode("utf-8"), _aad(user_id, provider, version))
    return ciphertext, nonce


def decrypt_api_key(
    ciphertext: bytes,
    nonce: bytes,
    *,
    user_id: uuid.UUID | str,
    provider: str,
    version: int,
    master_key: bytes,
) -> str:
    if len(master_key) != _KEY_BYTES or len(nonce) != _NONCE_BYTES:
        raise CredentialConfigError("invalid BYOK encryption material")
    try:
        value = AESGCM(master_key).decrypt(nonce, ciphertext, _aad(user_id, provider, version))
    except (InvalidTag, ValueError, TypeError) as exc:
        # Never try plaintext as a compatibility fallback.
        raise CredentialConfigError("BYOK credential cannot be decrypted") from exc
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialConfigError("BYOK credential is not valid UTF-8") from exc


def validate_api_key(api_key: str) -> str:
    normalized = api_key.strip()
    if not _KEY_RE.fullmatch(normalized):
        raise ValueError("BYOK_KEY_INVALID")
    return normalized


def allowed_models(settings: Settings) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(item.strip() for item in settings.deepseek_byok_allowed_models.split(",") if item.strip()))
    return values or ("deepseek-chat", "deepseek-reasoner")


def validate_models(settings: Settings, normal_model: str, reasoning_model: str) -> None:
    allowed = allowed_models(settings)
    if normal_model not in allowed or reasoning_model not in allowed:
        raise ValueError("BYOK_MODEL_NOT_ALLOWED")


def _stable_error(code: str) -> GatewayError:
    messages = {
        "BYOK_KEY_INVALID": "DeepSeek API Key 格式无效",
        "BYOK_PROVIDER_UNAVAILABLE": "DeepSeek 服务暂时不可用",
        "BYOK_RATE_LIMITED": "DeepSeek 请求过于频繁，请稍后重试",
        "BYOK_BALANCE_INSUFFICIENT": "DeepSeek 账户余额不足",
        "BYOK_MODEL_NOT_ALLOWED": "所选模型不在服务端允许列表中",
        "BYOK_DISABLED": "自带模型服务当前未启用",
    }
    return GatewayError(code, messages.get(code, "用户模型服务不可用"))


async def resolve_user_credential(
    req: ChatRequest, settings: Settings, *, session_factory=None
) -> ResolvedCredential | None:
    """Resolve the latest enabled credential without exposing it to core state.

    ``None`` means platform credentials.  Platform-only requests never query
    this table.  The tenant session is keyed by the authenticated user so an
    organization tenant cannot broaden the credential lookup.
    """
    if req.credential_policy != "text" or not req.user_id or not settings.user_model_credentials_enabled:
        return None
    try:
        user_id = uuid.UUID(req.user_id)
    except ValueError:
        return None
    factory = session_factory or tenant_session
    async with factory(user_id) as session:
        row = await session.scalar(
            select(UserModelCredential).where(
                UserModelCredential.user_id == user_id,
                UserModelCredential.provider == _PROVIDER,
                UserModelCredential.enabled.is_(True),
            )
        )
    if row is None:
        return None
    try:
        api_key = decrypt_api_key(
            row.encrypted_api_key,
            row.encryption_nonce,
            user_id=row.user_id,
            provider=row.provider,
            version=row.encryption_key_version,
            master_key=decode_master_key(settings.user_model_credentials_encryption_key),
        )
    except CredentialConfigError:
        raise _stable_error("BYOK_KEY_INVALID") from None
    model = row.reasoning_model if req.tier.value == "strong" else row.normal_model
    return ResolvedCredential(
        source=CredentialSource.user,
        provider=row.provider,
        api_key=api_key,
        base_url=settings.deepseek_byok_base_url,
        model=model,
        fallback_model=row.normal_model if req.tier.value == "strong" else "",
        fallback_to_platform=row.fallback_to_platform,
        credential_id=str(row.id),
        key_version=row.encryption_key_version,
    )


@dataclass(frozen=True)
class CredentialPublic:
    configured: bool
    provider: str | None
    enabled: bool | None
    fallback_to_platform: bool | None
    key_last_four: str | None
    normal_model: str | None
    reasoning_model: str | None
    last_validated_at: datetime | None
    last_validation_status: str | None
    last_error_code: str | None


def public_credential(row: UserModelCredential | None) -> dict:
    if row is None:
        return {
            "configured": False,
            "provider": _PROVIDER,
            "enabled": False,
            "fallback_to_platform": False,
            "key_last_four": None,
            "normal_model": None,
            "reasoning_model": None,
            "last_validated_at": None,
            "last_validation_status": None,
            "last_error_code": None,
        }
    return {
        "configured": True,
        "provider": row.provider,
        "enabled": row.enabled,
        "fallback_to_platform": row.fallback_to_platform,
        "key_last_four": row.key_last_four,
        "normal_model": row.normal_model,
        "reasoning_model": row.reasoning_model,
        "last_validated_at": row.last_validated_at,
        "last_validation_status": row.last_validation_status,
        "last_error_code": row.last_error_code,
    }


def validation_now() -> datetime:
    return datetime.now(UTC)
