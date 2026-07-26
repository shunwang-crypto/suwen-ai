"""虚拟时钟前进 N 天 (§9): 直连 Redis 累加 clock:offset_seconds。

用法: make time-travel DAYS=3 (等价 python scripts/time_travel.py 3; 支持负数/小数)。
FSRS 到期、BKT 衰减、今日复习队列全部随偏移前进 —— 禁止改容器系统时间。
"""

from __future__ import annotations

import argparse

from eduagents_api.timectl import OFFSET_KEY
from eduagents_core.config import get_settings
from redis import Redis


def main() -> None:
    parser = argparse.ArgumentParser(description="虚拟时钟前进 N 天 (可为负/小数)")
    parser.add_argument("days", type=float, help="前进天数, 如 3 或 0.5 或 -1")
    args = parser.parse_args()
    client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    new_offset = float(client.incrbyfloat(OFFSET_KEY, args.days * 86400.0))
    print(
        f"virtual clock offset = {new_offset:.0f}s "
        f"({new_offset / 86400.0:+.2f} days from real time)"
    )


if __name__ == "__main__":
    main()
