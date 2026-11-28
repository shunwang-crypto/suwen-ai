"""RedisBudgetGuard 单测 (fakeredis[lua] + 注入时间/注入 admin 判定)。

覆盖: 用户三档阈值 (<80% / 80-100% / ≥100%), 全局熔断三档 (80% 强制 cheap /
100% 拒非 admin / 150% 全档拒绝含 admin), Lua 原子预扣 + settle 差额返还 (P1-12),
20 并发不超发, 预算日期用真实时钟不随 timectl 偏移, Asia/Shanghai 日界,
stub/replay/cache 不计数, TTL 48h, 无用户主体放行, Retry-After 秒数。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from eduagents_api.budget import (
    CN_TZ,
    KEY_TTL_SECONDS,
    MICRO_PER_CNY,
    RESERVE_CNY_PER_TIER,
    RedisBudgetGuard,
    budget_key,
    create_budget_guard,
    global_budget_key,
)
from eduagents_core.config import Settings
from eduagents_core.gateway import BudgetExceeded, UsageEvent
from fakeredis import FakeAsyncRedis

DAILY_CNY = 5.0
GLOBAL_CNY = 50.0
# UTC 2026-07-26 12:00 = 沪时 2026-07-26 20:00 -> key 日期 20260726
NOON_UTC = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
DAY = "20260726"
# 档位预扣额 (厘)
EST_STANDARD = RESERVE_CNY_PER_TIER["standard"] * MICRO_PER_CNY  # 20
EST_CHEAP = RESERVE_CNY_PER_TIER["cheap"] * MICRO_PER_CNY  # 5
EST_PLAN = RESERVE_CNY_PER_TIER["plan"] * MICRO_PER_CNY  # 20


@pytest.fixture
async def redis():
    r = FakeAsyncRedis(decode_responses=True)
    yield r
    await r.aclose()


def make_guard(
    redis,
    now: datetime = NOON_UTC,
    *,
    is_admin: bool = False,
    global_cny: float = GLOBAL_CNY,
    daily_cny: float = DAILY_CNY,
):
    """返回 (guard, clock): clock["now"] 可改, 模拟日界推进 (注入时间, 非 timectl)。"""
    settings = Settings(
        _env_file=None,
        user_daily_budget_cny=daily_cny,
        global_daily_budget_cny=global_cny,
    )
    clock = {"now": now}

    async def now_fn() -> datetime:
        return clock["now"]

    async def is_admin_fn(user_id: str) -> bool:
        return is_admin

    return (
        RedisBudgetGuard(redis, settings, now_fn=now_fn, is_admin_fn=is_admin_fn),
        clock,
    )


def event(
    cost: float, mode: str = "live", user: str = "user-1", tier: str = "standard"
) -> UsageEvent:
    return UsageEvent(
        request_id="req-1",
        tenant_id="tenant-1",
        user_id=user,
        feature="chat",
        model="deepseek-chat",
        tier=tier,
        prompt_tokens=100,
        completion_tokens=100,
        cached_tokens=0,
        cost_cny=cost,
        latency_ms=10,
        mode=mode,
    )


async def _spend(redis, user: str, cny: float, day: str = DAY) -> None:
    await redis.set(f"budget:{user}:{day}", cny * MICRO_PER_CNY)


async def _spend_global(redis, cny: float, day: str = DAY) -> None:
    await redis.set(f"budget:global:{day}", cny * MICRO_PER_CNY)


# ---------- 用户三档 ----------


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


# ---------- 全局熔断三档 (P0-2b) ----------


async def test_global_soft_forces_cheap_for_everyone(redis):
    guard, _ = make_guard(redis)
    await _spend_global(redis, 0.8 * GLOBAL_CNY)
    # 全新用户 (个人 0 支出) 也被全站强制 cheap
    assert await guard.precheck("tenant-1", "fresh-user") == "cheap"


async def test_global_hard_rejects_non_admin(redis):
    guard, _ = make_guard(redis, is_admin=False)
    await _spend_global(redis, GLOBAL_CNY)
    with pytest.raises(BudgetExceeded) as exc_info:
        await guard.precheck("tenant-1", "fresh-user")
    assert exc_info.value.code == "BUDGET_EXCEEDED"
    assert exc_info.value.retry_after_seconds == 4 * 3600
    # 拒绝路径不预扣: 全局键纹丝不动
    assert float(await redis.get(f"budget:global:{DAY}")) == pytest.approx(
        GLOBAL_CNY * MICRO_PER_CNY
    )


async def test_global_hard_allows_admin_as_cheap(redis):
    guard, _ = make_guard(redis, is_admin=True)
    await _spend_global(redis, GLOBAL_CNY)
    assert await guard.precheck("tenant-1", "admin-user") == "cheap"
    # admin 放行后完成预扣
    assert float(await redis.get(f"budget:global:{DAY}")) == pytest.approx(
        GLOBAL_CNY * MICRO_PER_CNY + EST_STANDARD
    )


async def test_global_kill_switch_rejects_admin_too(redis):
    guard, _ = make_guard(redis, is_admin=True)
    await _spend_global(redis, 1.5 * GLOBAL_CNY)
    with pytest.raises(BudgetExceeded):
        await guard.precheck("tenant-1", "admin-user")


async def test_global_disabled_when_zero(redis):
    guard, _ = make_guard(redis, global_cny=0.0)
    await _spend_global(redis, 10 * GLOBAL_CNY)  # 键存在但熔断关闭
    assert await guard.precheck("tenant-1", "user-1") is None


# ---------- Lua 原子预扣 + 差额返还 (P1-12) ----------


async def test_precheck_reserves_estimate_on_both_keys(redis):
    guard, _ = make_guard(redis)
    assert await guard.precheck("tenant-1", "user-1", tier="cheap") is None
    assert float(await redis.get(f"budget:user-1:{DAY}")) == pytest.approx(EST_CHEAP)
    assert float(await redis.get(f"budget:global:{DAY}")) == pytest.approx(EST_CHEAP)
    ttl = await redis.ttl(f"budget:user-1:{DAY}")
    assert 0 < ttl <= KEY_TTL_SECONDS


async def test_settle_reconciles_reservation_delta(redis):
    guard, _ = make_guard(redis)
    await guard.precheck("tenant-1", "user-1", tier="standard")  # 预扣 20 厘
    # 实际成本 0.008 元 = 8 厘 -> 返还 12 厘
    await guard.settle(event(0.008), reserved_tier="standard")
    assert float(await redis.get(f"budget:user-1:{DAY}")) == pytest.approx(8.0)
    assert float(await redis.get(f"budget:global:{DAY}")) == pytest.approx(8.0)


async def test_plan_reservation_and_settlement_use_plan_tier(redis):
    guard, _ = make_guard(redis)
    assert await guard.precheck("tenant-1", "user-1", tier="plan") is None
    assert float(await redis.get(f"budget:user-1:{DAY}")) == pytest.approx(EST_PLAN)
    assert float(await redis.get(f"budget:global:{DAY}")) == pytest.approx(EST_PLAN)

    # Actual plan cost is lower than its reservation; settle returns the
    # difference while retaining the usage event's plan tier.
    await guard.settle(event(0.008, tier="plan"), reserved_tier="plan")
    assert float(await redis.get(f"budget:user-1:{DAY}")) == pytest.approx(8.0)
    assert float(await redis.get(f"budget:global:{DAY}")) == pytest.approx(8.0)


async def test_failed_call_refunds_full_reservation(redis):
    guard, _ = make_guard(redis)
    await guard.precheck("tenant-1", "user-1", tier="standard")
    # 调用彻底失败: 网关以零成本非 live 事件返还整笔预扣
    await guard.settle(event(0.0, mode="error"), reserved_tier="standard")
    assert float(await redis.get(f"budget:user-1:{DAY}")) == pytest.approx(0.0)
    assert float(await redis.get(f"budget:global:{DAY}")) == pytest.approx(0.0)


async def test_concurrent_precheck_no_overspend(redis):
    """20 并发同时 precheck: 原子预扣下放行数恰等于剩余额度容量, 不再超发。

    预算 0.1 元 = 100 厘, standard 预扣 20 厘 -> 容量恰 5 个;
    旧 check-then-act 实现下 20 个会全部读到 spent=0 全放行。
    """
    guard, _ = make_guard(redis, daily_cny=0.1)

    async def attempt() -> bool:
        try:
            await guard.precheck("tenant-1", "user-1", tier="standard")
            return True
        except BudgetExceeded:
            return False

    outcomes = await asyncio.gather(*(attempt() for _ in range(20)))
    assert sum(outcomes) == 5
    assert float(await redis.get(f"budget:user-1:{DAY}")) == pytest.approx(100.0)
    # 各自按实际成本 = 预扣额结算 -> 总额不变 (预扣与结算闭环)
    for _ in range(sum(outcomes)):
        await guard.settle(event(0.02), reserved_tier="standard")
    assert float(await redis.get(f"budget:user-1:{DAY}")) == pytest.approx(100.0)


async def test_concurrent_at_edge_only_one_passes(redis):
    guard, _ = make_guard(redis)
    await _spend(redis, "user-1", 0.998 * DAILY_CNY)  # 4990 厘, 距上限 10 厘

    async def attempt() -> bool:
        try:
            await guard.precheck("tenant-1", "user-1", tier="standard")
            return True
        except BudgetExceeded:
            return False

    outcomes = await asyncio.gather(*(attempt() for _ in range(20)))
    assert sum(outcomes) == 1  # 第一个预扣后其余 19 个原子地看到 ≥100%


# ---------- 日界与时钟 ----------


async def test_settle_accumulates_in_micro_cny(redis):
    guard, _ = make_guard(redis)
    await guard.settle(event(1.2))
    await guard.settle(event(0.3))
    key = budget_key("user-1", NOON_UTC.astimezone(CN_TZ))
    assert key == f"budget:user-1:{DAY}"  # 沪时日界
    raw = await redis.get(key)
    assert raw is not None and float(raw) == pytest.approx(1500.0)
    # P0-2b: settle 同时累加全局键 (同一 pipeline)
    gkey = global_budget_key(NOON_UTC.astimezone(CN_TZ))
    assert float(await redis.get(gkey)) == pytest.approx(1500.0)
    ttl = await redis.ttl(key)
    assert 0 < ttl <= KEY_TTL_SECONDS


async def test_stub_replay_cache_modes_not_counted(redis):
    guard, _ = make_guard(redis)
    for mode in ("stub", "replay", "cache"):
        await guard.settle(event(1.0, mode=mode))
    await guard.settle(event(0.0))  # live 但零成本也不写 key
    assert await redis.get(f"budget:user-1:{DAY}") is None
    assert await redis.get(f"budget:global:{DAY}") is None
    assert await guard.precheck("tenant-1", "user-1") is None


async def test_day_rollover_with_injected_clock(redis):
    guard, clock = make_guard(redis)
    await _spend(redis, "user-1", DAILY_CNY, day="20260726")
    with pytest.raises(BudgetExceeded):
        await guard.precheck("tenant-1", "user-1")
    # 注入时间推进 1 天: 新 key, 预算复位 (precheck 放行并预扣 standard 20 厘)
    clock["now"] = NOON_UTC + timedelta(days=1)
    assert await guard.precheck("tenant-1", "user-1") is None
    # settle 写入新日期 key: 预扣 20 + 实付 500
    await guard.settle(event(0.5))
    assert float(await redis.get("budget:user-1:20260727")) == pytest.approx(
        500.0 + EST_STANDARD
    )


async def test_shanghai_day_boundary_not_utc(redis):
    # UTC 2026-07-26 17:00 = 沪时 2026-07-27 01:00 -> 应记在 20260727, 不出现"半夜预算复活"
    guard, _ = make_guard(redis, now=datetime(2026, 7, 26, 17, 0, tzinfo=UTC))
    await guard.settle(event(1.0))
    assert await redis.get("budget:user-1:20260726") is None
    assert float(await redis.get("budget:user-1:20260727")) == pytest.approx(1000.0)


async def test_budget_date_ignores_timectl_offset(redis):
    """P1-12 后半: time-travel 演示按钮不得清零全站预算。

    create_budget_guard 缺省时钟 = 真实系统时钟; 写入 clock:offset_seconds
    (timectl 虚拟时钟偏移 30 天) 后, 预算键日期必须仍是真实当天。
    """
    settings = Settings(
        _env_file=None,
        user_daily_budget_cny=DAILY_CNY,
        global_daily_budget_cny=GLOBAL_CNY,
    )
    guard = create_budget_guard(redis, settings)
    await redis.set("clock:offset_seconds", 30 * 86400)  # time-travel 30 天
    await guard.settle(event(1.0))
    keys = [k async for k in redis.scan_iter(match="budget:user-1:*")]
    assert len(keys) == 1
    real_today = datetime.now(UTC).astimezone(CN_TZ).strftime("%Y%m%d")
    assert keys[0] == f"budget:user-1:{real_today}"  # 不随虚拟时钟偏移


async def test_missing_user_or_disabled_budget_passes(redis):
    guard, _ = make_guard(redis)
    # 无用户主体: 不做用户档校验 (仍受全局熔断约束, 预扣只打全局键)
    assert await guard.precheck("tenant-1", "") is None
    assert await redis.get(f"budget:user-1:{DAY}") is None
    await guard.settle(event(1.0, user=""))  # 无用户主体只计全局
    assert await redis.get(f"budget:user-1:{DAY}") is None
    # 全局键 = precheck 预扣 20 (standard 缺省档) + settle 1000
    assert float(await redis.get(f"budget:global:{DAY}")) == pytest.approx(
        1000.0 + EST_STANDARD
    )
    disabled, _ = make_guard(redis, daily_cny=0.0)
    # 用户预算=0 视为未启用 (而非全员封禁)
    assert await disabled.precheck("tenant-1", "user-9") is None


async def test_reservation_replayed_after_midnight(redis):
    guard, clock = make_guard(redis)
    _, ticket = await guard.reserve("tenant-1", "user-1", "standard", "call-A")
    assert await redis.hget(f"budget:call:{ticket.id}", "status") == "reserved"
    clock["now"] += timedelta(days=1)
    result = event(0.08)
    await guard.settle(result, ticket)
    await guard.settle(result, ticket)
    assert float(await redis.get(f"budget:user-1:{DAY}")) == 80
    assert not await redis.exists("budget:user-1:20260727")
    assert await redis.hlen("budget:usage:outbox") == 1
    assert await redis.hget(f"budget:call:{ticket.id}", "status") == "settled"


async def test_unknown_call_keeps_reservation_and_late_usage_does_not_revive_expired_day(redis):
    from eduagents_api.budget import replay_budget_usage

    guard, _ = make_guard(redis)
    _, ticket = await guard.reserve("tenant-1", "user-1", "standard", "lost-call")
    key = f"budget:call:{ticket.id}"
    await redis.zadd("budget:calls:pending", {key: 0})
    await replay_budget_usage({"redis": redis})
    assert await redis.hget(key, "status") == "usage_unknown"
    assert await redis.zscore("budget:calls:unknown", key) is not None
    assert float(await redis.get(f"budget:user-1:{DAY}")) == EST_STANDARD
    assert not await redis.hlen("budget:usage:outbox")

    await redis.delete(f"budget:user-1:{DAY}", f"budget:global:{DAY}")
    await guard.settle(event(0.008), ticket)
    assert not await redis.exists(f"budget:user-1:{DAY}", f"budget:global:{DAY}")
    assert await redis.hget(key, "status") == "settled"
    assert await redis.zscore("budget:calls:unknown", key) is None
    assert await redis.hexists("budget:usage:outbox", ticket.id)


async def test_purge_budget_data_only_removes_owned_tickets(redis):
    from eduagents_api.budget import purge_budget_calls

    guard, _ = make_guard(redis)
    _, lost = await guard.reserve("tenant-1", "user-1", "cheap", "unknown")
    _, finished = await guard.reserve("tenant-1", "user-1", "cheap", "completed")
    _, other = await guard.reserve("another-tenant", "another-user", "cheap", "keep")
    await guard.settle(event(0.01), finished)
    await redis.delete(f"budget:call:{finished.id}")
    await purge_budget_calls(redis, "tenant-1")
    assert not await redis.exists(f"budget:call:{lost.id}")
    assert not await redis.hexists("budget:usage:outbox", finished.id)
    assert await redis.zcard("budget:calls:pending") == 1
    assert await redis.exists(f"budget:call:{other.id}")
