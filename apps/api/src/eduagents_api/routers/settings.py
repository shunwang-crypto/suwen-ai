"""Private user settings, including encrypted DeepSeek BYOK configuration."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from eduagents_core.config import Settings
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..model_credentials import (
    allowed_models,
    decode_master_key,
    encrypt_api_key,
    public_credential,
    validate_api_key,
    validate_models,
)
from ..models import UserModelCredential

router = APIRouter(prefix="/api/settings", tags=["settings"])
log = get_logger("settings")


class ModelCredentialPut(BaseModel):
    provider: Literal["deepseek"] = "deepseek"
    api_key: str = Field(min_length=19, max_length=260)
    normal_model: str = Field(min_length=1, max_length=128)
    reasoning_model: str = Field(min_length=1, max_length=128)
    fallback_to_platform: bool = False

    @field_validator("api_key")
    @classmethod
    def key_shape(cls, value: str) -> str:
        return validate_api_key(value)


class ModelCredentialTest(BaseModel):
    provider: Literal["deepseek"] = "deepseek"
    api_key: str = Field(min_length=19, max_length=260)
    model: str = Field(min_length=1, max_length=128)

    @field_validator("api_key")
    @classmethod
    def key_shape(cls, value: str) -> str:
        return validate_api_key(value)


class ModelCredentialPatch(BaseModel):
    enabled: bool | None = None
    fallback_to_platform: bool | None = None
    normal_model: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_model: str | None = Field(default=None, min_length=1, max_length=128)


def _error(code: str, status: int = 422) -> HTTPException:
    messages = {
        "BYOK_DISABLED": "自带模型服务当前未开放",
        "BYOK_KEY_INVALID": "DeepSeek API Key 格式无效或验证失败",
        "BYOK_MODEL_NOT_ALLOWED": "所选模型不在服务端允许列表中",
        "BYOK_BALANCE_INSUFFICIENT": "DeepSeek 账户余额不足",
        "BYOK_RATE_LIMITED": "DeepSeek 请求过于频繁，请稍后重试",
        "BYOK_PROVIDER_UNAVAILABLE": "DeepSeek 服务暂时不可用",
        "BYOK_NOT_CONFIGURED": "尚未配置自带模型服务",
    }
    return HTTPException(status_code=status, detail={"code": code, "message": messages[code]})


def _require_enabled(settings: Settings) -> None:
    if not settings.user_model_credentials_enabled:
        raise _error("BYOK_DISABLED", 503)


async def _controlled_test(api_key: str, model: str, settings: Settings) -> None:
    """Perform a short provider check; stub/replay intentionally do not network."""
    if settings.gateway_mode in {"stub", "replay"}:
        return
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=settings.deepseek_byok_base_url,
        timeout=min(settings.gateway_timeout_seconds, 8.0),
        max_retries=0,
    )
    try:
        await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
        )
    except AuthenticationError:
        raise _error("BYOK_KEY_INVALID") from None
    except RateLimitError:
        raise _error("BYOK_RATE_LIMITED", 429) from None
    except APIStatusError as exc:
        if exc.status_code == 402:
            raise _error("BYOK_BALANCE_INSUFFICIENT", 402) from None
        raise _error("BYOK_PROVIDER_UNAVAILABLE", 503) from None
    except (APIConnectionError, APITimeoutError):
        raise _error("BYOK_PROVIDER_UNAVAILABLE", 503) from None
    finally:
        await client.close()


async def _load_row(ctx: AuthContext) -> UserModelCredential | None:
    # Credentials belong to the authenticated user, not to the currently
    # selected organization tenant.  Using the user as the RLS subject keeps
    # a member's BYOK usable in an org context without granting access to any
    # other user's credential.
    async with tenant_session(ctx.user_id) as session:
        return await session.scalar(
            select(UserModelCredential).where(
                UserModelCredential.user_id == ctx.user_id,
                UserModelCredential.provider == "deepseek",
            )
        )


@router.get("/model-credential")
async def get_model_credential(
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    row = await _load_row(ctx) if settings.user_model_credentials_enabled else None
    return {
        **public_credential(row),
        "server_enabled": settings.user_model_credentials_enabled,
        "allowed_models": list(allowed_models(settings)),
    }


@router.post("/model-credential/test")
async def test_model_credential(
    body: ModelCredentialTest,
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    _require_enabled(settings)
    if body.model not in allowed_models(settings):
        raise _error("BYOK_MODEL_NOT_ALLOWED")
    await _controlled_test(body.api_key, body.model, settings)
    return {"ok": True, "provider": "deepseek", "model": body.model}


@router.put("/model-credential")
async def put_model_credential(
    body: ModelCredentialPut,
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    _require_enabled(settings)
    try:
        validate_models(settings, body.normal_model, body.reasoning_model)
    except ValueError as exc:
        raise _error(str(exc)) from None
    await _controlled_test(body.api_key, body.normal_model, settings)
    try:
        encrypted, nonce = encrypt_api_key(
            body.api_key,
            user_id=ctx.user_id,
            provider=body.provider,
            version=settings.user_model_credentials_key_version,
            master_key=decode_master_key(settings.user_model_credentials_encryption_key),
        )
    except ValueError:
        raise _error("BYOK_KEY_INVALID") from None
    now = datetime.now(UTC)
    async with tenant_session(ctx.user_id) as session:
        row = await session.scalar(
            select(UserModelCredential).where(
                UserModelCredential.user_id == ctx.user_id,
                UserModelCredential.provider == body.provider,
            ).with_for_update()
        )
        if row is None:
            row = UserModelCredential(
                id=uuid.uuid4(),
                user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                provider=body.provider,
            )
            session.add(row)
        row.encrypted_api_key = encrypted
        row.encryption_nonce = nonce
        row.encryption_key_version = settings.user_model_credentials_key_version
        row.key_last_four = body.api_key[-4:]
        row.enabled = True
        row.fallback_to_platform = body.fallback_to_platform
        row.normal_model = body.normal_model
        row.reasoning_model = body.reasoning_model
        row.last_validated_at = now
        row.last_validation_status = "valid"
        row.last_error_code = None
        await session.flush()
        return {**public_credential(row), "server_enabled": True, "allowed_models": list(allowed_models(settings))}


@router.patch("/model-credential")
async def patch_model_credential(
    body: ModelCredentialPatch,
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    _require_enabled(settings)
    async with tenant_session(ctx.user_id) as session:
        row = await session.scalar(
            select(UserModelCredential).where(
                UserModelCredential.user_id == ctx.user_id,
                UserModelCredential.provider == "deepseek",
            ).with_for_update()
        )
        if row is None:
            raise _error("BYOK_NOT_CONFIGURED", 404)
        if body.normal_model is not None or body.reasoning_model is not None:
            normal = body.normal_model or row.normal_model
            reasoning = body.reasoning_model or row.reasoning_model
            try:
                validate_models(settings, normal, reasoning)
            except ValueError as exc:
                raise _error(str(exc)) from None
            row.normal_model, row.reasoning_model = normal, reasoning
        if body.enabled is not None:
            row.enabled = body.enabled
        if body.fallback_to_platform is not None:
            row.fallback_to_platform = body.fallback_to_platform
        await session.flush()
        return {**public_credential(row), "server_enabled": True, "allowed_models": list(allowed_models(settings))}


@router.delete("/model-credential", status_code=204)
async def delete_model_credential(
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    _require_enabled(settings)
    async with tenant_session(ctx.user_id) as session:
        row = await session.scalar(
            select(UserModelCredential).where(
                UserModelCredential.user_id == ctx.user_id,
                UserModelCredential.provider == "deepseek",
            ).with_for_update()
        )
        if row is not None:
            await session.delete(row)
