"""支付通道抽象 (0006): PaymentProvider 协议 + MockPaymentProvider (演示通道)。

- MockPaymentProvider 仅供 dev/演示: confirm 直接把订单标 paid 并入账 recharge,
  账本 note 如实标注「模拟支付-演示通道」, 绝不冒充真实收款;
  路由层双闸: 仅 payment_provider=mock 且非 prod 环境才暴露 mock-pay (prod 403)。
- 真实通道 (微信/支付宝) 留接口不实现: create_payment_provider 收到未知
  provider 名直接抛 NotImplementedError —— 不做任何假装成功的伪实现 (零虚假实现纪律)。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from eduagents_core.config import Settings
from eduagents_core.logging import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PaymentOrder
from .wallet import WalletService, q4

log = get_logger("payments")


class PaymentProvider(Protocol):
    """支付通道协议。三个方法都在服务层事务 (wallet.service_session) 内被调用。"""

    name: str

    async def create_order(
        self, session: AsyncSession, user_id: uuid.UUID, amount_cny: Decimal
    ) -> PaymentOrder:
        """建单 (status=pending)。"""
        ...

    async def confirm(self, session: AsyncSession, order: PaymentOrder) -> Decimal:
        """确认支付: 订单过渡到 paid 并入账, 返回到账后余额。
        调用方必须已 FOR UPDATE 锁单且校验 status='pending' (幂等闸)。"""
        ...

    async def verify_callback(self, payload: dict) -> bool:
        """异步回调验签 (真实通道用); mock 无回调通道, 一律 False。"""
        ...


class MockPaymentProvider:
    """演示通道: 无真实资金流。confirm 即到账, note 固定标注模拟支付。"""

    name = "mock"

    def __init__(self, wallet: WalletService):
        self._wallet = wallet

    async def create_order(
        self, session: AsyncSession, user_id: uuid.UUID, amount_cny: Decimal
    ) -> PaymentOrder:
        # 1 元 = 1 额度 (credit 以 CNY 计价), 量化到 NUMERIC(12,4) 同精度
        order = PaymentOrder(
            user_id=user_id,
            amount_cny=q4(amount_cny),
            credits=q4(amount_cny),
            status="pending",
            provider=self.name,
        )
        session.add(order)
        await session.flush()
        log.info(
            "payment_order_created",
            order_id=str(order.id),
            user_id=str(user_id),
            amount=str(order.amount_cny),
        )
        return order

    async def confirm(self, session: AsyncSession, order: PaymentOrder) -> Decimal:
        order.status = "paid"
        order.paid_at = datetime.now(UTC)
        order.provider_txn_id = f"mock-{uuid.uuid4().hex}"
        balance = await self._wallet.recharge_apply(
            session, order, note="模拟支付-演示通道"
        )
        log.info("payment_order_paid_mock", order_id=str(order.id))
        return balance

    async def verify_callback(self, payload: dict) -> bool:
        return False  # mock 无异步回调通道: 任何回调都不认


def create_payment_provider(settings: Settings, wallet: WalletService) -> PaymentProvider:
    if settings.payment_provider == "mock":
        return MockPaymentProvider(wallet)
    # 真实通道留接口: 配置了未实现的 provider 必须炸响, 不静默降级成 mock
    raise NotImplementedError(
        f"payment provider '{settings.payment_provider}' 未实现 (真实通道仅留接口)"
    )
