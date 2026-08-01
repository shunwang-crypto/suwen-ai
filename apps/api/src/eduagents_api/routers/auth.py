from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
from eduagents_core.config import Settings
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

from ..captcha import create_captcha, verify_captcha
from ..db import plain_session
from ..deps import AuthContext, current_user, get_app_settings
from ..email_utils import normalize_email
from ..email_verification import (
    EMAIL_CODE_DAILY_EMAIL_PREFIX,
    EMAIL_CODE_DAILY_IP_PREFIX,
    code_key,
    consume_email_code,
    cooldown_key,
    daily_key,
    email_hash,
    generate_email_code,
    ip_hash,
    store_email_code,
)
from ..emailer import create_email_sender
from ..models import PasswordResetToken, RefreshToken, User
from ..schemas import (
    AccountProfileUpdateIn,
    ForgotPasswordIn,
    LoginRequest,
    RegisterEmailCodeRequest,
    RegisterRequest,
    ResetPasswordIn,
    TokenResponse,
    UserOut,
)
from ..security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_password_reset_token,
    new_refresh_token,
    verify_password,
)
from ..wallet import get_wallet_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = get_logger("auth")

_REFRESH_COOKIE = "edu_refresh"

# 登录防爆破 (同邮箱维度): 连续失败计数, >=3 次后 login 必须携带图形验证码;
# 成功登录清零。计数键 TTL 15min (滑动: 每次失败重置过期时间)。
_LOGIN_FAIL_THRESHOLD = 3
_LOGIN_FAIL_TTL_SECONDS = 900


def _login_fail_key(email_hash: str) -> str:
    return f"login_fail:{email_hash}"


async def _mark_service_layer(session) -> None:
    """password_reset_tokens 的 RLS 服务层标记 (0006, 计费线协调契约)。

    该表 ENABLE+FORCE RLS 且唯一策略 prt_service_all 要求事务内
    app.billing_service='true' — 必须 set_config(..., is_local=true) 事务级注入
    (同 db.tenant_session 的 GUC 口径: 连接归池即失效, 不跨请求泄漏)。
    """
    await session.execute(text("SELECT set_config('app.billing_service', 'true', true)"))


def _email_hash(email: str) -> str:
    """归一化 email 的 sha256 (日志/限流键用, 不落明文)。"""
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()


def _registration_requires_email_verification(settings: Settings) -> bool:
    # A production misconfiguration must never turn ownership verification off.
    return settings.app_env == "prod" or settings.email_verification_required


async def _increment_daily_counter(redis, key: str, limit: int) -> bool:
    count = int(await redis.incr(key))
    if count == 1:
        # Keep one extra day so a clock boundary cannot leave an unbounded key.
        await redis.expire(key, 172800)
    return count <= limit


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
        raise HTTPException(status_code=429, detail="RATE_LIMITED", headers={"Retry-After": "30"})


def _set_refresh_cookie(response: Response, settings: Settings, raw: str) -> None:
    response.set_cookie(
        _REFRESH_COOKIE,
        raw,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.app_env == "prod",
        path="/api/auth",
    )


async def _issue_refresh(user_id, settings: Settings, rotated_from: str | None = None) -> str:
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


@router.get("/captcha")
async def get_captcha(
    request: Request,
    settings: Settings = Depends(get_app_settings),
):
    """图形验证码 (自托管 Pillow, 零外部依赖): 答案入 Redis TTL 300s, 单次有效。"""
    await _auth_rate_limit(
        request,
        settings,
        bucket="auth_captcha",
        per_minute=settings.captcha_rate_limit_per_minute,
    )
    captcha_id, image = await create_captcha(request.app.state.redis)
    return {"captcha_id": captcha_id, "image": image}


