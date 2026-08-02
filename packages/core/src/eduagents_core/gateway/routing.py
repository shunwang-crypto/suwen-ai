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
    """Return the configured chain in the documented business order.

    Empty-key candidates are retained for status/health reporting but filtered
    by the gateway before a network call.
    """

    opencode_base = settings.opencode_zen_base_url
    opencode_key = settings.opencode_zen_api_key
    # Existing CHEAP/STANDARD/PLAN settings remain usable when the new route
    # fields are left empty.  Explicit route values always win.
    text_fallback_key = settings.text_fallback_api_key or settings.cheap_api_key
    text_fallback_model = settings.text_fallback_model or settings.cheap_model
    vision_fallback_key = settings.vision_fallback_api_key or settings.standard_api_key
    vision_fallback_model = settings.vision_fallback_model or settings.vision_model
    plan_primary_key = settings.plan_primary_api_key or settings.plan_api_key
    plan_primary_model = settings.plan_primary_model or settings.plan_model
    plan_secondary_key = settings.plan_secondary_api_key or opencode_key
    plan_tertiary_key = settings.plan_tertiary_api_key or settings.cheap_api_key
    plan_tertiary_model = settings.plan_tertiary_model or settings.cheap_model
    if route == BusinessRoute.text:
        return (
            _candidate(
                settings=settings,
                id="opencode-text",
                route=route,
                provider="opencode",
                base_url=opencode_base,
                api_key=opencode_key,
                model=settings.opencode_text_model,
                input_price_per_m=settings.opencode_input_price_per_m,
                output_price_per_m=settings.opencode_output_price_per_m,
                supports_vision=False,
                supports_json=True,
                reserve_tier="standard",
            ),
            _candidate(
                settings=settings,
                id="siliconflow-text",
                route=route,
                provider="siliconflow",
                base_url=settings.text_fallback_base_url,
                api_key=text_fallback_key,
                model=text_fallback_model,
                input_price_per_m=settings.text_fallback_input_price_per_m,
                output_price_per_m=settings.text_fallback_output_price_per_m,
                supports_vision=False,
                supports_json=True,
                reserve_tier="standard",
            ),
        )
    if route == BusinessRoute.vision:
        return (
            _candidate(
                settings=settings,
                id="opencode-vision",
                route=route,
                provider="opencode",
                base_url=opencode_base,
                api_key=opencode_key,
                model=settings.opencode_vision_model,
                input_price_per_m=settings.opencode_input_price_per_m,
                output_price_per_m=settings.opencode_output_price_per_m,
                supports_vision=True,
                supports_json=False,
                reserve_tier="standard",
            ),
            _candidate(
                settings=settings,
                id="siliconflow-vision",
                route=route,
                provider="siliconflow",
                base_url=settings.vision_fallback_base_url,
                api_key=vision_fallback_key,
                model=vision_fallback_model,
                input_price_per_m=settings.vision_fallback_input_price_per_m,
                output_price_per_m=settings.vision_fallback_output_price_per_m,
                supports_vision=True,
                supports_json=False,
                reserve_tier="standard",
            ),
        )
    return (
        _candidate(
            settings=settings,
            id="deepseek-plan",
            route=route,
            provider="deepseek",
            base_url=settings.plan_primary_base_url,
            api_key=plan_primary_key,
            model=plan_primary_model,
            input_price_per_m=settings.plan_primary_input_price_per_m,
            output_price_per_m=settings.plan_primary_output_price_per_m,
            supports_vision=False,
            supports_json=True,
            reserve_tier="plan",
        ),
        _candidate(
            settings=settings,
            id="opencode-plan",
            route=route,
            provider="opencode",
            base_url=settings.plan_secondary_base_url,
            api_key=plan_secondary_key,
            model=settings.plan_secondary_model,
            input_price_per_m=settings.plan_secondary_input_price_per_m,
            output_price_per_m=settings.plan_secondary_output_price_per_m,
            supports_vision=False,
            supports_json=True,
            reserve_tier="plan",
        ),
        _candidate(
            settings=settings,
            id="siliconflow-plan",
            route=route,
            provider="siliconflow",
            base_url=settings.plan_tertiary_base_url,
            api_key=plan_tertiary_key,
            model=plan_tertiary_model,
            input_price_per_m=settings.plan_tertiary_input_price_per_m,
            output_price_per_m=settings.plan_tertiary_output_price_per_m,
            supports_vision=False,
            supports_json=True,
            reserve_tier="plan",
        ),
    )


def route_candidates(settings: Settings, route: BusinessRoute) -> tuple[ProviderCandidate, ...]:
    return tuple(candidate for candidate in configured_candidates(settings, route) if candidate.configured)
