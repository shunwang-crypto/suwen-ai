"""OpenAIGateway 的 mixin 兼容门面 (实现已按域拆至三文件, 本模块仅转发, 零行为变更).

- mixin_breakers.GatewayBreakersMixin: 熔断器与路由状态的查询/操作面
- mixin_replay.GatewayReplayMixin: 回放/存根/成本计算
- mixin_live_calls.GatewayLiveCallsMixin: 候选路由与档位直连的真实调用/流式/结算
  (共享常量 _RETRYABLE/_DEGRADE/_TIER_RANK/_ROUTING_RETRYABLE_CODES 与
  _warn_if_upcost 随 LiveCalls 实现迁至该模块)

保留既有导入路径: tests/unit/test_cost_safety.py 从本模块导入常量并
monkeypatch 本模块的 log 对象 —— 因此 log 必须与实现模块 re-export 同一实例。
"""

from __future__ import annotations

from .mixin_breakers import GatewayBreakersMixin
from .mixin_live_calls import (
    _DEGRADE,
    _RETRYABLE,
    _ROUTING_RETRYABLE_CODES,
    _TIER_RANK,
    GatewayLiveCallsMixin,
    _warn_if_upcost,
    log,
)
from .mixin_replay import GatewayReplayMixin

__all__ = [
    "GatewayBreakersMixin",
    "GatewayLiveCallsMixin",
    "GatewayReplayMixin",
    "_DEGRADE",
    "_RETRYABLE",
    "_ROUTING_RETRYABLE_CODES",
    "_TIER_RANK",
    "_warn_if_upcost",
    "log",
]
