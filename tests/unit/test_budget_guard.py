"""RedisBudgetGuard 单测 (fakeredis + 注入时间)。

覆盖: 三档阈值 (<80% / 80-100% / ≥100%), Asia/Shanghai 日界 (UTC 晚间跨天),
日界翻转 (注入时间推进), settle 厘级累加, stub/replay/cache 不计数, TTL 48h,
无用户主体放行, Retry-After 秒数。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from eduagents_api.budget import (
    CN_TZ,
    KEY_TTL_SECONDS,
    MICRO_PER_CNY,
    RedisBudgetGuard,
    budget_key,
    create_budget_guard,
)
from eduagents_core.config import Settings
from eduagents_core.gateway import BudgetExceeded, UsageEvent
from fakeredis import FakeAsyncRedis

DAILY_CNY = 5.0
# UTC 2026-07-26 12:00 = 沪时 2026-07-26 20:00 -> key 日期 20260726
NOON_UTC = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
async def redis():
    r = FakeAsyncRedis(decode_responses=True)
    yield r
    await r.aclose()


def make_guard(redis, now: datetime = NOON_UTC):
    """返回 (guard, clock): clock["now"] 可改, 模拟虚拟时钟推进。"""
    settings = Settings(_env_file=None, user_daily_budget_cny=DAILY_CNY)
    clock = {"now": now}

    async def now_fn() -> datetime:
        return clock["now"]

    return RedisBudgetGuard(redis, settings, now_fn=now_fn), clock


def event(cost: float, mode: str = "live", user: str = "user-1") -> UsageEvent:
    return UsageEvent(
        request_id="req-1",
        tenant_id="tenant-1",
        user_id=user,
        feature="chat",
        model="deepseek-chat",
        tier="standard",
        prompt_tokens=100,
        completion_tokens=100,
        cached_tokens=0,
        cost_cny=cost,
        latency_ms=10,
        mode=mode,
    )


async def _spend(redis, user: str, cny: float, day: str = "20260726") -> None:
    await redis.set(f"budget:{user}:{day}", cny * MICRO_PER_CNY)


async def test_below_soft_threshold_returns_none(redis):
    guard, _ = make_guard(redis)
    await _spend(redis, "user-1", 0.79 * DAILY_CNY)
    assert await guard.precheck("tenant-1", "user-1") is None


async def test_soft_threshold_returns_cheap(redis):
    guard, _ = make_guard(redis)
    await _spend(redis, "user-1", 0.8 * DAILY_CNY)  # 恰 80% 含边界
    assert await guard.precheck("tenant-1", "user-1") == "cheap"
    await _spend(redis, "user-1", 0.99 * DAILY_CNY)
    assert await guard.precheck("tenant-1", "user-1") == "cheap"


async def test_hard_threshold_raises_with_retry_after(redis):
    guard, _ = make_guard(redis)
    await _spend(redis, "user-1", DAILY_CNY)  # 恰 100%
    with pytest.raises(BudgetExceeded) as exc_info:
        await guard.precheck("tenant-1", "user-1")
    assert exc_info.value.code == "BUDGET_EXCEEDED"
    # 沪时 20:00 -> 距次日 00:00 恰 4 小时
    assert exc_info.value.retry_after_seconds == 4 * 3600


async def test_settle_accumulates_in_micro_cny(redis):
    guard, _ = make_guard(redis)
    await guard.settle(event(1.2))
    await guard.settle(event(0.3))
    key = budget_key("user-1", NOON_UTC.astimezone(CN_TZ))
    assert key == "budget:user-1:20260726"  # 沪时日界
    raw = await redis.get(key)
    assert raw is not None and float(raw) == pytest.approx(1500.0)
    ttl = await redis.ttl(key)
    assert 0 < ttl <= KEY_TTL_SECONDS


async def test_stub_replay_cache_modes_not_counted(redis):
    guard, _ = make_guard(redis)
    for mode in ("stub", "replay", "cache"):
        await guard.settle(event(1.0, mode=mode))
    await guard.settle(event(0.0))  # live 但零成本也不写 key
    assert await redis.get("budget:user-1:20260726") is None
    assert await guard.precheck("tenant-1", "user-1") is None


async def test_day_rollover_with_injected_clock(redis):
    guard, clock = make_guard(redis)
    await _spend(redis, "user-1", DAILY_CNY, day="20260726")
    with pytest.raises(BudgetExceeded):
        await guard.precheck("tenant-1", "user-1")
    # 虚拟时钟推进 1 天: 新 key, 预算复位
    clock["now"] = NOON_UTC + timedelta(days=1)
    assert await guard.precheck("tenant-1", "user-1") is None
    # settle 写入新日期 key
    await guard.settle(event(0.5))
    assert float(await redis.get("budget:user-1:20260727")) == pytest.approx(500.0)


async def test_shanghai_day_boundary_not_utc(redis):
    # UTC 2026-07-26 17:00 = 沪时 2026-07-27 01:00 -> 应记在 20260727, 不出现"半夜预算复活"
    guard, _ = make_guard(redis, now=datetime(2026, 7, 26, 17, 0, tzinfo=UTC))
    await guard.settle(event(1.0))
    assert await redis.get("budget:user-1:20260726") is None
    assert float(await redis.get("budget:user-1:20260727")) == pytest.approx(1000.0)


async def test_missing_user_or_disabled_budget_passes(redis):
    guard, _ = make_guard(redis)
    assert await guard.precheck("tenant-1", "") is None
    await guard.settle(event(1.0, user=""))  # 无用户主体不计
    disabled = RedisBudgetGuard(
        redis, Settings(_env_file=None, user_daily_budget_cny=0.0), now_fn=None
    )
    # 预算=0 视为未启用 (而非全员封禁)
    assert await disabled.precheck("tenant-1", "user-1") is None


async def test_factory_uses_virtual_clock(redis):
    # create_budget_guard 缺省走 timectl.now(redis): 写入 clock:offset 偏移应改变日界
    settings = Settings(_env_file=None, user_daily_budget_cny=DAILY_CNY)
    guard = create_budget_guard(redis, settings)
    await redis.set("clock:offset_seconds", 30 * 86400)  # time-travel 30 天
    await guard.settle(event(1.0))
    keys = [k async for k in redis.scan_iter(match="budget:user-1:*")]
    assert len(keys) == 1
    real_today = datetime.now(UTC).strftime("%Y%m%d")
    assert real_today not in keys[0]  # 日期跟随虚拟时钟, 不是系统当天
