"""预算强制 (P0-2b / P1-12): 用户日预算 + 全局日成本熔断, 挂在模型网关入口 (唯一 choke point)。

键设计 (厘 = 0.001 CNY 为微元):
- 用户键 budget:{user_id}:{YYYYMMDD}
- 全局键 budget:global:{YYYYMMDD}  — 全站日成本熔断 (P0-2b)

precheck 顺序 (单个 Lua 脚本内原子完成, P1-12):
1. 全局硬限: ≥150% 全档拒绝 (含 admin, 全站熔断); ≥100% 拒非 admin;
   ≥80% 全站强制 cheap;
2. 用户档位: ≥100% 抛 BudgetExceeded / ≥80% 返回 "cheap" / 否则放行;
3. 通过后按档位定额**原子预扣**估算额 (cheap 0.005 / standard 0.02 /
   strong 0.05 / plan 0.02 元),
   同时打进用户键与全局键 — 并发下不再有 "都读到 spent=0" 的 check-then-act 竞态。
settle 按实际成本与预扣差额结算 (delta 可负 = 返还); 调用失败时网关以零成本事件
整额返还。用户键与全局键在同一 pipeline 内更新 (P0-2b)。

日界时钟 (P1-12 后半, 与 FSRS **刻意不同源**):
- FSRS/复习/计划走 timectl 虚拟时钟 — time-travel 演示按钮要能把复习队列推进 N 天;
- 预算键日期一律 **真实系统时钟** (Asia/Shanghai): 若预算也跟虚拟时钟, 一次演示
  advance 就把全站当日已花额清零 (换 key 即复位), 成本熔断被演示功能旁路。
  now_fn 仅供测试注入固定时间, 生产不注入。

admin 豁免 (100%~150% 区间): 默认经 users.role 查询 (plain_session, 非 RLS 表) +
进程内 5 分钟缓存; 只在全局越限时才触发查询, 正常路径零 DB 开销。
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from zoneinfo import ZoneInfo

from eduagents_core.config import Settings
from eduagents_core.gateway import BudgetExceeded, UsageEvent
from eduagents_core.logging import get_logger

log = get_logger("budget")

CN_TZ = ZoneInfo("Asia/Shanghai")
MICRO_PER_CNY = 1000.0  # 厘
KEY_TTL_SECONDS = 48 * 3600
SOFT_RATIO = 0.8
GLOBAL_HARD_RATIO = 1.5  # 全局 150%: 全档熔断 (含 admin)

# P1-12: 按档位定额预扣 (元)。settle 时以实际成本差额返还/补记。
RESERVE_CNY_PER_TIER: dict[str, float] = {
    "cheap": 0.005,
    "standard": 0.02,
    "strong": 0.05,
    "plan": 0.02,
}

# 单个 Lua 脚本原子完成 检查(全局→用户) + 预扣(用户键+全局键)。
# KEYS: 1=用户键 2=全局键
# ARGV: 1=预扣厘 2=用户日限厘(<=0 不启用) 3=全局日限厘(<=0 不启用)
#       4=TTL秒 5=is_admin(0/1) 6=track_user(0/1, user_id 是否存在)
# 返回: global_killed | global_exceeded | user_exceeded (三者均未预扣)
#       | cheap | ok (二者已预扣)
_PRECHECK_LUA = """
local user_key = KEYS[1]
local global_key = KEYS[2]
local est = tonumber(ARGV[1])
local user_limit = tonumber(ARGV[2])
local global_limit = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local is_admin = tonumber(ARGV[5])
local track_user = tonumber(ARGV[6])

local verdict = 'ok'
if global_limit > 0 then
  local g = tonumber(redis.call('GET', global_key) or '0')
  if g >= global_limit * 1.5 then
    return 'global_killed'
  end
  if g >= global_limit then
    if is_admin == 0 then
      return 'global_exceeded'
    end
    verdict = 'cheap'
  elseif g >= global_limit * 0.8 then
    verdict = 'cheap'
  end
end
if track_user == 1 and user_limit > 0 then
  local u = tonumber(redis.call('GET', user_key) or '0')
  if u >= user_limit then
    return 'user_exceeded'
  end
  if u >= user_limit * 0.8 then
    verdict = 'cheap'
  end
end
if est > 0 then
  if track_user == 1 then
    redis.call('INCRBYFLOAT', user_key, est)
    redis.call('EXPIRE', user_key, ttl)
  end
  redis.call('INCRBYFLOAT', global_key, est)
  redis.call('EXPIRE', global_key, ttl)
