"""预算强制端到端 (INTEGRATION=1, 真 Redis)。

- RedisBudgetGuard + 真 Redis 验三档阈值与 settle 累加 (Lua 预扣在真 Redis 上跑通);
- OpenAIGateway(live 模式 + 不可达端点 + guard): 超预算时 BudgetExceeded 在
  chat/stream_chat 两条路径都向上传播 (未发出任何网络调用即被拦截);
- 软限 (80-100%) 时请求被降为 cheap 档; 降级兜底 stub 结果不结算, 预扣被整额
  返还, 计数器净变化为零 (P1-12);
- 全局 150% 熔断: 个人额度未满也全档拒绝 (P0-2b)。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from eduagents_api.budget import MICRO_PER_CNY, RedisBudgetGuard
from eduagents_core.config import Settings
from eduagents_core.gateway import (
    BudgetExceeded,
    ChatMessage,
    ChatRequest,
    GatewayError,
    OpenAIGateway,
    StreamDelta,
    Tier,
    UsageEvent,
)
from redis.asyncio import Redis

from .conftest import requires_services

pytestmark = requires_services

DAILY_CNY = 5.0
# UTC 04:00 = 沪时 12:00 -> key 日期 20260726
FIXED_NOW = datetime(2026, 7, 26, 4, 0, tzinfo=UTC)
DAY = "20260726"


async def _fixed_now() -> datetime:
    return FIXED_NOW


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, user_daily_budget_cny=DAILY_CNY, **overrides)


def _gateway_settings(tmp_path) -> Settings:
    # live 模式 + 本机不可达端点 (假 key): 预算拦截必须发生在任何网络调用之前;
    # 软限用例里降级链快速失败后落到 stub 兜底 (dev 环境允许)
    return _settings(
        gateway_mode="live",
        app_env="dev",
        gateway_replay_fallback_stub=True,
        gateway_max_retries=1,
        gateway_timeout_seconds=1.0,
        gateway_cassette_dir=str(tmp_path / "cassettes"),
        cheap_api_key="fake-key",
        cheap_base_url="http://127.0.0.1:9",
        standard_api_key="fake-key",
        standard_base_url="http://127.0.0.1:9",
        strong_api_key="fake-key",
        strong_base_url="http://127.0.0.1:9",
    )


@pytest.fixture
async def redis():
    r = Redis.from_url(Settings(_env_file=None).redis_url, decode_responses=True)
    created: list[str] = []
    yield r, created
    if created:
        await r.delete(*created)
    await r.aclose()


def _req(user: str, tier: Tier = Tier.standard) -> ChatRequest:
    return ChatRequest(
        messages=(ChatMessage(role="user", content="什么是泰勒公式?"),),
        tier=tier,
        tenant_id=str(uuid.uuid4()),
        user_id=user,
        request_id="req-budget-e2e",
    )


async def _seed(redis, created, user: str, ratio: float) -> str:
    key = f"budget:{user}:{DAY}"
    created.append(key)
    await redis.set(key, ratio * DAILY_CNY * MICRO_PER_CNY)
    return key


async def test_three_thresholds_and_settle_on_real_redis(redis):
    r, created = redis
    guard = RedisBudgetGuard(r, _settings(), now_fn=_fixed_now)
    user = f"e2e-{uuid.uuid4().hex[:12]}"
    key = f"budget:{user}:{DAY}"
    created.append(key)
    created.append(f"budget:global:{DAY}")  # settle/预扣 同步累加全局键 (P0-2b)

    # <80%: settle 累加后仍正常放行
    def live_event(cost: float) -> UsageEvent:
        return UsageEvent(
            request_id="r", tenant_id="t", user_id=user, feature="chat",
            model="m", tier="standard", prompt_tokens=1, completion_tokens=1,
            cached_tokens=0, cost_cny=cost, latency_ms=1, mode="live",
        )

    await guard.settle(live_event(1.0))
    await guard.settle(live_event(2.0))
    assert float(await r.get(key)) == pytest.approx(3000.0)  # 厘级累加
    assert await guard.precheck("t", user) is None

    # 80-100%: cheap
    await guard.settle(live_event(1.5))  # 累计 4.5 = 90%
    assert await guard.precheck("t", user) == "cheap"

    # >=100%: BudgetExceeded + Retry-After (沪时 12:00 -> 距午夜 12 小时)
    await guard.settle(live_event(0.5))  # 累计 5.0 = 100%
    with pytest.raises(BudgetExceeded) as exc_info:
        await guard.precheck("t", user)
    assert exc_info.value.code == "BUDGET_EXCEEDED"
    assert exc_info.value.retry_after_seconds == 12 * 3600


async def test_gateway_chat_propagates_budget_exceeded(redis, tmp_path):
    r, created = redis
    settings = _gateway_settings(tmp_path)
    guard = RedisBudgetGuard(r, settings, now_fn=_fixed_now)
    gw = OpenAIGateway(settings, budget_guard=guard)
    user = f"e2e-{uuid.uuid4().hex[:12]}"
    await _seed(r, created, user, ratio=1.0)

    with pytest.raises(BudgetExceeded) as exc_info:
        await gw.chat(_req(user))
    assert exc_info.value.code == "BUDGET_EXCEEDED"


async def test_gateway_stream_propagates_budget_exceeded(redis, tmp_path):
    r, created = redis
    settings = _gateway_settings(tmp_path)
    guard = RedisBudgetGuard(r, settings, now_fn=_fixed_now)
    gw = OpenAIGateway(settings, budget_guard=guard)
    user = f"e2e-{uuid.uuid4().hex[:12]}"
    await _seed(r, created, user, ratio=1.1)

    stream = gw.stream_chat(_req(user))
    with pytest.raises(BudgetExceeded):
        await anext(stream)


async def test_gateway_soft_limit_downgrades_to_cheap(redis, tmp_path):
    r, created = redis
    settings = _gateway_settings(tmp_path)
    guard = RedisBudgetGuard(r, settings, now_fn=_fixed_now)
    gw = OpenAIGateway(settings, budget_guard=guard)
    user = f"e2e-{uuid.uuid4().hex[:12]}"
    key = await _seed(r, created, user, ratio=0.9)
    created.append(f"budget:global:{DAY}")
    before = float(await r.get(key))

    result = await gw.chat(_req(user, tier=Tier.standard))
    # 端点全部不可达 -> 降级链落到 stub 兜底; tier 已被预算降档为 cheap
    assert result.tier == "cheap"
    assert result.mode == "stub"
    # stub 兜底 (mode!=live) 零成本结算: 预扣整额返还, 计数器净变化为零 (P1-12)
    assert float(await r.get(key)) == pytest.approx(before)


async def test_global_kill_switch_blocks_even_fresh_user(redis, tmp_path):
    """P0-2b: 全局 ≥150% 全档熔断 — 个人额度全空的新用户也被拒。"""
    r, created = redis
    settings = _gateway_settings(tmp_path)
    guard = RedisBudgetGuard(r, settings, now_fn=_fixed_now)
    gw = OpenAIGateway(settings, budget_guard=guard)
    gkey = f"budget:global:{DAY}"
    created.append(gkey)
    await r.set(
        gkey, 1.5 * settings.global_daily_budget_cny * MICRO_PER_CNY
    )

    user = f"e2e-{uuid.uuid4().hex[:12]}"
    with pytest.raises(BudgetExceeded):
        await gw.chat(_req(user))
    # 拒绝路径不预扣: 全局键数值不变
    assert float(await r.get(gkey)) == pytest.approx(
        1.5 * settings.global_daily_budget_cny * MICRO_PER_CNY
    )



async def test_worker_replays_completed_usage_once_after_api_exit(redis, client):
    from eduagents_api.budget import replay_budget_usage
    from eduagents_api.db import tenant_session
    from eduagents_api.models import UsageLog, WalletLedger
    from eduagents_api.usage import persist_usage
    from sqlalchemy import func, select

    response = await client.post("/api/auth/register", json={
        "email": f"billing-{uuid.uuid4().hex[:12]}@test.eduagents.cn", "password": "pass-12345",
    })
    assert response.status_code == 201
    user = response.json()["id"]
    db, created = redis
    guard = RedisBudgetGuard(db, _settings(), now_fn=_fixed_now)
    _, ticket = await guard.reserve(user, user, "standard", "model-turn-1")
    created.extend([f"budget:call:{ticket.id}", f"budget:{user}:{DAY}", f"budget:global:{DAY}"])
    event = UsageEvent("request", user, user, "chat", "flash", "standard", 10, 10, 0, .04, 30,
                       "live", platform_billed_cost_cny=.04, billing_reference="model-turn-1")
    await guard.settle(event, ticket)
    await replay_budget_usage({"redis": db})
    # A late original sink invocation must not duplicate the worker commit.
    assert await persist_usage(event)
    async with tenant_session(user) as session:
        assert await session.scalar(select(func.count()).where(UsageLog.id == uuid.UUID(ticket.id))) == 1
        assert await session.scalar(select(func.count()).where(
            WalletLedger.user_id == uuid.UUID(user), WalletLedger.ref == "model-turn-1")) == 1
    assert not await db.hexists("budget:usage:outbox", ticket.id)


async def test_interrupted_paid_stream_survives_failed_commit_and_concurrent_replay(
    redis, client, monkeypatch, tmp_path,
):
    from eduagents_api import usage
    from eduagents_api.budget import replay_budget_usage
    from eduagents_api.db import tenant_session
    from eduagents_api.models import UsageLog, WalletAccount, WalletLedger
    from eduagents_api.wallet import WalletService
    from sqlalchemy import select

    registered = await client.post("/api/auth/register", json={
        "email": f"stream-metering-{uuid.uuid4().hex[:12]}@test.eduagents.cn",
        "password": "pass-12345",
    })
    assert registered.status_code == 201
    user = uuid.UUID(registered.json()["id"])
    owner = str(user)
    request = ChatRequest(
        messages=(ChatMessage("user", "导数"),),
        tenant_id=owner,
        user_id=owner,
        request_id="paid-stream-test",
        billing_reference="paid-stream-model-call",
    )
    db, created = redis
    user_key, global_key = f"budget:{owner}:{DAY}", f"budget:global:{DAY}"
    created.extend([user_key, global_key])
    settings = _settings(
        gateway_mode="live",
        gateway_cassette_dir=str(tmp_path),
        model_routing_enabled=True,
        trenddao_api_key="test-key",
        trenddao_input_price_per_m=5000,
        trenddao_output_price_per_m=15000,
        billing_markup=3.0,
    )
    monkeypatch.setattr("eduagents_api.wallet.get_wallet_service", lambda: WalletService(settings))
    guard = RedisBudgetGuard(db, settings, now_fn=_fixed_now)

    async def upstream(_request, _candidate):
        async def response():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="答案"))], usage=None)
            raise TimeoutError("upstream timed out before sending final usage")

        return response()

    async def snapshot():
        async with tenant_session(user) as session:
            balance = await session.scalar(select(WalletAccount.balance).where(WalletAccount.user_id == user))
            logs = (await session.scalars(select(UsageLog).where(UsageLog.user_id == user))).all()
            charges = (await session.scalars(select(WalletLedger).where(
                WalletLedger.user_id == user, WalletLedger.kind == "consume",
            ))).all()
        return balance, logs, charges

    starting_balance, logs, charges = await snapshot()
    assert not logs and not charges

    async def fail_before_commit(service, *args, **kwargs):
        await consume(service, *args, **kwargs)
        raise ConnectionError("injected failure after wallet writes, before transaction commit")

    persist_usage = usage.persist_usage
    consume = WalletService.consume
    online_events = []

    async def online_sink(event):
        online_events.append(event)
        return await persist_usage(event)

    gateway = OpenAIGateway(settings, usage_sink=online_sink, budget_guard=guard)
    monkeypatch.setattr(gateway, "_create_candidate_stream", upstream)
    with monkeypatch.context() as patch:
        patch.setattr(WalletService, "consume", fail_before_commit)
        stream = gateway.stream_chat(request)
        assert await anext(stream) == StreamDelta("答案")
        with pytest.raises(GatewayError, match="MODEL_STREAM_INTERRUPTED"):
            await anext(stream)

    assert await snapshot() == (starting_balance, [], [])
    assert len(online_events) == 1
    ticket_id = online_events[0].settlement_id
    assert ticket_id, "partial usage must keep its reservation ID for outbox replay"
    created.append(f"budget:call:{ticket_id}")
    assert await db.hexists("budget:usage:outbox", ticket_id)

    # Both worker instances must have read the same pending event before either commits it.
    same_event = asyncio.Barrier(2)

    async def replay_sink(event):
        if event.settlement_id == ticket_id:
            await same_event.wait()
        return await persist_usage(event)

    monkeypatch.setattr(usage, "persist_usage", replay_sink)
    redis_url = Settings(_env_file=None).redis_url
    async with Redis.from_url(redis_url, decode_responses=True) as second_worker, asyncio.timeout(10):
        await asyncio.gather(replay_budget_usage({"redis": db}), replay_budget_usage({"redis": second_worker}))

    balance, logs, charges = await snapshot()
    assert len(logs) == len(charges) == 1
    assert logs[0].id == uuid.UUID(ticket_id)
    assert logs[0].tenant_id == logs[0].user_id == charges[0].user_id == user
    assert logs[0].request_id == request.request_id and charges[0].ref == request.billing_reference
    assert (logs[0].prompt_tokens, logs[0].completion_tokens) == (2, 2)
    assert logs[0].platform_billed_cost_cny == pytest.approx(0.04)
    assert charges[0].delta == Decimal("-0.1200")
    assert charges[0].balance_after == balance == starting_balance - Decimal("0.1200")
    assert float(await db.get(user_key)) == float(await db.get(global_key)) == 40
    assert not await db.hexists("budget:usage:outbox", ticket_id)
