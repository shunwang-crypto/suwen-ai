"""熔断器与路由状态的查询/操作面 (从 openai_gateway_mixins.py 按域拆出, 纯搬移零行为变更)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..logging import get_logger
from .base import Tier
from .routing import BusinessRoute, configured_candidates

if TYPE_CHECKING:
    from ..config import Settings
    from .circuit import CircuitBreaker
    from .faults import FaultInjector

log = get_logger("gateway")


class GatewayBreakersMixin:
    """熔断器与路由状态的查询/操作面 (纯搬移)."""

    if TYPE_CHECKING:
        # 宿主 OpenAIGateway.__init__ 注入的状态, 运行时由宿主赋值;
        # 声明在类体仅为把 mixin 的隐式契约显式化 (TYPE_CHECKING 块零运行时开销)。
        _s: Settings
        _breakers: dict[Tier, CircuitBreaker]
        _candidate_breakers: dict[str, CircuitBreaker]
        _candidate_failures: dict[str, str | None]
        _faults: FaultInjector

    def breaker_state(self, tier: Tier) -> str:
        return self._breakers[tier].state.value

    def breaker_states(self) -> dict[str, str]:
        """Return legacy tier aliases plus safe candidate breaker states."""
        states = {t.value: self._breakers[t].state.value for t in Tier}
        states.update(
            {candidate_id: breaker.state.value for candidate_id, breaker in self._candidate_breakers.items()}
        )
        return states

    def trip_breaker(self, tier: Tier) -> None:
        """M5-D2: 一键熔断某一档 (不真的打 provider, 直接把断路器拉开)。"""
        self._breakers[tier].trip()
        log.warning("breaker_tripped_manually", tier=tier.value)

    def trip_candidate(self, candidate_id: str) -> None:
        breaker = self._candidate_breakers.get(candidate_id)
        if breaker is None:
            raise ValueError("unknown provider candidate")
        breaker.trip()
        log.warning("candidate_breaker_tripped_manually", candidate_id=candidate_id)

    def reset_breakers(self) -> None:
        for breaker in self._breakers.values():
            breaker.reset()
        for breaker in self._candidate_breakers.values():
            breaker.reset()
        for candidate_id in self._candidate_failures:
            self._candidate_failures[candidate_id] = None
        log.warning("breakers_reset_manually")

    def routing_status(self) -> list[dict[str, object]]:
        """Safe candidate status for operators; never includes key or request data."""
        rows: list[dict[str, object]] = []
        for route in BusinessRoute:
            for candidate in configured_candidates(self._s, route):
                breaker = self._candidate_breakers[candidate.id]
                rows.append(
                    {
                        "id": candidate.id,
                        "route": route.value,
                        "provider": candidate.provider,
                        "model": candidate.model,
                        "configured": candidate.configured,
                        "disabled": not self._s.model_routing_enabled or not candidate.configured,
                        "routing_enabled": self._s.model_routing_enabled,
                        "breaker_open": breaker.state.value == "open",
                        "state": breaker.state.value,
                        "last_failure_code": self._candidate_failures.get(candidate.id),
                    }
                )
        return rows

    @property
    def faults(self) -> FaultInjector:
        return self._faults
