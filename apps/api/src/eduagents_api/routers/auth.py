from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eduagents_core.config import Settings
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, update

from ..db import plain_session
from ..deps import AuthContext, current_user, get_app_settings
from ..models import RefreshToken, User
from ..schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut
from ..security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = get_logger("auth")

_REFRESH_COOKIE = "edu_refresh"


async def _auth_rate_limit(
    request: Request, settings: Settings, *, bucket: str = "auth", per_minute: int | None = None
) -> None:
    limiter = request.app.state.rate_limiter
    ip = request.client.host if request.client else "unknown"
    limit = per_minute if per_minute is not None else settings.auth_rate_limit_per_minute
    if not await limiter.allow(f"ip:{ip}:{bucket}", limit):
        raise HTTPException(
            status_code=429, detail="RATE_LIMITED", headers={"Retry-After": "30"}
        )


def _set_refresh_cookie(
    response: Response, settings: Settings, raw: str
) -> None:
    response.set_cookie(
        _REFRESH_COOKIE,
        raw,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.app_env == "prod",
        path="/api/auth",
    )


async def _issue_refresh(
    user_id, settings: Settings, rotated_from: str | None = None
) -> str:
    raw, token_hash = new_refresh_token()
    async with plain_session() as session:
        session.add(
            RefreshToken(
                user_id=user_id,
                token_hash=token_hash,
                expires_at=datetime.now(UTC)
                + timedelta(seconds=settings.refresh_token_ttl_seconds),
                rotated_from=rotated_from,
            )
        )
    return raw


@router.post("/register", response_model=UserOut, status_code=201)
async def register(
    body: RegisterRequest,
    request: Request,
    settings: Settings = Depends(get_app_settings),
):
    await _auth_rate_limit(request, settings)
    async with plain_session() as session:
        existing = await session.scalar(select(User).where(User.email == body.email))
        if existing:
            raise HTTPException(status_code=409, detail="EMAIL_EXISTS")
        user = User(
            email=body.email,
            password_hash=hash_password(body.password, settings.bcrypt_rounds),
            role="student",
        )
        session.add(user)
        await session.flush()
        log.info("user_registered", user_id=str(user.id))
        return UserOut(
            id=user.id, email=user.email, role=user.role, created_at=user.created_at
        )


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
):
    await _auth_rate_limit(request, settings)
    async with plain_session() as session:
        user = await session.scalar(select(User).where(User.email == body.email))
        if user is None or not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="BAD_CREDENTIALS")
        token, _jti, expires_at = create_access_token(
            settings, user_id=user.id, tenant_id=user.id, role=user.role
        )
        user_id = user.id
    raw = await _issue_refresh(user_id, settings)
    _set_refresh_cookie(response, settings, raw)
    log.info("user_login", user_id=str(user_id))
    return TokenResponse(access_token=token, expires_at=expires_at)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
):
    # refresh 走独立宽桶: 整页刷新即触发静默续期, 与撞库面 (login/register) 风险不同
    await _auth_rate_limit(
        request, settings,
        bucket="auth_refresh",
        per_minute=settings.auth_refresh_rate_limit_per_minute,
    )
    raw = request.cookies.get(_REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="REFRESH_INVALID")
    token_hash = hash_refresh_token(raw)
    now = datetime.now(UTC)
    replayed_user_id = None
    async with plain_session() as session:
        row = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        if row is None:
            raise HTTPException(status_code=401, detail="REFRESH_INVALID")
        if row.revoked:
            # 重放检测: 已轮换的旧 token 再次出现。撤销必须落库后才能抛 401,
            # 不能在本事务内 raise (session.begin 会把撤销一起回滚)。
            replayed_user_id = row.user_id
        elif row.expires_at.replace(tzinfo=UTC) < now:
            raise HTTPException(status_code=401, detail="REFRESH_INVALID")
        else:
            row.revoked = True  # rotate: old one is single-use
            user = await session.get(User, row.user_id)
            if user is None:
                raise HTTPException(status_code=401, detail="REFRESH_INVALID")
            token, _jti, expires_at = create_access_token(
                settings, user_id=user.id, tenant_id=user.id, role=user.role
            )
            user_id = user.id
    if replayed_user_id is not None:
        async with plain_session() as session:
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == replayed_user_id)
                .values(revoked=True)
            )
        log.warning("refresh_replay_detected", user_id=str(replayed_user_id))
        raise HTTPException(status_code=401, detail="REFRESH_INVALID")
    new_raw = await _issue_refresh(user_id, settings, rotated_from=token_hash)
    _set_refresh_cookie(response, settings, new_raw)
    return TokenResponse(access_token=token, expires_at=expires_at)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    ctx: AuthContext = Depends(current_user),
    settings: Settings = Depends(get_app_settings),
):
    # access token -> Redis 黑名单, TTL 为剩余有效期
    redis = request.app.state.redis
    await redis.setex(f"bl:{ctx.jti}", settings.access_token_ttl_seconds, "1")
    raw = request.cookies.get(_REFRESH_COOKIE)
    if raw:
        async with plain_session() as session:
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.token_hash == hash_refresh_token(raw))
                .values(revoked=True)
            )
    response.delete_cookie(_REFRESH_COOKIE, path="/api/auth")
    return Response(status_code=204)


@router.get("/me", response_model=UserOut)
async def me(ctx: AuthContext = Depends(current_user)):
    async with plain_session() as session:
        user = await session.get(User, ctx.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="INVALID_TOKEN")
        return UserOut(
            id=user.id, email=user.email, role=user.role, created_at=user.created_at
        )
