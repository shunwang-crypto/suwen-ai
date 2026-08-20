"""Explicit business routes and provider candidates.

The legacy gateway used ``Tier`` as both a product budget tier and a provider
selector.  Those are different concepts: a route may contain free and paid
providers, and a visual model must never inherit the model name of another
provider.  This module keeps the candidate description immutable and safe to
use as a cache/circuit-breaker identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Settings


class BusinessRoute(StrEnum):
    text = "text"
    vision = "vision"
    plan = "plan"


@dataclass(frozen=True, slots=True, repr=False)
class ProviderCandidate:
    """One concrete, independently reachable provider/model candidate.

    ``api_key`` deliberately does not appear in repr/logging.  Candidate IDs
    are stable non-secret identities and are suitable for breaker/client/cache
    keys.  An empty key/model means the candidate is disabled, not callable.
    """

    id: str
    route: BusinessRoute
    provider: str
    base_url: str
    api_key: str = field(default="", repr=False)
    model: str = ""
    input_price_per_m: float = 0.0
    output_price_per_m: float = 0.0
    supports_vision: bool = False
    supports_json: bool = True
    supports_stream: bool = True
    supports_thinking: bool = False
    enable_thinking: bool | None = None
    reserve_tier: str = "standard"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    @property
    def free(self) -> bool:
        return self.input_price_per_m <= 0 and self.output_price_per_m <= 0

    def __repr__(self) -> str:
        return (
            "ProviderCandidate("
            f"id={self.id!r}, route={self.route.value!r}, provider={self.provider!r}, "
            f"model={self.model!r}, configured={self.configured}, "
            f"supports_vision={self.supports_vision}, supports_json={self.supports_json}, "
            f"supports_stream={self.supports_stream})"
        )


def _candidate(
    *,
    settings: Settings,
    id: str,
    route: BusinessRoute,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    input_price_per_m: float,
    output_price_per_m: float,
    supports_vision: bool,
    supports_json: bool,
    reserve_tier: str,
    supports_thinking: bool = False,
    enable_thinking: bool | None = None,
) -> ProviderCandidate:
    del settings  # kept in the helper signature to make config provenance explicit
    return ProviderCandidate(
        id=id,
        route=route,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        input_price_per_m=input_price_per_m,
        output_price_per_m=output_price_per_m,
        supports_vision=supports_vision,
        supports_json=supports_json,
        supports_stream=True,
        supports_thinking=supports_thinking,
        enable_thinking=enable_thinking,
        reserve_tier=reserve_tier,
    )


def configured_candidates(settings: Settings, route: BusinessRoute) -> tuple[ProviderCandidate, ...]:
    """Return the single local TrendDAO candidate for every business route."""
    model = settings.trenddao_vision_model if route == BusinessRoute.vision else settings.trenddao_text_model
    return (
        _candidate(
            settings=settings,
            id=f"trenddao-{route.value}",
            route=route,
            provider="trenddao",
            base_url=settings.trenddao_base_url,
            api_key=settings.trenddao_api_key,
            model=model,
            input_price_per_m=settings.trenddao_input_price_per_m,
            output_price_per_m=settings.trenddao_output_price_per_m,
            supports_vision=route == BusinessRoute.vision,
            supports_json=route != BusinessRoute.vision,
            reserve_tier="plan" if route == BusinessRoute.plan else "standard",
        ),
    )


def route_candidates(settings: Settings, route: BusinessRoute) -> tuple[ProviderCandidate, ...]:
    return tuple(candidate for candidate in configured_candidates(settings, route) if candidate.configured)
