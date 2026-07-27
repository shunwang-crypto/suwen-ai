"""预付额度钱包单测 (0006, 无 DB):

- Decimal 计价纪律: float 走 str 中转吸噪声, ×markup 后量化 NUMERIC(12,4) 半进位;
- InsufficientCredits: 继承 BudgetExceeded (网关 precheck 只放行该类冒头),
  code=INSUFFICIENT_CREDITS, balance 随异常携带;
- WalletBudgetGuard 组合预检: 先钱包后预算 / 钱包拦截时预算不再预扣 /
  钱包自身故障 fail-open 不放大不可用性 / settle 原样透传;
- API 错误信封: InsufficientCredits -> 402 + balance 字段 (子类 handler 优先于
  BudgetExceeded 的 429, 且 429 不回归);
- MockPaymentProvider: 无回调通道一律不认; 未实现的真实通道配置必须炸响。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from eduagents_api.wallet import (
    InsufficientCredits,
    WalletBudgetGuard,
    WalletService,
    as_decimal,
    q4,
)
from eduagents_core.config import Settings
from eduagents_core.gateway import BudgetExceeded, UsageEvent


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _event(cost: float = 0.0) -> UsageEvent:
    return UsageEvent(
        request_id="req-1",
        tenant_id="t-1",
        user_id="u-1",
        feature="chat",
        model="m",
        tier="standard",
        prompt_tokens=1,
        completion_tokens=1,
        cached_tokens=0,
        cost_cny=cost,
        latency_ms=1,
        mode="live",
    )


# ---------- Decimal 计价纪律 ----------


def test_charge_is_exact_decimal_with_markup():
    ws = WalletService(_settings())  # billing_markup 默认 3.0
    assert ws.markup == Decimal("3")
    assert ws.charge_for(0.1) == Decimal("0.3000")  # float 0.1 不带二进制噪声进账
    assert ws.charge_for(Decimal("0.0333")) == Decimal("0.0999")


def test_charge_absorbs_float_noise_via_str():
    # 0.1 + 0.2 = 0.30000000000000004 — str 中转后量化必须收敛到精确值
    ws = WalletService(_settings())
    assert as_decimal(0.1 + 0.2) == Decimal("0.30000000000000004")
    assert ws.charge_for(0.1 + 0.2) == Decimal("0.9000")


def test_charge_rounds_half_up_at_4th_decimal():
    ws = WalletService(_settings())
    # 0.00015 × 3 = 0.00045 -> 半进位到 0.0005 (银行家舍入会给 0.0004, 必须区分)
    assert ws.charge_for(Decimal("0.00015")) == Decimal("0.0005")
    assert q4(Decimal("0.00005")) == Decimal("0.0001")


def test_custom_markup_and_grant_from_settings():
    ws = WalletService(_settings(billing_markup=2.5, signup_grant_cny=6.66))
    assert ws.charge_for(Decimal("1")) == Decimal("2.5000")
    assert ws.grant_cny == Decimal("6.6600")


# ---------- InsufficientCredits ----------


def test_insufficient_credits_is_budget_exceeded_subclass():
    exc = InsufficientCredits(balance=Decimal("-0.05"))
    assert isinstance(exc, BudgetExceeded)  # 网关 _budget_precheck 只放行该类冒头
    assert exc.code == "INSUFFICIENT_CREDITS"
    assert exc.balance == Decimal("-0.0500")
    assert exc.retry_after_seconds is None


# ---------- WalletBudgetGuard 组合预检 ----------


class _FakeWallet:
    def __init__(self, balance: Decimal | None = None, boom: bool = False):
        self._balance = balance
        self._boom = boom
        self.calls = 0

    async def precheck_balance(self, user_id: str) -> None:
        self.calls += 1
        if self._boom:
            raise RuntimeError("db down")
        if self._balance is not None and self._balance <= 0:
            raise InsufficientCredits(balance=self._balance)


class _FakeInner:
    def __init__(self, verdict: str | None = None):
        self._verdict = verdict
        self.precheck_calls: list[tuple] = []
        self.settle_calls: list[tuple] = []

    async def precheck(self, tenant_id, user_id, tier="standard"):
        self.precheck_calls.append((tenant_id, user_id, tier))
        return self._verdict

    async def settle(self, event, reserved_tier=None):
        self.settle_calls.append((event, reserved_tier))


async def test_guard_blocks_on_wallet_before_budget():
    inner = _FakeInner()
    guard = WalletBudgetGuard(_FakeWallet(balance=Decimal("-1")), inner)
    with pytest.raises(InsufficientCredits):
        await guard.precheck("t-1", "u-1")
    assert inner.precheck_calls == []  # 钱包拦截时预算不预扣 (先钱包后预算)


async def test_guard_delegates_verdict_when_wallet_ok():
    inner = _FakeInner(verdict="cheap")
    wallet = _FakeWallet(balance=Decimal("5"))
    guard = WalletBudgetGuard(wallet, inner)
    assert await guard.precheck("t-1", "u-1", tier="strong") == "cheap"
    assert wallet.calls == 1
    assert inner.precheck_calls == [("t-1", "u-1", "strong")]


async def test_guard_fails_open_on_wallet_error():
    inner = _FakeInner()
    guard = WalletBudgetGuard(_FakeWallet(boom=True), inner)
    assert await guard.precheck("t-1", "u-1") is None  # 钱包故障不阻断业务
    assert len(inner.precheck_calls) == 1  # 预算预检照常


async def test_guard_settle_passthrough():
    inner = _FakeInner()
    guard = WalletBudgetGuard(_FakeWallet(balance=Decimal("5")), inner)
    ev = _event(0.5)
    await guard.settle(ev, reserved_tier="standard")
    assert inner.settle_calls == [(ev, "standard")]


# ---------- API 错误信封: 402 + balance ----------


def _mini_app():
    from eduagents_api.middleware import install_error_handlers
    from fastapi import FastAPI

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom-credits")
    async def boom_credits():
        raise InsufficientCredits(balance=Decimal("-1.5"))

    @app.get("/boom-budget")
    async def boom_budget():
        raise BudgetExceeded(retry_after_seconds=60)

    return app


def test_insufficient_credits_becomes_402_with_balance():
    from fastapi.testclient import TestClient

    client = TestClient(_mini_app())
    resp = client.get("/boom-credits")
    assert resp.status_code == 402
    body = resp.json()
    assert body["code"] == "INSUFFICIENT_CREDITS"
    assert body["balance"] == "-1.5000"
    assert "充值" in body["message"]
    assert "request_id" in body


def test_budget_exceeded_still_429_not_shadowed():
    # 回归: 子类 handler 注册后, 基类 BudgetExceeded 仍走 429 通道
    from fastapi.testclient import TestClient

    client = TestClient(_mini_app())
    resp = client.get("/boom-budget")
    assert resp.status_code == 429
    assert resp.json()["code"] == "BUDGET_EXCEEDED"


# ---------- 支付通道 ----------


async def test_mock_provider_rejects_callbacks_and_unknown_provider_raises():
    from eduagents_api.payments import MockPaymentProvider, create_payment_provider

    ws = WalletService(_settings())
    provider = create_payment_provider(_settings(), ws)
    assert isinstance(provider, MockPaymentProvider)
    assert provider.name == "mock"
    # mock 无回调通道: 任何 payload 一律不认
    assert await provider.verify_callback({"forged": True}) is False
    # 未实现的真实通道: 必须炸响, 不静默降级成 mock
    with pytest.raises(NotImplementedError):
        create_payment_provider(_settings(payment_provider="wechat"), ws)
