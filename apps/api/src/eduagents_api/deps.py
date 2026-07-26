from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt as pyjwt
from eduagents_core.config import Settings, get_settings
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    jti: str


def get_app_settings() -> Settings:
    return get_settings()


def get_redis(request: Request):
    return request.app.state.redis


def get_gateway(request: Request):
    return request.app.state.gateway


def get_rate_limiter(request: Request):
    return request.app.state.rate_limiter


async def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_app_settings),
) -> AuthContext:
    if creds is None:
        raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
    try:
        payload = decode_access_token(settings, creds.credentials)
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=401, detail="INVALID_TOKEN") from None
    jti = payload.get("jti", "")
    # logout blacklist (Redis, TTL = remaining token validity)
    redis = request.app.state.redis
    if jti and await redis.exists(f"bl:{jti}"):
        raise HTTPException(status_code=401, detail="TOKEN_REVOKED")
    ctx = AuthContext(
        user_id=uuid.UUID(payload["sub"]),
        tenant_id=uuid.UUID(payload["tid"]),
        role=payload.get("role", "student"),
        jti=jti,
    )
    request.state.auth = ctx
    return ctx


async def require_admin(ctx: AuthContext = Depends(current_user)) -> AuthContext:
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    return ctx


async def require_teacher(ctx: AuthContext = Depends(current_user)) -> AuthContext:
    if ctx.role not in ("teacher", "admin"):
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    return ctx


async def api_rate_limited(
    request: Request,
    ctx: AuthContext = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
) -> AuthContext:
    limiter = request.app.state.rate_limiter
    if not await limiter.allow(f"user:{ctx.user_id}:api", settings.api_rate_limit_per_minute):
        raise HTTPException(
            status_code=429, detail="RATE_LIMITED", headers={"Retry-After": "10"}
        )
    return ctx