@router.post("/register/email-code", status_code=202)
async def send_register_email_code(
    body: RegisterEmailCodeRequest,
    request: Request,
    settings: Settings = Depends(get_app_settings),
):
    """Send a single-use ownership code after consuming a graphic captcha.

    The endpoint has one response for both existing and non-existing
    addresses. Existing accounts consume the same abuse-control buckets but
    receive no message, preventing this endpoint from becoming an account
    enumeration oracle.
    """

    if not settings.public_registration:
        raise HTTPException(status_code=403, detail="REGISTRATION_CLOSED")
    redis = request.app.state.redis
    if not await verify_captcha(redis, body.captcha_id, body.captcha_answer):
        raise HTTPException(status_code=400, detail="CAPTCHA_INVALID")

    email = normalize_email(str(body.email))
    address_hash = email_hash(email)
    remote_ip = request.client.host if request.client else "unknown"
    remote_ip_hash = ip_hash(remote_ip)
    limiter = request.app.state.rate_limiter
    if not await limiter.allow(
        f"email-code:email:{address_hash}", settings.email_code_per_minute
    ) or not await limiter.allow(
        f"email-code:ip:{remote_ip_hash}", settings.email_code_per_minute
    ):
        raise HTTPException(
            status_code=429,
            detail="EMAIL_CODE_RATE_LIMITED",
            headers={"Retry-After": str(settings.email_code_resend_cooldown_seconds)},
        )

    email_daily = daily_key(EMAIL_CODE_DAILY_EMAIL_PREFIX, address_hash)
    ip_daily = daily_key(EMAIL_CODE_DAILY_IP_PREFIX, remote_ip_hash)
    email_daily_ok = await _increment_daily_counter(
        redis, email_daily, settings.email_code_daily_limit
    )
    ip_daily_ok = await _increment_daily_counter(
        redis, ip_daily, settings.email_code_ip_daily_limit
    )
    if not email_daily_ok or not ip_daily_ok:
        raise HTTPException(status_code=429, detail="EMAIL_CODE_RATE_LIMITED")

    cooldown = await redis.set(
        cooldown_key(email),
        "1",
        ex=settings.email_code_resend_cooldown_seconds,
        nx=True,
    )
    if not cooldown:
        raise HTTPException(
            status_code=429,
            detail="EMAIL_CODE_RATE_LIMITED",
            headers={"Retry-After": str(settings.email_code_resend_cooldown_seconds)},
        )

    async with plain_session() as session:
        existing = await session.scalar(select(User.id).where(func.lower(User.email) == email))
    if existing is not None:
        # Keep the cooldown for existing addresses as well. The same 202
        # response is returned, but no code is generated or sent.
        return {"accepted": True}

    code = generate_email_code()
    await store_email_code(redis, email, code, settings)
    try:
        sender = create_email_sender(settings, redis)
        await sender.send(
            to=email,
            subject="EduAgents 注册验证码",
            body=(
                "你好，\n\n"
                "你的 EduAgents 注册验证码是："
                f"{code}\n\n"
                f"验证码 {settings.email_code_ttl_seconds // 60} 分钟内有效。\n"
                "如果不是本人操作请忽略本邮件。\n"
            ),
        )
    except RuntimeError as exc:
        await redis.delete(code_key(email), cooldown_key(email))
        if str(exc) == "SMTP_NOT_CONFIGURED":
            raise HTTPException(status_code=503, detail="SMTP_NOT_CONFIGURED") from None
        raise HTTPException(status_code=503, detail="EMAIL_SEND_FAILED") from None
    except Exception:  # noqa: BLE001 — do not expose SMTP details or secrets
        await redis.delete(code_key(email), cooldown_key(email))
        log.exception("registration_email_failed", recipient_hash=address_hash[:16])
        raise HTTPException(status_code=503, detail="EMAIL_SEND_FAILED") from None
    return {"accepted": True}


