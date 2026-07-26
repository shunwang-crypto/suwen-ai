from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt as pyjwt
import orjson
from eduagents_core.config import Settings, get_settings
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .security import decode_access_token

_bearer = HTTPBearer(auto_error=False)

# 机构成员关系缓存 (Wave3): org_members 极小但每请求都要, Redis 60s 兜住热路径。
# 建 org / join 时由 routers/orgs.py 主动失效 (invalidate_org_cache)。
_ORG_CACHE_TTL = 60


def _org_cache_key(user_id: uuid.UUID | str) -> str:
    return f"orgs:{user_id}"


async def invalidate_org_cache(redis, user_id: uuid.UUID | str) -> None:
    await redis.delete(_org_cache_key(user_id))


# 账号状态检查 (P1-3 封禁): disabled:{uid} 缓存 users.status 快照, 60s TTL,
# miss 时查库回填。封禁/注销路径 (admin.py / account.py) 主动写标记立即生效。
_ACCOUNT_STATUS_TTL = 60


def account_status_key(user_id: uuid.UUID | str) -> str:
    return f"disabled:{user_id}"


async def _ensure_account_active(redis, user_id: uuid.UUID) -> None:
    """status != 'active' -> 403 ACCOUNT_DISABLED (用户行已删按 'deleted' 处理)。"""
    key = account_status_key(user_id)
    status = await redis.get(key)
    if status is None:
        from sqlalchemy import select

        from .db import plain_session
        from .models import User

        async with plain_session() as session:
            status = await session.scalar(select(User.status).where(User.id == user_id))
        status = status or "deleted"
        await redis.setex(key, _ACCOUNT_STATUS_TTL, status)
    if status != "active":
        raise HTTPException(status_code=403, detail="ACCOUNT_DISABLED")


async def _load_org_memberships(
    redis, user_id: uuid.UUID
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """返回 (org_ids, admin_org_ids)。缓存值损坏时回源查库。"""
    cached = await redis.get(_org_cache_key(user_id))
    if cached is not None:
        try:
            pairs = orjson.loads(cached)
            return (
                tuple(str(o) for o, _ in pairs),
                tuple(str(o) for o, r in pairs if r == "admin"),
            )
        except (orjson.JSONDecodeError, TypeError, ValueError):
            pass
    # 循环依赖规避: db/models 延迟导入 (deps 在 db 之前被大量模块引用)
    from sqlalchemy import select

    from .db import tenant_session
    from .models import OrgMember

    async with tenant_session(user_id) as session:
        rows = (
            await session.execute(
                select(OrgMember.org_id, OrgMember.role).where(
                    OrgMember.user_id == user_id
                )
            )
        ).all()
    pairs = [(str(org_id), role) for org_id, role in rows]
    await redis.setex(_org_cache_key(user_id), _ORG_CACHE_TTL, orjson.dumps(pairs))
    return (
        tuple(o for o, _ in pairs),
        tuple(o for o, r in pairs if r == "admin"),
    )


@dataclass(frozen=True)
class AuthContext:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    jti: str
    # Wave3 机构上下文: 所属机构 id 集合 (RLS org_ids GUC 注入源) 与其中的 admin 子集
    org_ids: tuple[str, ...] = ()
    admin_org_ids: tuple[str, ...] = ()


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
    user_id = uuid.UUID(payload["sub"])
    # P1-3: 封禁/注销的账号即使持有效 token 也拒绝 (60s 缓存窗口内立即生效)
    await _ensure_account_active(redis, user_id)
    org_ids, admin_org_ids = await _load_org_memberships(redis, user_id)
    ctx = AuthContext(
        user_id=user_id,
        tenant_id=uuid.UUID(payload["tid"]),
        role=payload.get("role", "student"),
        jti=jti,
        org_ids=org_ids,
        admin_org_ids=admin_org_ids,
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
