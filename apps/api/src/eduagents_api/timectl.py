"""虚拟时钟控制 (§9): API 层业务时间的唯一入口。

偏移秒数存 Redis (OFFSET_KEY), 跨 api/worker 进程共享; now() = 系统时间 + 偏移,
等价于 OffsetClock(offset_provider=Redis)。演示/验收用 time-travel 推进偏移,
禁止改容器系统时间的方案 (D-A1 的载体)。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from eduagents_core.clock import SystemClock

OFFSET_KEY = "clock:offset_seconds"

_system = SystemClock()


async def get_offset(redis) -> float:
    """当前偏移秒数; 键不存在/损坏时按 0 处理 (真实时间)。"""
    raw = await redis.get(OFFSET_KEY)
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


async def set_offset(redis, seconds: float) -> None:
    await redis.set(OFFSET_KEY, seconds)


async def advance(redis, days: float) -> float:
    """偏移前进 N 天 (可为负/小数), 返回新偏移秒数。INCRBYFLOAT 原子累加。"""
    return float(await redis.incrbyfloat(OFFSET_KEY, days * 86400.0))


async def now(redis) -> datetime:
    """业务时间 (UTC, tz-aware)。API 层一切 FSRS/BKT/队列时间读取必须走这里。"""
    return _system.now() + timedelta(seconds=await get_offset(redis))
