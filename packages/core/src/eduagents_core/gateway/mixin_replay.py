"""回放/存根/成本计算 (从 openai_gateway_mixins.py 按域拆出, 纯搬移零行为变更)."""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING

from ..logging import get_logger
from .base import ChatResult, GatewayError, message_text
from .recorder import Cassette
from .stub import estimate_tokens, stub_text

if TYPE_CHECKING:
    from ..config import Settings, TierConfig
    from .base import ChatRequest
    from .routing import ProviderCandidate

log = get_logger("gateway")


class GatewayReplayMixin:
    """回放/存根/成本计算 (纯搬移)."""

    if TYPE_CHECKING:
        # 宿主 OpenAIGateway.__init__ 注入的状态 (隐式契约显式化)。
        _s: Settings
        _cassette: Cassette

    def _cassette_key(self, req: ChatRequest) -> str:
        return Cassette.key(
            {
                "tier": req.tier.value,
                "model": req.model_override or self._s.tier(req.tier.value).model,
                "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                "json_mode": req.json_mode,
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
                "enable_thinking": self._s.tier(req.tier.value).enable_thinking,
            }
        )

    def _cost(self, tier_cfg: TierConfig | ProviderCandidate, p_tok: int, c_tok: int) -> float:
        return round(
            p_tok / 1e6 * tier_cfg.input_price_per_m + c_tok / 1e6 * tier_cfg.output_price_per_m,
            6,
        )

    def _stub_result(self, req: ChatRequest, started: float) -> ChatResult:
        text = stub_text(req)
        p_tok = sum(estimate_tokens(message_text(m.content)) for m in req.messages)
        c_tok = estimate_tokens(text)
        return ChatResult(
            text=text,
            model=f"stub-{req.tier.value}",
            tier=req.tier.value,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cost_cny=0.0,
            latency_ms=int((perf_counter() - started) * 1000),
            mode="stub",
        )

    def _replay_result(self, req: ChatRequest, started: float) -> ChatResult:
        hit = self._cassette.get(self._cassette_key(req))
        if hit is None:
            if self._s.gateway_replay_fallback_stub:
                log.warning("cassette_miss_fallback_stub", tier=req.tier.value)
                return self._stub_result(req, started)
            raise GatewayError("CASSETTE_MISS", "replay 模式下未找到录制的响应")
        tier_cfg = self._s.tier(req.tier.value)
        p_tok = int(hit.get("prompt_tokens", 0))
        c_tok = int(hit.get("completion_tokens", 0))
        return ChatResult(
            text=hit["text"],
            model=str(hit.get("model", tier_cfg.model)),
            tier=req.tier.value,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cost_cny=0.0,
            latency_ms=int((perf_counter() - started) * 1000),
            mode="replay",
            finish_reason=hit.get("finish_reason"),
        )
