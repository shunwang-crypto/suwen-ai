"""管理员时间偏移 API (§9) + 故障注入开关 (M5-D2)。

时间偏移: 推进 Redis 里的全局虚拟时钟偏移, FSRS 到期/BKT 衰减/复习队列随之前进;
禁止改容器系统时间的方案。

故障注入: 现场演示降级链的操作台 —— 关主模型/加延迟/按概率报错 + 一键熔断,
只作用于 live/record 网关路径 (stub/replay 是兜底通道, 按设计不受影响)。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from eduagents_core.config import get_settings
from eduagents_core.gateway import Tier, get_fault_injector
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select, update

from .. import timectl
from ..db import plain_session, tenant_session
from ..deps import AuthContext, account_status_key, require_admin
from ..email_utils import normalize_email
from ..models import (
    ErrorItem,
    KnowledgeBase,
    Plan,
    RefreshToken,
    RegistrationInvite,
    UsageLog,
    User,
    UserModelCredential,
    WalletAccount,
    WalletLedger,
)
from ..registration_invites import generate_invite_code, hash_invite_code
from ..schemas import (
    AdminModelCredentialSummaryOut,
    AdminUserDetailOut,
    AdminUserListItemOut,
    AdminUserOut,
    AdminUserPageOut,
    AdminWalletAdjustIn,
    AdminWalletAdjustOut,
    RegistrationInviteBatchCreate,
    RegistrationInviteCreate,
    RegistrationInviteOut,
)
from ..wallet import WalletAdjustmentConflict, get_wallet_service, q4, service_session

router = APIRouter(prefix="/api/admin", tags=["admin"])
log = get_logger("admin")


def _money4(value) -> str:
    return format(q4(Decimal(str(value or 0))), "f")


@router.get(
    "/users",
    response_model=list[AdminUserOut] | AdminUserPageOut,
)
async def search_users(
    query: str | None = Query(default=None, min_length=3, max_length=320),
    limit: int = Query(default=20, ge=1, le=100),
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=20, ge=1, le=100),
    role: str | None = Query(default=None, pattern="^(student|teacher|admin)$"),
    email_verified: bool | None = None,
    status: str | None = Query(default=None, pattern="^(active|disabled)$"),
    ctx: AuthContext = Depends(require_admin),
):
    """分页用户列表；带 query 的旧客户端继续收到旧数组契约。"""
    normalized = normalize_email(query) if query else None
    if normalized is not None and len(normalized) < 3:
        raise HTTPException(status_code=422, detail="USER_QUERY_TOO_SHORT")
    escaped = (
        normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if normalized
        else None
    )
    prefix = f"{escaped}%" if escaped else None
    exact = func.lower(User.email) == normalized if normalized else None
    filters = []
    if normalized:
        filters.append(or_(exact, func.lower(User.email).like(prefix, escape="\\")))
    if role:
        filters.append(User.role == role)
    if status:
        filters.append(User.status == status)
    if email_verified is True:
        filters.append(User.email_verified_at.is_not(None))
    elif email_verified is False:
        filters.append(User.email_verified_at.is_(None))
    async with service_session() as session:
        total = await session.scalar(select(func.count()).select_from(User).where(*filters))
        rows = (
            await session.execute(
                select(User, WalletAccount.balance)
                .outerjoin(WalletAccount, WalletAccount.user_id == User.id)
                .where(*filters)
                .order_by(User.created_at.desc(), User.id)
                .offset((page - 1) * page_size if not normalized else 0)
                .limit(limit if normalized else page_size)
            )
        ).all()
    user_ids = [user.id for user, _balance in rows]
    metrics = await _admin_user_metrics(ctx, user_ids)
    log.info(
        "admin_user_search",
        admin_user_id=str(ctx.user_id),
        query_hash=(hashlib.sha256(normalized.encode()).hexdigest()[:16] if normalized else None),
        result_count=len(rows),
    )
    old_items = [
        AdminUserOut(
            user_id=user.id,
            email=user.email,
            role=user.role,
            status=user.status,
            wallet_balance=_money4(balance),
            created_at=user.created_at,
        )
        for user, balance in rows
    ]
    if normalized:
        return old_items
    items = [
        AdminUserListItemOut(
            **item.model_dump(),
            email_verified=user.email_verified_at is not None,
            **metrics.get(user.id, {}),
        )
        for (user, _balance), item in zip(rows, old_items, strict=True)
    ]
    return AdminUserPageOut(
        items=items,
        page=page,
        page_size=page_size,
        total=int(total or 0),
        has_more=page * page_size < int(total or 0),
    )


async def _admin_user_metrics(ctx: AuthContext, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """Fixed-query aggregate metrics for a page, never one query per user."""
    if not user_ids:
        return {}
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with tenant_session(ctx.tenant_id, is_admin=True) as session:
        usage_rows = (
            await session.execute(
                select(
                    UsageLog.user_id,
                    func.count().filter(UsageLog.credential_source == "platform"),
                    func.count().filter(UsageLog.credential_source == "user"),
                    func.coalesce(func.sum(UsageLog.platform_billed_cost_cny), 0.0),
                    func.coalesce(
                        func.sum(
                            case(
                                (UsageLog.at >= start, UsageLog.platform_billed_cost_cny),
                                else_=0.0,
                            )
                        ),
                        0.0,
                    ),
                )
                .where(UsageLog.user_id.in_(user_ids))
                .group_by(UsageLog.user_id)
            )
        ).all()
        kb_rows = (
            await session.execute(
                select(KnowledgeBase.tenant_id, func.count())
                .where(KnowledgeBase.tenant_id.in_(user_ids))
                .group_by(KnowledgeBase.tenant_id)
            )
        ).all()
        error_rows = (
            await session.execute(
                select(ErrorItem.tenant_id, func.count())
                .where(ErrorItem.tenant_id.in_(user_ids))
                .group_by(ErrorItem.tenant_id)
            )
        ).all()
        plan_rows = (
            await session.execute(
                select(Plan.tenant_id, func.count())
                .where(Plan.tenant_id.in_(user_ids))
                .group_by(Plan.tenant_id)
            )
        ).all()
    result = {
        uid: {
            "today_platform_cost_cny": f"{float(today):.6f}",
            "total_platform_cost_cny": f"{float(total):.6f}",
            "platform_call_count": int(platform_calls or 0),
            "byok_call_count": int(byok_calls or 0),
            "knowledge_base_count": 0,
            "error_count": 0,
            "plan_count": 0,
        }
        for uid, platform_calls, byok_calls, total, today in usage_rows
    }
    for uid, count in kb_rows:
        result.setdefault(uid, {})["knowledge_base_count"] = int(count)
    for uid, count in error_rows:
        result.setdefault(uid, {})["error_count"] = int(count)
    for uid, count in plan_rows:
        result.setdefault(uid, {})["plan_count"] = int(count)
    return result


@router.get("/users/{user_id}", response_model=AdminUserDetailOut)
async def user_detail(user_id: uuid.UUID, ctx: AuthContext = Depends(require_admin)):
    """Safe aggregate detail; encrypted credential material is never selected."""
    async with service_session() as session:
        row = (
            await session.execute(
                select(User, WalletAccount.balance)
                .outerjoin(WalletAccount, WalletAccount.user_id == User.id)
                .where(User.id == user_id)
            )
        ).first()
        adjustments = (
            await session.scalars(
                select(WalletLedger)
                .where(WalletLedger.user_id == user_id, WalletLedger.kind == "admin_adjust")
                .order_by(WalletLedger.created_at.desc())
                .limit(20)
            )
        ).all()
    if row is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    user, balance = row
    metrics = (await _admin_user_metrics(ctx, [user_id])).get(user_id, {})
    async with tenant_session(ctx.tenant_id, is_admin=True) as session:
        credential = await session.execute(
            select(
                UserModelCredential.provider,
                UserModelCredential.enabled,
                UserModelCredential.fallback_to_platform,
                UserModelCredential.key_last_four,
                UserModelCredential.normal_model,
                UserModelCredential.reasoning_model,
            ).where(UserModelCredential.user_id == user_id)
        )
        credential_row = credential.first()
    return AdminUserDetailOut(
        user_id=user.id,
        email=user.email,
        role=user.role,
        status=user.status,
        wallet_balance=_money4(balance),
        created_at=user.created_at,
        email_verified=user.email_verified_at is not None,
        **metrics,
        model_credential=(
            AdminModelCredentialSummaryOut(
                configured=True,
                provider=credential_row.provider,
                enabled=credential_row.enabled,
                fallback_to_platform=credential_row.fallback_to_platform,
                key_last_four=credential_row.key_last_four,
                normal_model=credential_row.normal_model,
                reasoning_model=credential_row.reasoning_model,
            )
            if credential_row
            else AdminModelCredentialSummaryOut(configured=False)
        ),
        recent_admin_adjustments=[
            {
                "delta": _money4(adjustment.delta),
                "balance_after": _money4(adjustment.balance_after),
                "note": adjustment.note,
                "reference": adjustment.ref,
                "created_at": adjustment.created_at,
            }
            for adjustment in adjustments
        ],
    )


@router.post(
    "/users/{user_id}/wallet-adjust",
    response_model=AdminWalletAdjustOut,
)
async def adjust_user_wallet(
    user_id: uuid.UUID,
    body: AdminWalletAdjustIn,
    ctx: AuthContext = Depends(require_admin),
):
    """原子、可重试的管理员额度调整。"""
    wallet = get_wallet_service()
    try:
        async with service_session() as session:
            result = await wallet.admin_adjust(
                user_id,
                body.delta,
                body.note,
                body.reference,
                session=session,
            )
    except ValueError as exc:
        if not str(exc).startswith("wallet_adjust_no_user:"):
            raise
        raise HTTPException(status_code=404, detail="NOT_FOUND") from None
    except WalletAdjustmentConflict:
        raise HTTPException(status_code=409, detail="WALLET_ADJUST_REFERENCE_CONFLICT") from None

    log.warning(
        "admin_wallet_adjusted",
        admin_user_id=str(ctx.user_id),
        target_user_id=str(user_id),
        delta=format(result.delta, "f"),
        reference=result.reference,
    )
    return AdminWalletAdjustOut(
        user_id=result.user_id,
        previous_balance=_money4(result.previous_balance),
        delta=_money4(result.delta),
        balance=_money4(result.balance),
        note=result.note,
        reference=result.reference or body.reference,
    )


def _invite_status(row: RegistrationInvite, now: datetime) -> str:
    if row.status == "active" and row.expires_at is not None and row.expires_at <= now:
        return "expired"
    return row.status


def _invite_out(
    row: RegistrationInvite,
    now: datetime,
    *,
    code: str | None = None,
    used_by_email: str | None = None,
) -> RegistrationInviteOut:
    return RegistrationInviteOut(
        id=row.id,
        code=code,
        status=_invite_status(row, now),
        expires_at=row.expires_at,
        max_uses=row.max_uses,
        used_count=row.used_count,
        created_at=row.created_at,
        used_by=row.used_by,
        used_by_email=used_by_email,
    )


async def _create_invites(
    count: int,
    max_uses: int,
    expires_in_hours: int | None,
    ctx: AuthContext,
) -> tuple[list[RegistrationInviteOut], list[str]]:
    now = datetime.now(UTC)
    expires_at = (
        now + timedelta(hours=expires_in_hours) if expires_in_hours is not None else None
    )
    rows: list[RegistrationInvite] = []
    codes: list[str] = []
    async with plain_session() as session:
        for _ in range(count):
            code = generate_invite_code()
            row = RegistrationInvite(
                code_hash=hash_invite_code(code),
                max_uses=max_uses,
                expires_at=expires_at,
                created_by=ctx.user_id,
            )
            session.add(row)
            rows.append(row)
            codes.append(code)
        await session.flush()
    log.warning(
        "admin_registration_invites_created",
        by=str(ctx.user_id),
        invite_ids=[str(row.id) for row in rows],
        count=count,
        max_uses=max_uses,
        expires_at=expires_at.isoformat() if expires_at else None,
    )
    return [_invite_out(row, now, code=code) for row, code in zip(rows, codes, strict=True)], codes


@router.post("/registration-invites", response_model=RegistrationInviteOut, status_code=201)
async def create_registration_invite(
    body: RegistrationInviteCreate, ctx: AuthContext = Depends(require_admin)
):
    invites, _ = await _create_invites(1, body.max_uses, body.expires_in_hours, ctx)
    return invites[0]


@router.post(
    "/registration-invites/batch",
    response_model=list[RegistrationInviteOut],
    status_code=201,
)
async def create_registration_invites_batch(
    body: RegistrationInviteBatchCreate, ctx: AuthContext = Depends(require_admin)
):
    invites, _ = await _create_invites(body.count, body.max_uses, body.expires_in_hours, ctx)
    return invites


@router.get("/registration-invites", response_model=list[RegistrationInviteOut])
async def list_registration_invites(
    limit: int = Query(default=100, ge=1, le=200), ctx: AuthContext = Depends(require_admin)
):
    now = datetime.now(UTC)
    async with plain_session() as session:
        rows = (
            await session.execute(
                select(RegistrationInvite, User.email)
                .outerjoin(User, User.id == RegistrationInvite.used_by)
                .order_by(RegistrationInvite.created_at.desc())
                .limit(limit)
            )
        ).all()
    return [_invite_out(row, now, used_by_email=email) for row, email in rows]


@router.post("/registration-invites/{invite_id}/revoke", response_model=RegistrationInviteOut)
async def revoke_registration_invite(
    invite_id: uuid.UUID, ctx: AuthContext = Depends(require_admin)
):
    now = datetime.now(UTC)
    async with plain_session() as session:
        row = await session.scalar(
            select(RegistrationInvite).where(RegistrationInvite.id == invite_id).with_for_update()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        if _invite_status(row, now) != "active":
            raise HTTPException(status_code=409, detail="INVITE_NOT_ACTIVE")
        row.status = "revoked"
        row.revoked_at = now
    log.warning(
        "admin_registration_invite_revoked", by=str(ctx.user_id), invite_id=str(invite_id)
    )
    return _invite_out(row, now)


class TimeTravelIn(BaseModel):
    days: float = Field(ge=-365.0, le=365.0, description="虚拟时钟前进天数 (可为负/小数)")


@router.post("/time-travel")
async def time_travel(
    body: TimeTravelIn, request: Request, ctx: AuthContext = Depends(require_admin)
):
    # P1-13: 生产禁用虚拟时钟 (误触会整体前移全体 FSRS 到期; 演示/开发环境专用)
    if not timectl.is_enabled():
        raise HTTPException(status_code=403, detail="TIME_TRAVEL_DISABLED")
    new_offset = await timectl.advance(request.app.state.redis, body.days)
    return {"offset_days": new_offset / 86400.0, "offset_seconds": new_offset}


@router.get("/time-travel")
async def get_time_travel(request: Request, ctx: AuthContext = Depends(require_admin)):
    offset = await timectl.get_offset(request.app.state.redis)
    now_dt = await timectl.now(request.app.state.redis)
    return {
        "offset_days": offset / 86400.0,
        "offset_seconds": offset,
        "virtual_now": now_dt.isoformat(),
    }


# --------------------------------------------------------------------------
# 账号封禁/解封 (P1-3)
# --------------------------------------------------------------------------


async def _set_user_status(user_id: uuid.UUID, status: str) -> None:
    async with plain_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        user.status = status
        if status != "active":
            # 全部 refresh 撤销: 无法枚举在途 access jti, 由 deps 的状态检查
            # (Redis 标记 + 60s 缓存) 立即拦截
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False))
                .values(revoked=True)
            )


@router.post("/users/{user_id}/disable")
async def disable_user(
    user_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(require_admin)
):
    """封禁: users.status='disabled' + 撤销全部 refresh + Redis 标记立即生效。"""
    if user_id == ctx.user_id:
        raise HTTPException(status_code=400, detail="CANNOT_DISABLE_SELF")
    await _set_user_status(user_id, "disabled")
    await request.app.state.redis.setex(account_status_key(user_id), 60, "disabled")
    log.warning("admin_user_disabled", target=str(user_id), by=str(ctx.user_id))
    return {"user_id": str(user_id), "status": "disabled"}


@router.post("/users/{user_id}/enable")
async def enable_user(
    user_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(require_admin)
):
    await _set_user_status(user_id, "active")
    # 删标记而非写 active: 下一请求查库回填, 以 DB 为唯一事实源
    await request.app.state.redis.delete(account_status_key(user_id))
    log.warning("admin_user_enabled", target=str(user_id), by=str(ctx.user_id))
    return {"user_id": str(user_id), "status": "active"}


# --------------------------------------------------------------------------
# 故障注入 (M5-D2)
# --------------------------------------------------------------------------

_FAULT_NOTE = (
    "故障注入只作用于 live/record 网关路径; stub/replay 不受影响 "
    "(它们是降级链最后一环的兜底通道, 演示必须可靠)。"
)


class FaultsIn(BaseModel):
    """全字段可选: 省略 = 保持原值 (面板上三个旋钮各自独立提交)。"""

    provider_down: bool | None = None
    latency_ms: int | None = Field(default=None, ge=0, le=30_000)
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    # 一键熔断: 直接把断路器拉开 (不必等错误率累积), "all" = 全档打开
    trip_breaker: str | None = Field(
        default=None,
        pattern="^(cheap|standard|strong|plan|trenddao-text|trenddao-vision|trenddao-plan|all)$",
    )
    reset_breakers: bool = False


def _faults_payload(request: Request) -> dict:
    injector = get_fault_injector()
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is not None and hasattr(gateway, "breaker_states"):
        all_states = gateway.breaker_states()
        # Keep the original four-item `breakers` contract stable. New
        # candidate-level state is exposed in the additive `routing` field.
        states = {tier.value: all_states.get(tier.value, "unknown") for tier in Tier}
    else:  # 网关未就绪 (极早期/测试替身): 如实报 unknown, 不假装 closed
        states = {t.value: "unknown" for t in Tier}
    routing = gateway.routing_status() if gateway is not None and hasattr(gateway, "routing_status") else []
    return {
        **injector.snapshot(),
        "gateway_mode": get_settings().gateway_mode,
        "note": _FAULT_NOTE,
        "breakers": [
            {"tier": tier, "candidate_id": tier, "state": state}
            for tier, state in states.items()
        ],
        "routing": routing,
    }


@router.get("/faults")
async def get_faults(request: Request, ctx: AuthContext = Depends(require_admin)):
    if not get_settings().enable_fault_injection:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return _faults_payload(request)


@router.post("/faults")
async def set_faults(
    body: FaultsIn, request: Request, ctx: AuthContext = Depends(require_admin)
):
    if not get_settings().enable_fault_injection:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    injector = get_fault_injector()
    injector.update(
        provider_down=body.provider_down,
        latency_ms=body.latency_ms,
        error_rate=body.error_rate,
    )
    gateway = getattr(request.app.state, "gateway", None)
    # reset 先于 trip: 允许一次请求里 "全恢复 + 只熔断 standard"
    if body.reset_breakers and gateway is not None and hasattr(gateway, "reset_breakers"):
        gateway.reset_breakers()
    if body.trip_breaker and gateway is not None and hasattr(gateway, "trip_breaker"):
        if body.trip_breaker == "all":
            for tier in Tier:
                gateway.trip_breaker(tier)
            for row in getattr(gateway, "routing_status", lambda: [])():
                if hasattr(gateway, "trip_candidate"):
                    gateway.trip_candidate(str(row["id"]))
        elif body.trip_breaker in {tier.value for tier in Tier}:
            gateway.trip_breaker(Tier(body.trip_breaker))
        elif hasattr(gateway, "trip_candidate"):
            gateway.trip_candidate(body.trip_breaker)
    return _faults_payload(request)