end
return verdict
"""

# admin 角色进程内缓存: user_id -> (记录时刻 monotonic, is_admin)
_ADMIN_CACHE: dict[str, tuple[float, bool]] = {}
_ADMIN_CACHE_TTL = 300.0


async def _lookup_is_admin(user_id: str) -> bool:
    """users.role == 'admin' 查询 (users 非 RLS 表, plain_session)。

    查询失败按非 admin 处理 (拒绝方向, 安全侧)。仅在全局预算越限时被调用。
    """
    cached = _ADMIN_CACHE.get(user_id)
    if cached is not None and monotonic() - cached[0] < _ADMIN_CACHE_TTL:
        return cached[1]
    is_admin = False
    try:
        from sqlalchemy import select

        from .db import plain_session
        from .models import User

        async with plain_session() as session:
            role = await session.scalar(
                select(User.role).where(User.id == _uuid.UUID(user_id))
            )
        is_admin = role == "admin"
    except Exception:  # noqa: BLE001 — 查不到/DB 抖动一律按非 admin (拒绝方向安全)
        log.exception("budget_admin_lookup_failed")
    _ADMIN_CACHE[user_id] = (monotonic(), is_admin)
    return is_admin


def budget_key(user_id: str, cn_now: datetime) -> str:
    return f"budget:{user_id}:{cn_now:%Y%m%d}"


def global_budget_key(cn_now: datetime) -> str:
    return f"budget:global:{cn_now:%Y%m%d}"


class RedisBudgetGuard:
    """实现 eduagents_core.gateway.BudgetGuard 协议 (见模块 docstring)。

    now_fn: 仅测试注入固定时间; 缺省 = 真实系统时钟 (刻意不接 timectl 虚拟时钟,
    否则 time-travel 演示一键清零全站预算, P1-12)。
    is_admin_fn: 仅测试注入; 缺省查 users.role。
    """

    def __init__(
        self,
        redis,
        settings: Settings,
        now_fn: Callable[[], Awaitable[datetime]] | None = None,
        is_admin_fn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._redis = redis
        self._daily_micro = settings.user_daily_budget_cny * MICRO_PER_CNY
        self._global_daily_micro = settings.global_daily_budget_cny * MICRO_PER_CNY
        self._now_fn = now_fn
        self._is_admin_fn = is_admin_fn or _lookup_is_admin
        self._precheck_script = redis.register_script(_PRECHECK_LUA)

    async def _now_cn(self) -> datetime:
        if self._now_fn is not None:
            return (await self._now_fn()).astimezone(CN_TZ)
        # 真实系统时钟: 预算日界不随 timectl 偏移 (P1-12)
        return datetime.now(UTC).astimezone(CN_TZ)

    def _retry_after(self, cn_now: datetime) -> int:
        midnight = (cn_now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(1, int((midnight - cn_now).total_seconds()))

    async def _run_precheck_script(
        self, user_id: str, cn_now: datetime, tier: str, is_admin: bool
    ) -> str:
        raw = await self._precheck_script(
            keys=[
                budget_key(user_id or "-", cn_now),
                global_budget_key(cn_now),
            ],
            args=[
                RESERVE_CNY_PER_TIER.get(tier, RESERVE_CNY_PER_TIER["standard"])
                * MICRO_PER_CNY,
                self._daily_micro,
                self._global_daily_micro,
                KEY_TTL_SECONDS,
                1 if is_admin else 0,
                1 if user_id else 0,
            ],
        )
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    async def precheck(
        self, tenant_id: str, user_id: str, tier: str = "standard"
    ) -> str | None:
        cn_now = await self._now_cn()
        verdict = await self._run_precheck_script(user_id, cn_now, tier, is_admin=False)
        if verdict == "global_exceeded" and user_id:
            # 100%~150% 区间 admin 豁免: 只在越限时才查角色 (正常路径零 DB 开销);
            # admin 确认后带 is_admin=1 重跑脚本完成预扣 (仍强制 cheap)
            if await self._is_admin_fn(user_id):
                verdict = await self._run_precheck_script(
                    user_id, cn_now, tier, is_admin=True
                )
        if verdict == "global_killed":
            log.error("budget_global_killed", user_id=user_id)
            raise BudgetExceeded(
                "全站 AI 成本已触发熔断，请联系管理员",
                retry_after_seconds=self._retry_after(cn_now),
            )
        if verdict == "global_exceeded":
            log.warning("budget_global_exceeded", user_id=user_id)
            raise BudgetExceeded(
                "全站 AI 用量已达今日上限，明天再来",
                retry_after_seconds=self._retry_after(cn_now),
            )
        if verdict == "user_exceeded":
            log.warning("budget_exceeded", user_id=user_id)
            raise BudgetExceeded(
                retry_after_seconds=self._retry_after(cn_now)
            )
        return "cheap" if verdict == "cheap" else None

    async def settle(
        self, event: UsageEvent, reserved_tier: str | None = None
    ) -> None:
        """差额结算: delta = 实际成本 - 预扣额 (可负 = 返还)。

        - live 事件按实际成本结算; 非 live (stub/replay/cache/error) 实际成本按 0,
          若有预扣则整额返还;
        - 无预扣 (reserved_tier=None, 兼容旧调用方) 退化为纯累加, 语义同旧版。
        跨午夜的极端情况: 预扣与结算可能落在不同日期键, 漂移 ≤ 一次预扣额, 可接受。
        """
        reserved_micro = (
            RESERVE_CNY_PER_TIER.get(reserved_tier, 0.0) * MICRO_PER_CNY
            if reserved_tier
            else 0.0
        )
        actual_micro = (
            event.cost_cny * MICRO_PER_CNY
            if event.mode == "live" and event.cost_cny > 0
            else 0.0
        )
        delta = actual_micro - reserved_micro
        if delta == 0:
            return
        cn_now = await self._now_cn()
        pipe = self._redis.pipeline()
        if event.user_id:
            key = budget_key(event.user_id, cn_now)
            pipe.incrbyfloat(key, delta)
            pipe.expire(key, KEY_TTL_SECONDS)
        gkey = global_budget_key(cn_now)
        pipe.incrbyfloat(gkey, delta)
        pipe.expire(gkey, KEY_TTL_SECONDS)
        await pipe.execute()


def create_budget_guard(redis, settings: Settings) -> RedisBudgetGuard:
    """main.py / worker 接线工厂:
    OpenAIGateway(settings, usage_sink=..., budget_guard=create_budget_guard(redis, settings))
    """
    return RedisBudgetGuard(redis, settings)
