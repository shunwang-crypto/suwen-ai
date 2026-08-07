from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from eduagents_core.config import Settings

# 修正#2: 已知弱/默认 secret 黑名单 (config.py 默认值必须在列)
_KNOWN_WEAK_JWT_SECRETS = frozenset(
    {"dev-secret-change-me", "changeme", "change-me", "secret", "dev", "test"}
)
_MIN_JWT_SECRET_LEN = 32
# 多进程/多机部署的系统时钟可能有秒级漂移。仅容忍很小的有界窗口，
# 既避免刚签发 token 因 iat 落在“未来”被拒，也不放宽签名、算法或长期过期校验。
_JWT_CLOCK_SKEW_SECONDS = 5


def assert_production_secrets(settings: Settings) -> None:
    """启动 fail-fast (修正#2): prod 环境拒绝默认/弱 JWT secret。

    HS256 + 已知 secret = 任何拿到 .env 的人可自签 admin token。
    必须在 lifespan 首行调用 (main.py 归属主线, 接线见报告):
        assert_production_secrets(settings)
    """
    if settings.app_env != "prod":
        return
    secret = settings.jwt_secret or ""
    if secret in _KNOWN_WEAK_JWT_SECRETS or len(secret) < _MIN_JWT_SECRET_LEN:
        raise RuntimeError(
            "FATAL: APP_ENV=prod 但 JWT_SECRET 是默认值或长度不足 "
            f"{_MIN_JWT_SECRET_LEN} 字符 — 拒绝启动。请生成强随机 secret "
            "(例如 `openssl rand -base64 48`) 并通过 secrets 注入。"
        )
    if settings.user_model_credentials_enabled:
        try:
            key = base64.urlsafe_b64decode(
                settings.user_model_credentials_encryption_key.encode("ascii")
            )
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise RuntimeError(
                "FATAL: BYOK 已启用但 USER_MODEL_CREDENTIALS_ENCRYPTION_KEY 不是合法 base64"
            ) from exc
        if len(key) != 32:
            raise RuntimeError(
                "FATAL: BYOK 已启用但 USER_MODEL_CREDENTIALS_ENCRYPTION_KEY 不是 32 字节"
            )


def hash_password(password: str, rounds: int = 12) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=rounds)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def create_access_token(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role: str,
    now: datetime | None = None,
) -> tuple[str, str, datetime]:
    """Returns (token, jti, expires_at). jti enables the logout blacklist."""
    now = now or datetime.now(UTC)
    jti = uuid.uuid4().hex
    expires_at = now + timedelta(seconds=settings.access_token_ttl_seconds)
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, jti, expires_at


def decode_access_token(settings: Settings, token: str) -> dict:
    """Raises jwt.PyJWTError on any problem (expiry, signature, malformed)."""
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        leeway=_JWT_CLOCK_SKEW_SECONDS,
    )


def new_refresh_token() -> tuple[str, str]:
    """Returns (raw_token_for_cookie, sha256_hash_for_db). Raw is never stored."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_password_reset_token() -> tuple[str, str]:
    """密码找回 token: 返回 (raw_for_email_link, sha256_hash_for_db)。

    32 字节 urlsafe 随机串, 原文只出现在邮件链接里, 落库仅 sha256
    (与 refresh token 同口径: 拖库拿不到可用 token)。
    """
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()