@router.post("/register", response_model=UserOut, status_code=201)
async def register(
    body: RegisterRequest,
    request: Request,
    settings: Settings = Depends(get_app_settings),
):
    if not settings.public_registration:
        raise HTTPException(status_code=403, detail="REGISTRATION_CLOSED")
    await _auth_rate_limit(request, settings)
    normalized_email = normalize_email(str(body.email))
    requires_email_code = _registration_requires_email_verification(settings)
    if requires_email_code:
        if not body.email_code:
            raise HTTPException(status_code=400, detail="EMAIL_CODE_REQUIRED")
        if not await consume_email_code(
            request.app.state.redis, normalized_email, body.email_code, settings
        ):
            raise HTTPException(status_code=400, detail="EMAIL_CODE_INVALID")
    # Legacy/local mode keeps the existing captcha-only path. Production can
    # never use it as a substitute for proving mailbox ownership.
    elif settings.captcha_required_register and settings.app_env != "test":
        if not await verify_captcha(request.app.state.redis, body.captcha_id, body.captcha_answer):
            raise HTTPException(status_code=400, detail="CAPTCHA_INVALID")
    try:
        async with plain_session() as session:
            existing = await session.scalar(
                select(User).where(func.lower(User.email) == normalized_email)
            )
            if existing:
                raise HTTPException(status_code=409, detail="EMAIL_EXISTS")
            user = User(
                email=normalized_email,
                email_verified_at=datetime.now(UTC) if requires_email_code else None,
                display_name=body.display_name,
                password_hash=hash_password(body.password, settings.bcrypt_rounds),
                role="student",
            )
            session.add(user)
            await session.flush()
            # 0006 预付额度: 注册赠送与建号及验证时间同事务提交。
            await get_wallet_service().grant_signup(session, user.id)
            log.info("user_registered", user_id=str(user.id))
            result = UserOut(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=user.role,
                created_at=user.created_at,
                email_verified_at=user.email_verified_at,
            )
    except IntegrityError:
        # The lower-case unique index is the concurrency authority. Do not
        # leak a raw database error if two verified requests race.
        raise HTTPException(status_code=409, detail="EMAIL_EXISTS") from None
    return result


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
    await _auth_rate_limit(request, settings, extra_keys=(f"acct:{email_hash}:login",))
    redis = request.app.state.redis
    # 防爆破: 同邮箱连续失败 >=3 次后必须先过图形验证码 (未带 -> CAPTCHA_REQUIRED,
    # 带了但答错/过期 -> CAPTCHA_INVALID)。验证码在密码校验**之前**消耗,
    # 攻击者无法用一张图试多个密码。
    fail_key = _login_fail_key(email_hash)
    fails = int(await redis.get(fail_key) or 0)
    if fails >= _LOGIN_FAIL_THRESHOLD:
        if not body.captcha_id or not body.captcha_answer:
            raise HTTPException(status_code=400, detail="CAPTCHA_REQUIRED")
        if not await verify_captcha(redis, body.captcha_id, body.captcha_answer):
            raise HTTPException(status_code=400, detail="CAPTCHA_INVALID")
    async with plain_session() as session:
        user = await session.scalar(select(User).where(User.email == normalize_email(str(body.email))))
        if user is None or not verify_password(body.password, user.password_hash):
            # 失败计数 +1, 滑动 15min 过期 (INCR 后无条件 EXPIRE)
            await redis.incr(fail_key)
            await redis.expire(fail_key, _LOGIN_FAIL_TTL_SECONDS)
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
    await redis.delete(fail_key)  # 成功登录清零失败计数
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
        request,
        settings,
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


