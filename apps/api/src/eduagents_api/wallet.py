"""预付额度钱包 (0006): WalletService + 入口余额预检 (先钱包 402, 后预算 429)。

计费口径 (契约已与前端冻结):
- 扣费 = 模型真实成本 cost_cny × billing_markup (Settings, 默认 3.0);
- cost_cny <= 0 (stub/replay/缓存命中/失败返还) 一律零扣费直接跳过;
- 全链 Decimal (str 中转吸掉 float 噪声), NUMERIC(12,4) 半进位量化, 禁 float 运算。

原子性与幂等:
- consume 挂在 usage.persist_usage 的 tenant_session 事务内 (不新开事务):
  usage_log 行与钱包账本一起提交/一起回滚, 不出现单边落账;
- 账户行 SELECT ... FOR UPDATE 行锁 -> 并发扣费串行化, balance_after 精确;
- 重试幂等靠 0006 唯一部分索引 + ON CONFLICT DO NOTHING:
  (user_id) WHERE kind='signup_grant' / (user_id, ref) WHERE kind='consume';
  rowcount=0 即重放, 余额不动。取舍如实声明: 同一 request_id 内的第 2..n 次
  LLM 调用会命中 consume 唯一索引被跳过 (契约冻结 ref=request_id, 宁可少收不双扣)。

余额拦截语义:
- 进行中的调用允许扣到负值, 不打断流 (consume 永不抛余额错误);
- 余额 <=0 的**下一次**请求在网关入口 precheck 被 WalletBudgetGuard 拦截,
  抛 InsufficientCredits -> API 层 402 错误信封 code=INSUFFICIENT_CREDITS (含 balance);
- 无钱包行 (0006 之前的存量用户 / worker 里 org 主体) 放行不拦 —— 首笔扣费落负
  之后自然进入拦截; 钱包预检故障 fail-open (与预算预检同哲学, 不放大不可用性)。

RLS: 写路径必须先在事务内 set_config('app.billing_service','true',true) 标记
服务层身份 (0006 策略), 事务结束即失效; 用户读路径走 tenant_session(user_id)
的本人策略 — 越权读他人钱包/流水得到空集。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal

from eduagents_core.config import Settings, get_settings
from eduagents_core.gateway import BudgetExceeded, GatewayError, UsageEvent
from eduagents_core.logging import get_logger
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_sessionmaker, tenant_session
from .models import WalletAccount, WalletLedger

log = get_logger("wallet")

# NUMERIC(12,4) 同精度量化 (半进位)
_Q4 = Decimal("0.0001")

LEDGER_KINDS = ("signup_grant", "consume", "recharge", "refund", "admin_adjust")


def q4(value: Decimal) -> Decimal:
    """量化到 0.0001 (与 NUMERIC(12,4) 同精度), 半进位。"""
    return value.quantize(_Q4, rounding=ROUND_HALF_UP)


def as_decimal(value: float | int | str | Decimal) -> Decimal:
    """float -> Decimal 必须走 str 中转, 吸掉二进制浮点噪声 (0.1+0.2 之类)。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class InsufficientCredits(BudgetExceeded):
    """余额不足: API 层 402 + code=INSUFFICIENT_CREDITS (含 balance 字段)。

    继承 BudgetExceeded 是刻意的: 网关 _budget_precheck 只放行 BudgetExceeded
    向上传播 (其余异常 fail-open), 子类因此免改 core 层即可穿透到 API 错误信封;
    starlette 按 __mro__ 匹配 handler, 子类 handler 先于 BudgetExceeded 命中。
    """

    def __init__(self, balance: Decimal):
        # 绕过 BudgetExceeded.__init__ 的硬编码 code, 直接落 GatewayError
        GatewayError.__init__(
            self, "INSUFFICIENT_CREDITS", "钱包额度已用完，请充值后继续使用 AI 功能"
        )
        self.retry_after_seconds: int | None = None
        self.balance = q4(balance)


@asynccontextmanager
async def service_session() -> AsyncIterator[AsyncSession]:
    """服务层记账事务: 无租户上下文 + billing_service GUC (0006 写策略唯一通道)。

    仅供钱包/订单的服务层操作 (建单/mock 入账/admin_adjust); 用户读路径一律走
    tenant_session 的本人策略。set_config(is_local=true) 事务级, 连接归池即失效。
    """
    async with get_sessionmaker()() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.billing_service', 'true', true)")
            )
            yield session


