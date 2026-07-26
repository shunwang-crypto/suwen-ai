"""虚拟时钟控制 (§9): API 层业务时间的唯一入口。

偏移秒数存 Redis (OFFSET_KEY), 跨 api/worker 进程共享; now() = 系统时间 + 偏移,
等价于 OffsetClock(offset_provider=Redis)。演示/验收用 time-travel 推进偏移,
禁止改容器系统时间的方案 (D-A1 的载体)。

P1-13 护栏: ENABLE_TIME_TRAVEL 环境变量显式开关 (未设置时 dev/test 默认开,
prod 默认关)。admin 入口须先判 is_enabled() 再调 set_offset/advance
(admin.py 由主线接线, 见 PRODUCTION_GAP_REVIEW P1-13)。偏移键带 30 天 TTL,
演示残留不再永久污染 FSRS/预算日界。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from eduagents_core.clock import SystemClock

OFFSET_KEY = "clock:offset_seconds"
# 偏移键 TTL: 30 天。单键设计原本无 TTL (演示期常驻), 护栏后有界。
OFFSET_TTL_SECONDS = 30 * 24 * 3600

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

_system = SystemClock()


def is_enabled() -> bool:
    """time-travel 是否允许 (P1-13)。

    ENABLE_TIME_TRAVEL 显式设置时以其为准; 未设置时按 APP_ENV 兜底:
    prod 关, 其余 (dev/test) 开。config 归属他线, 这里直接读环境变量。
    """
    raw = os.environ.get("ENABLE_TIME_TRAVEL", "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return os.environ.get("APP_ENV", "dev").strip().lower() != "prod"


async def get_offset(redis) -> float:
    """当前偏移秒数; 键不存在/损坏时按 0 处理 (真实时间)。"""
    raw = await redis.get(OFFSET_KEY)
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


async def set_offset(redis, seconds: float) -> None:
    await redis.set(OFFSET_KEY, seconds, ex=OFFSET_TTL_SECONDS)


async def advance(redis, days: float) -> float:
    """偏移前进 N 天 (可为负/小数), 返回新偏移秒数。INCRBYFLOAT 原子累加。"""
    new_offset = float(await redis.incrbyfloat(OFFSET_KEY, days * 86400.0))
    await redis.expire(OFFSET_KEY, OFFSET_TTL_SECONDS)
    return new_offset


async def now(redis) -> datetime:
    """业务时间 (UTC, tz-aware)。API 层一切 FSRS/BKT/队列时间读取必须走这里。"""
    return _system.now() + timedelta(seconds=await get_offset(redis))
