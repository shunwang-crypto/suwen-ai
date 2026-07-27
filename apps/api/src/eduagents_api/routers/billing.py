"""预付额度计费 API (0006): /api/billing/*。契约已与前端冻结, 响应形状不得改。

- GET  /wallet                     {"balance": "10.0000", "currency": "CNY"}
- GET  /ledger?limit=50            {"data": [{id,delta,balance_after,kind,ref,note,created_at}]}
- GET  /pricing                    {"markup": 3.0, "signup_grant": "10.00", "note": ...}
- POST /orders {"amount_cny": 10}  {"id","amount_cny","credits","status":"pending","provider":"mock"}
- POST /orders/{id}/mock-pay       {"status": "paid", "balance": "20.0000"}  (仅 mock + 非 prod)

金额序列化统一字符串 (余额/流水/credits 4 位小数, 人民币面额 2 位), 前端不做
浮点运算。读路径走 tenant_session 本人 RLS 策略 (他人钱包/流水天然空集);
写路径走 wallet.service_session (billing_service GUC)。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from eduagents_core.config import Settings
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..models import PaymentOrder, WalletLedger
from ..payments import create_payment_provider
from ..wallet import get_wallet_service, q4, service_session

router = APIRouter(prefix="/api/billing", tags=["billing"])

# 充值面额白名单 (CNY), 契约冻结
ORDER_AMOUNTS_CNY = (10, 30, 68, 128)

LEDGER_LIMIT_DEFAULT = 50
LEDGER_LIMIT_MAX = 200


def _money4(value: Decimal) -> str:
    """Decimal -> "10.0000" (4 位定点字符串, 与钱包精度一致)。"""
    return format(q4(value), "f")


def _money2(value: Decimal) -> str:
    """Decimal -> "10.00" (人民币面额 2 位)。"""
    return format(value.quantize(Decimal("0.01")), "f")


class OrderIn(BaseModel):
    amount_cny: int  # 白名单校验在端点 (非法面额 422 AMOUNT_NOT_ALLOWED)


@router.get("/wallet")
async def get_wallet(ctx: AuthContext = Depends(api_rate_limited)):
    balance = await get_wallet_service().balance(ctx.user_id)
    return {"balance": _money4(balance), "currency": "CNY"}


@router.get("/ledger")
async def get_ledger(
    limit: int = Query(LEDGER_LIMIT_DEFAULT, ge=1, le=LEDGER_LIMIT_MAX),
    ctx: AuthContext = Depends(api_rate_limited),
):
    # RLS 本人策略 + 显式 user_id 条件双保险 (与仓库既有路由同风格)
    async with tenant_session(ctx.user_id) as session:
        rows = (
            await session.scalars(
                select(WalletLedger)
                .where(WalletLedger.user_id == ctx.user_id)
                .order_by(WalletLedger.created_at.desc(), WalletLedger.id.desc())
                .limit(limit)
            )
        ).all()
        data = [
            {
                "id": str(r.id),
                "delta": _money4(r.delta),
                "balance_after": _money4(r.balance_after),
                "kind": r.kind,
                "ref": r.ref,
                "note": r.note,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    return {"data": data}


@router.get("/pricing")
async def get_pricing(
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    wallet = get_wallet_service()
    markup = settings.billing_markup
    return {
        "markup": markup,
        "signup_grant": _money2(wallet.grant_cny),
        "note": f"按模型真实成本×{markup:g} 透明计费, 缓存命中免费",
    }


@router.post("/orders", status_code=201)
async def create_order(
    body: OrderIn,
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    if body.amount_cny not in ORDER_AMOUNTS_CNY:
        raise HTTPException(status_code=422, detail="AMOUNT_NOT_ALLOWED")
    provider = create_payment_provider(settings, get_wallet_service())
    async with service_session() as session:
        order = await provider.create_order(
            session, ctx.user_id, Decimal(body.amount_cny)
        )
        return {
            "id": str(order.id),
            "amount_cny": _money2(order.amount_cny),
            "credits": _money4(order.credits),
            "status": order.status,
            "provider": order.provider,
        }


@router.post("/orders/{order_id}/mock-pay")
async def mock_pay(
    order_id: uuid.UUID,
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    """模拟支付确认 (仅演示): mock provider + 非 prod 双闸, prod 一律 403。"""
    if settings.app_env == "prod":
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    wallet = get_wallet_service()
    provider = create_payment_provider(settings, wallet)
    if provider.name != "mock":
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    async with service_session() as session:
        # FOR UPDATE 锁单 + pending 闸: 重复确认 409, 不双入账 (幂等)
        order = await session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == order_id, PaymentOrder.user_id == ctx.user_id)
            .with_for_update()
        )
        if order is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        if order.status != "pending":
            raise HTTPException(status_code=409, detail="ORDER_NOT_PENDING")
        balance = await provider.confirm(session, order)
    return {"status": "paid", "balance": _money4(balance)}
