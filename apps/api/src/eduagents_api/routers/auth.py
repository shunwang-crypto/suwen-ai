from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
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


def _email_hash(email: str) -> str:
    """归一化 email 的 sha256 (日志/限流键用, 不落明文)。"""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


async def _auth_rate_limit(
    request: Request,
    settings: Settings,
    *,
    bucket: str = "auth",
    per_minute: int | None = None,
    extra_keys: tuple[str, ...] = (),
) -> None:
    """IP 桶 + 可选附加桶 (账号维度) 并联: 任一超限即 429 (P0-1)。

    两个桶都无条件计数 (不短路), 攻击者无法靠打满 IP 桶来规避账号桶计数。
    """
    limiter = request.app.state.rate_limiter
    ip = request.client.host if request.client else "unknown"
    limit = per_minute if per_minute is not None else settings.auth_rate_limit_per_minute
    allowed = await limiter.allow(f"ip:{ip}:{bucket}", limit)
    for key in extra_keys:
        if not await limiter.allow(key, limit):
            allowed = False
    if not allowed:
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
    # P0-1: IP 桶之外叠加账号维度桶 (email 归一化哈希), 反代坍缩/XFF 伪造时
    # 单账号撞库仍被 5 次/分钟卡住; 不同邮箱互不影响。
    email_hash = _email_hash(body.email)
    await _auth_rate_limit(
        request, settings, extra_keys=(f"acct:{email_hash}:login",)
    )
    async with plain_session() as session:
        user = await session.scalar(select(User).where(User.email == body.email))
        if user is None or not verify_password(body.password, user.password_hash):
            log.warning(
                "login_failed",
                email_hash=email_hash[:16],
                ip=request.client.host if request.client else "unknown",
            )
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
    settings: Settings = Depends(get_app_settings),
):
    """登出 (漏洞修复, 审查报告二-新增条): 不依赖有效 access token。

    旧实现 Depends(current_user) 在 access token 过期 (>15min) 时先 401,
    refresh 撤销与 delete_cookie 一行都不执行 — 共享电脑上下一个人打开
    站点即静默 refresh 进上一个账号。现改为:
    1) 无条件按 cookie 撤销 refresh + delete_cookie;
    2) 尽力解析 Bearer (容忍过期, verify_exp=False, 签名仍校验) 拉黑 jti;
    3) 永远 204。
    """
    raw = request.cookies.get(_REFRESH_COOKIE)
    if raw:
        async with plain_session() as session:
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.token_hash == hash_refresh_token(raw))
                .values(revoked=True)
            )
    response.delete_cookie(_REFRESH_COOKIE, path="/api/auth")
    # 尽力拉黑 access token 的 jti: 过期 token 也接受 (它 15 分钟内可能仍在
    # 其它标签页使用剩余有效期), 签名不合法的直接忽略。
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        try:
            payload = pyjwt.decode(
                token,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
                options={"verify_exp": False},
            )
            jti = payload.get("jti", "")
            if jti:
                await request.app.state.redis.setex(
                    f"bl:{jti}", settings.access_token_ttl_seconds, "1"
                )
        except pyjwt.PyJWTError:
            pass  # 伪造/损坏 token: 登出本身仍然成功
    return None


@router.get("/me", response_model=UserOut)
async def me(ctx: AuthContext = Depends(current_user)):
    async with plain_session() as session:
        user = await session.get(User, ctx.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="INVALID_TOKEN")
        return UserOut(
            id=user.id, email=user.email, role=user.role, created_at=user.created_at
        )