class WalletService:
    """钱包服务层 (写路径唯一入口)。金额参数/返回值全部 Decimal。"""

    def __init__(self, settings: Settings):
        self._markup = as_decimal(settings.billing_markup)
        self._grant_cny = q4(as_decimal(settings.signup_grant_cny))

    @property
    def markup(self) -> Decimal:
        return self._markup

    @property
    def grant_cny(self) -> Decimal:
        return self._grant_cny

    def charge_for(self, cost_cny: float | Decimal) -> Decimal:
        """真实成本 -> 应扣额度 (× markup, 量化 4 位)。纯函数, 单测覆盖精度。"""
        return q4(as_decimal(cost_cny) * self._markup)

    # ---------- 事务内原语 (调用方负责事务边界) ----------

    async def _mark_service(self, session: AsyncSession) -> None:
        """当前事务标记为服务层身份 (0006 RLS 写策略), 事务结束即失效。"""
        await session.execute(
            text("SELECT set_config('app.billing_service', 'true', true)")
        )

    async def _lock_balance(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> Decimal | None:
        """行锁取余额; 无行则按 0 补建 (仅当 users 行真实存在 —— worker 里
        org 主体的 user_id 位置放的是 org_id, 不得为其误建钱包)。None = 非真实用户。"""
        balance = await session.scalar(
            select(WalletAccount.balance)
            .where(WalletAccount.user_id == user_id)
            .with_for_update()
        )
        if balance is not None:
            return balance
        await session.execute(
            text(
                "INSERT INTO wallet_accounts (user_id, balance) "
                "SELECT :uid, 0 WHERE EXISTS (SELECT 1 FROM users WHERE id = :uid) "
                "ON CONFLICT (user_id) DO NOTHING"
            ),
            {"uid": user_id},
        )
        return await session.scalar(
            select(WalletAccount.balance)
            .where(WalletAccount.user_id == user_id)
            .with_for_update()
        )

    async def _set_balance(
        self, session: AsyncSession, user_id: uuid.UUID, new_balance: Decimal
    ) -> None:
        await session.execute(
            update(WalletAccount)
            .where(WalletAccount.user_id == user_id)
            .values(balance=new_balance)
        )

    async def grant_signup(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> Decimal:
        """注册赠送 (挂在 register 的 plain_session 事务内, 与建号原子)。

        幂等: (user_id) WHERE kind='signup_grant' 唯一索引, 重放返回现余额不重发。
        """
        await self._mark_service(session)
        balance = await self._lock_balance(session, user_id)
        if balance is None:  # register 流程内不可能; 防御性兜底
            log.warning("wallet_grant_no_user", user_id=str(user_id))
            return Decimal("0")
        new_balance = q4(balance + self._grant_cny)
        result = await session.execute(
            pg_insert(WalletLedger)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                delta=self._grant_cny,
                balance_after=new_balance,
                kind="signup_grant",
                ref=None,
                note="注册赠送",
            )
            .on_conflict_do_nothing(
                index_elements=["user_id"],
                index_where=text("kind = 'signup_grant'"),
            )
        )
        if result.rowcount == 0:
            log.info("wallet_grant_duplicate", user_id=str(user_id))
            return balance
        await self._set_balance(session, user_id, new_balance)
        log.info("wallet_signup_granted", user_id=str(user_id), amount=str(self._grant_cny))
        return new_balance

    async def consume(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        cost_cny: float | Decimal,
        request_id: str,
        feature: str,
    ) -> Decimal | None:
        """扣费 (usage_log 同事务): 行锁 + markup 计价 + 账本落账。

        - cost_cny <= 0 直接跳过 (stub/replay/缓存命中免费);
        - 允许扣到负值 (进行中调用不打断), 拦截交给下一次请求的入口 precheck;
        - 同 (user_id, request_id) 的重放命中唯一索引 -> 跳过, 余额不动;
        - 返回扣费后余额; 跳过时返回 None/现余额 (仅供日志, 调用方不依赖)。
        """
        cost = as_decimal(cost_cny)
        if cost <= 0:
            return None
        charge = q4(cost * self._markup)
        if charge <= 0:
            return None
        await self._mark_service(session)
        balance = await self._lock_balance(session, user_id)
        if balance is None:
            # org 主体/已注销用户: 无个人钱包可扣, 如实记日志不伪造账目
            log.warning("wallet_consume_no_user", user_id=str(user_id), feature=feature)
            return None
        new_balance = q4(balance - charge)
        # 无 request_id 的事件退化为一次性凭证 (无法去重, 但不能互相撞唯一索引)
        ref = (request_id or f"noreq-{uuid.uuid4().hex}")[:128]
        result = await session.execute(
            pg_insert(WalletLedger)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                delta=-charge,
                balance_after=new_balance,
                kind="consume",
                ref=ref,
                note=f"AI 调用扣费: {feature}"[:256],
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "ref"],
                index_where=text("kind = 'consume'"),
            )
        )
        if result.rowcount == 0:
            log.info("wallet_consume_duplicate", user_id=str(user_id), ref=ref)
            return balance
        await self._set_balance(session, user_id, new_balance)
        log.info(
            "wallet_consumed",
            user_id=str(user_id),
            feature=feature,
            charge=str(charge),
            balance=str(new_balance),
        )
        return new_balance

    async def recharge_apply(
        self,
        session: AsyncSession,
        order,
        note: str = "充值到账",
    ) -> Decimal:
        """订单入账 (kind=recharge, ref=order_id)。幂等由调用方的订单状态机保证:
        FOR UPDATE 锁单 + status='pending' 才允许过渡到 paid 后调本方法。"""
        await self._mark_service(session)
        balance = await self._lock_balance(session, order.user_id)
        if balance is None:
            raise ValueError(f"wallet_recharge_no_user: {order.user_id}")
        credits = as_decimal(order.credits)
        new_balance = q4(balance + credits)
        session.add(
            WalletLedger(
                user_id=order.user_id,
                delta=credits,
                balance_after=new_balance,
                kind="recharge",
                ref=str(order.id)[:128],
                note=note[:256],
            )
        )
        await self._set_balance(session, order.user_id, new_balance)
        log.info(
            "wallet_recharged",
            user_id=str(order.user_id),
            order_id=str(order.id),
            credits=str(credits),
        )
        return new_balance

    async def admin_adjust(
        self, user_id: uuid.UUID, delta: Decimal, note: str = ""
    ) -> Decimal:
        """管理员调账 (kind=admin_adjust, 可正可负): 独立服务层事务。"""
        async with service_session() as session:
            balance = await self._lock_balance(session, user_id)
            if balance is None:
                raise ValueError(f"wallet_adjust_no_user: {user_id}")
            new_balance = q4(balance + as_decimal(delta))
            session.add(
                WalletLedger(
                    user_id=user_id,
                    delta=q4(as_decimal(delta)),
                    balance_after=new_balance,
                    kind="admin_adjust",
                    ref=None,
                    note=(note or "管理员调账")[:256],
                )
            )
            await self._set_balance(session, user_id, new_balance)
            log.info("wallet_adjusted", user_id=str(user_id), delta=str(delta))
            return new_balance

    # ---------- 读路径 (本人 RLS 策略) ----------

    async def balance(self, user_id: uuid.UUID) -> Decimal:
        """本人余额 (tenant_session 走 0006 owner_read 策略); 无行 = 0。"""
        async with tenant_session(user_id) as session:
            bal = await session.scalar(
                select(WalletAccount.balance).where(WalletAccount.user_id == user_id)
            )
        return q4(bal) if bal is not None else Decimal("0.0000")

    async def precheck_balance(self, user_id: str) -> None:
        """入口余额预检: 余额 <=0 抛 InsufficientCredits (402)。

        无用户主体/非 uuid/无钱包行 一律放行 (存量用户与 org 主体 fail-open,
        首笔扣费落负后自然进入拦截), 不放大不可用性。
        """
        if not user_id:
            return
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            return
        async with tenant_session(uid) as session:
            bal = await session.scalar(
                select(WalletAccount.balance).where(WalletAccount.user_id == uid)
            )
        if bal is not None and bal <= 0:
            log.warning("wallet_blocked", user_id=user_id, balance=str(bal))
            raise InsufficientCredits(balance=bal)


