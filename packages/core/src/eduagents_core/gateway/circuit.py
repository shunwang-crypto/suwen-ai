from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from enum import StrEnum


class BreakerState(StrEnum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


class CircuitBreaker:
    """Sliding-window error-rate breaker.

    closed  -> open      when failure rate > threshold over the window
    open    -> half_open one probe allowed after open_seconds
    half_open -> closed  probe succeeds / -> open probe fails
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        min_samples: int = 6,
        failure_rate: float = 0.5,
        open_seconds: float = 30.0,
        time_func: Callable[[], float] = time.monotonic,
    ):
        self._window = window_seconds
        self._min_samples = min_samples
        self._failure_rate = failure_rate
        self._open_seconds = open_seconds
        self._now = time_func
        self._events: deque[tuple[float, bool]] = deque()
        self._state = BreakerState.closed
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        self._maybe_transition()
        return self._state

    def _prune(self) -> None:
        cutoff = self._now() - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _maybe_transition(self) -> None:
        if self._state == BreakerState.open and (
            self._now() - self._opened_at >= self._open_seconds
        ):
            self._state = BreakerState.half_open
            self._probe_in_flight = False

    def allow(self) -> bool:
        self._maybe_transition()
        if self._state == BreakerState.closed:
            return True
        if self._state == BreakerState.half_open and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def trip(self) -> None:
        """强制拉开断路器 (M5-D2 演示: "一键熔断主模型")。

        清空窗口样本, 否则 open_seconds 后半开探测成功回到 closed 时, 旧的失败样本
        还留在窗口里会立刻把它再次拉开, 演示看起来像"恢复不了"。
        """
        self._events.clear()
        self._probe_in_flight = False
        self._state = BreakerState.open
        self._opened_at = self._now()

    def reset(self) -> None:
        """强制闭合 (演示收尾/一键恢复)。"""
        self._events.clear()
        self._probe_in_flight = False
        self._state = BreakerState.closed
        self._opened_at = 0.0

    def record(self, ok: bool) -> None:
        self._maybe_transition()
        if self._state == BreakerState.half_open:
            self._probe_in_flight = False
            if ok:
                self._state = BreakerState.closed
                self._events.clear()
            else:
                self._state = BreakerState.open
                self._opened_at = self._now()
            return
        self._events.append((self._now(), ok))
        self._prune()
        failures = sum(1 for _, e_ok in self._events if not e_ok)
        total = len(self._events)
        if total >= self._min_samples and failures / total > self._failure_rate:
            self._state = BreakerState.open
            self._opened_at = self._now()
