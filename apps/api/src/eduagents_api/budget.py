"""用户日预算强制 (Wave1-包B): RedisBudgetGuard, 挂在模型网关入口 (唯一 choke point)。

- 天粒度 key budget:{user_id}:{YYYYMMDD}, 日界固定 Asia/Shanghai —— 时间读 timectl.now
  的虚拟时钟 (D-A1), time-travel 演示推进日期时预算随之翻天, 且不会出现 UTC 混用导致的
  "半夜预算复活"。
- INCRBYFLOAT 以厘 (0.001 CNY) 为微元计数; TTL 48h (24h 会让 23:59 创建的 key 在次日
  争用窗口内读到空值)。
- 阈值: <80% None (正常) / 80-100% "cheap" (降档+压 max_tokens, 网关执行) /
  ≥100% 抛 BudgetExceeded (API 层转 429 + Retry-After=距沪时午夜秒数)。
- stub/replay 零成本不计数: 网关只对 mode=live 的结果调用 settle, 这里再守一道。
- 并发下两请求同时读到 <100% 会少量超支 —— 竞赛级可接受, 严格版是 Lua 原子预扣+返还。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from eduagents_core.config import Settings
from eduagents_core.gateway import BudgetExceeded, UsageEvent
from eduagents_core.logging import get_logger

from . import timectl

log = get_logger("budget")

CN_TZ = ZoneInfo("Asia/Shanghai")
MICRO_PER_CNY = 1000.0  # 厘
KEY_TTL_SECONDS = 48 * 3600
SOFT_RATIO = 0.8


def budget_key(user_id: str, cn_now: datetime) -> str:
    return f"budget:{user_id}:{cn_now:%Y%m%d}"


class RedisBudgetGuard:
    """实现 eduagents_core.gateway.BudgetGuard 协议。

    now_fn: 可注入的业务时间源 (返回 tz-aware datetime); 缺省走 timectl.now(redis)
    虚拟时钟。测试用注入时间验证日界翻转。
    """

    def __init__(
        self,
        redis,
        settings: Settings,
        now_fn: Callable[[], Awaitable[datetime]] | None = None,
    ) -> None:
        self._redis = redis
        self._daily_micro = settings.user_daily_budget_cny * MICRO_PER_CNY
        self._now_fn = now_fn

    async def _now_cn(self) -> datetime:
        now = await (self._now_fn() if self._now_fn else timectl.now(self._redis))
        return now.astimezone(CN_TZ)

    async def precheck(self, tenant_id: str, user_id: str) -> str | None:
        if not user_id or self._daily_micro <= 0:
            return None  # 无用户主体的系统调用 / 预算未启用: 不适用个人日预算
        cn_now = await self._now_cn()
        raw = await self._redis.get(budget_key(user_id, cn_now))
        try:
            spent = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            spent = 0.0
        if spent >= self._daily_micro:
            midnight = (cn_now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            log.warning("budget_exceeded", user_id=user_id, spent_micro=spent)
            raise BudgetExceeded(
                retry_after_seconds=max(1, int((midnight - cn_now).total_seconds()))
            )
        if spent >= SOFT_RATIO * self._daily_micro:
            return "cheap"
        return None

    async def settle(self, event: UsageEvent) -> None:
        # 网关已只对 mode=live 调 settle; 双保险防未来调用方把 stub/replay 计进预算
        if event.mode != "live" or event.cost_cny <= 0 or not event.user_id:
            return
        cn_now = await self._now_cn()
        key = budget_key(event.user_id, cn_now)
        pipe = self._redis.pipeline()
        pipe.incrbyfloat(key, event.cost_cny * MICRO_PER_CNY)
        pipe.expire(key, KEY_TTL_SECONDS)
        await pipe.execute()


def create_budget_guard(redis, settings: Settings) -> RedisBudgetGuard:
    """main.py 接线工厂:
    OpenAIGateway(settings, usage_sink=..., budget_guard=create_budget_guard(redis, settings))
    """
    return RedisBudgetGuard(redis, settings)