class WalletBudgetGuard:
    """网关入口组合预检: 先钱包 (402) 后预算 (429), 与 RedisBudgetGuard 并列。

    实现 eduagents_core.gateway.BudgetGuard 协议; settle 原样透传给预算 guard
    (钱包扣费不在 settle —— 它挂在 usage_log 写入点的同一事务, 见 usage.py)。
    """

    def __init__(self, wallet: WalletService, inner) -> None:
        self._wallet = wallet
        self._inner = inner

    async def precheck(
        self, tenant_id: str, user_id: str, tier: str = "standard"
    ) -> str | None:
        try:
            await self._wallet.precheck_balance(user_id)
        except BudgetExceeded:
            raise  # InsufficientCredits 冒头 -> API 层 402
        except Exception:  # noqa: BLE001 — 钱包预检故障 fail-open, 预算预检照常
            log.exception("wallet_precheck_failed_open")
        return await self._inner.precheck(tenant_id, user_id, tier)

    async def settle(self, event: UsageEvent, reserved_tier: str | None = None) -> None:
        await self._inner.settle(event, reserved_tier)


def get_wallet_service() -> WalletService:
    """按当前 Settings 构造 (无进程级缓存: 测试 cache_clear 后立即生效, 构造成本可忽略)。"""
    return WalletService(get_settings())


def create_billing_guard(redis, settings: Settings) -> WalletBudgetGuard:
    """main.py / worker 接线工厂: 钱包预检 + 预算预检的组合 guard。
    OpenAIGateway(settings, usage_sink=..., budget_guard=create_billing_guard(redis, settings))
    """
    from .budget import create_budget_guard

    return WalletBudgetGuard(WalletService(settings), create_budget_guard(redis, settings))