@router.post("/forgot", status_code=204)
async def forgot_password(
    body: ForgotPasswordIn,
    request: Request,
    settings: Settings = Depends(get_app_settings),
):
    """密码找回申请: **恒定 204** (防枚举 — 无论邮箱是否注册, 响应完全一致)。

    存在的用户: 生成 32 字节 urlsafe token, sha256 落 password_reset_tokens
    (30min 过期, 同用户旧的未用 token 直接作废删除), 通过 EmailSender 发送
    重置链接 {WEB_ORIGIN}/reset?token=...。token 原文只进邮件, 绝不进响应。
    """
    # 独立限流桶 5/min/IP: 防批量骚扰发信 (与 login/register 撞库桶隔离)
    await _auth_rate_limit(
        request,
        settings,
        bucket="auth_forgot",
        per_minute=settings.auth_forgot_rate_limit_per_minute,
    )
    redis = request.app.state.redis
    # 验证码在查库**之前**消耗: 无论用户存在与否行为一致, 不成为枚举侧信道
    if not await verify_captcha(redis, body.captcha_id, body.captcha_answer):
        raise HTTPException(status_code=400, detail="CAPTCHA_INVALID")
    reset_link = None
    async with plain_session() as session:
        await _mark_service_layer(session)  # RLS: password_reset_tokens 仅服务层可写
        user = await session.scalar(select(User).where(User.email == normalize_email(str(body.email))))
        if user is not None:
            raw, token_hash = new_password_reset_token()
            # 旧未用 token 作废 (删除): 任一时刻每用户至多一个有效重置链接
            await session.execute(
                delete(PasswordResetToken).where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.used_at.is_(None),
                )
            )
            session.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=token_hash,
                    expires_at=datetime.now(UTC)
                    + timedelta(seconds=settings.password_reset_ttl_seconds),
                )
            )
            reset_link = f"{settings.web_origin}/reset?token={raw}"
    if reset_link is not None:
        # 事务提交后再发信 (SMTP 秒级耗时不占 DB 事务); 发信失败不改变 204 口径
        # (防枚举优先), 但日志如实记录失败, 不伪造"已发送"。
        try:
            sender = create_email_sender(settings, redis)
            await sender.send(
                to=body.email,
                subject="EduAgents 密码重置",
                body=(
                    "你好,\n\n"
                    "我们收到了你的密码重置申请。请在 30 分钟内打开以下链接设置新密码:\n\n"
                    f"{reset_link}\n\n"
                    "如果这不是你本人的操作, 请忽略本邮件, 你的密码不会被更改。\n"
                ),
            )
        except Exception:  # noqa: BLE001 — SMTP 故障不暴露给请求方 (防枚举)
            log.exception("password_reset_email_failed", email_hash=_email_hash(body.email)[:16])
    log.info("password_reset_requested", email_hash=_email_hash(body.email)[:16])
    return None


@router.post("/reset", status_code=204)
async def reset_password(
    body: ResetPasswordIn,
    request: Request,
    settings: Settings = Depends(get_app_settings),
):
    """凭一次性 token 改密: 校验 sha256/过期/未用 -> bcrypt 改密 -> 标记 used ->
    吊销该用户全部 refresh token (与 refresh 重放检测同一吊销口径) -> 204。

    失败一律 400 RESET_TOKEN_INVALID, 不区分"不存在/过期/已用" (不给探测面)。
    """
    await _auth_rate_limit(request, settings, bucket="auth_reset")
    token_hash = hash_refresh_token(body.token)  # 同 sha256 口径 (security.py)
    now = datetime.now(UTC)
    async with plain_session() as session:
        await _mark_service_layer(session)  # RLS: password_reset_tokens 仅服务层可读写
        row = await session.scalar(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        )
        if row is None or row.used_at is not None or row.expires_at.replace(tzinfo=UTC) < now:
            raise HTTPException(status_code=400, detail="RESET_TOKEN_INVALID")
        user = await session.get(User, row.user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="RESET_TOKEN_INVALID")
        user.password_hash = hash_password(body.new_password, settings.bcrypt_rounds)
        row.used_at = now  # 单次性: 标记已用, 二次提交同 token 直接 400
        # 吊销全部 refresh token (复用 refresh 重放检测的全家撤销写法):
        # 改密后所有已登录会话立即失效, 盗号者手里的 cookie 变废纸
        await session.execute(
            update(RefreshToken).where(RefreshToken.user_id == row.user_id).values(revoked=True)
        )
        user_id = row.user_id
    # 改密成功顺手清掉登录失败计数 (用户凭新密码登录不应再被要求验证码)
    await request.app.state.redis.delete(_login_fail_key(_email_hash(user.email)))
    log.info("password_reset_completed", user_id=str(user_id))
    return None


@router.get("/me", response_model=UserOut)
async def me(ctx: AuthContext = Depends(current_user)):
    async with plain_session() as session:
        user = await session.get(User, ctx.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="INVALID_TOKEN")
        return UserOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            created_at=user.created_at,
            email_verified_at=user.email_verified_at,
        )


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: AccountProfileUpdateIn,
    ctx: AuthContext = Depends(current_user),
):
    async with plain_session() as session:
        user = await session.get(User, ctx.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="INVALID_TOKEN")
        user.display_name = body.display_name
        await session.flush()
        return UserOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            created_at=user.created_at,
            email_verified_at=user.email_verified_at,
        )
