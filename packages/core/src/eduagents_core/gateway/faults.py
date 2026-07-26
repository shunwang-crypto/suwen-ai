"""故障注入开关 (M5-D2): 现场演示"关掉主模型 → 走降级链"的操作台。

设计约束 (三条, 缺一不可):

1. **只作用于 live/record 路径**。stub/replay 是演示的兜底通道 —— 主模型全挂之后
   `_live_chat` 正是靠 cassette/stub 兜住不 5xx。若故障注入连兜底一起打穿, 演示就
   从"优雅降级"变成"整体崩溃", 与 M5-D2 的验收点正好相反。因此注入点放在
   `_call_tier` / `_live_stream` 真正发出网络请求之前, 不进 `_stub_result` /
   `_replay_result`。
2. **抛可重试异常**。provider_down 抛 openai 的 `APIConnectionError` (在 `_RETRYABLE`
   里), 于是链路完整走一遍: tenacity 重试 → 全败 → breaker.record(False) →
   错误率越阈值 → 熔断器 open → 降级到下一档 tier → 全档失败 → cassette → stub。
   自定义异常类型会被 tenacity 直接 reraise, 看不到重试与熔断, 演示价值减半。
3. **进程内单例, 不落盘不进 Redis**。演示开关必须能一键归零且不污染下一次启动;
   API 与网关同进程, 单例足够。多副本部署时各副本独立 —— 演示是单副本, 如实标注。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import asdict, dataclass

import httpx
from openai import APIConnectionError

from ..logging import get_logger

log = get_logger("faults")

MAX_LATENCY_MS = 30_000


@dataclass(frozen=True)
class FaultState:
    """故障注入的三个旋钮。"""

    provider_down: bool = False
    latency_ms: int = 0
    error_rate: float = 0.0

    @property
    def active(self) -> bool:
        return self.provider_down or self.latency_ms > 0 or self.error_rate > 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["active"] = self.active
        return d


class FaultInjector:
    """进程内故障开关。gateway 在 live/record 发请求前调用 `apply()`。"""

    def __init__(self, state: FaultState | None = None, rng: random.Random | None = None):
        self._state = state or FaultState()
        self._rng = rng or random.Random()

    @property
    def state(self) -> FaultState:
        return self._state

    def snapshot(self) -> dict:
        return self._state.as_dict()

    def update(
        self,
        *,
        provider_down: bool | None = None,
        latency_ms: int | None = None,
        error_rate: float | None = None,
    ) -> FaultState:
        """部分更新 (None = 保持原值)。数值越界一律夹取, 演示开关不该 422。"""
        s = self._state
        self._state = FaultState(
            provider_down=s.provider_down if provider_down is None else bool(provider_down),
            latency_ms=(
                s.latency_ms
                if latency_ms is None
                else max(0, min(int(latency_ms), MAX_LATENCY_MS))
            ),
            error_rate=(
                s.error_rate if error_rate is None else max(0.0, min(float(error_rate), 1.0))
            ),
        )
        if self._state != s:
            log.warning("fault_injection_changed", **self._state.as_dict())
        return self._state

    def reset(self) -> FaultState:
        return self.update(provider_down=False, latency_ms=0, error_rate=0.0)

    async def apply(self, tier: str, base_url: str = "https://api.openai.com/v1") -> None:
        """在真实调用前执行: 先注入延迟, 再决定是否抛可重试异常。

        latency 放在 error 之前 —— 演示"慢 + 挂"组合时, 观感应该是"卡住然后失败",
        这也更贴近真实 provider 劣化的样子 (先超时再报错)。
        """
        s = self._state
        if s.latency_ms > 0:
            await asyncio.sleep(s.latency_ms / 1000.0)
        if s.provider_down:
            raise self._boom(tier, base_url, "provider_down")
        if s.error_rate > 0.0 and self._rng.random() < s.error_rate:
            raise self._boom(tier, base_url, "error_rate")

    def _boom(self, tier: str, base_url: str, reason: str) -> APIConnectionError:
        log.warning("fault_injected", tier=tier, reason=reason)
        request = httpx.Request("POST", f"{base_url.rstrip('/')}/chat/completions")
        return APIConnectionError(
            message=f"[fault-injection:{reason}] tier={tier} 模拟 provider 不可用",
            request=request,
        )


_injector = FaultInjector()


def get_fault_injector() -> FaultInjector:
    """进程内单例 (API 端点与网关共享同一个开关)。"""
    return _injector
