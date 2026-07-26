"""预算强制端到端 (INTEGRATION=1, 真 Redis)。

- RedisBudgetGuard + 真 Redis 验三档阈值与 settle 累加 (Lua 预扣在真 Redis 上跑通);
- OpenAIGateway(live 模式 + 不可达端点 + guard): 超预算时 BudgetExceeded 在
  chat/stream_chat 两条路径都向上传播 (未发出任何网络调用即被拦截);
- 软限 (80-100%) 时请求被降为 cheap 档; 降级兜底 stub 结果不结算, 预扣被整额
  返还, 计数器净变化为零 (P1-12);
- 全局 150% 熔断: 个人额度未满也全档拒绝 (P0-2b)。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from eduagents_api.budget import MICRO_PER_CNY, RedisBudgetGuard
from eduagents_core.config import Settings
from eduagents_core.gateway import (
    BudgetExceeded,
    ChatMessage,
    ChatRequest,
    OpenAIGateway,
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
