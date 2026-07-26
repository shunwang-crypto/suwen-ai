from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from time import perf_counter

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Settings, TierConfig
from ..logging import get_logger
from .base import (
    ChatRequest,
    ChatResult,
    GatewayError,
    StreamDelta,
    Tier,
    UsageEvent,
    UsageSink,
)
from .circuit import CircuitBreaker
from .recorder import Cassette
from .stub import estimate_tokens, stub_text

log = get_logger("gateway")

_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)

# Degradation chain per requested tier: real different endpoints, tried in order.
_DEGRADE: dict[Tier, list[Tier]] = {
    Tier.cheap: [Tier.cheap, Tier.standard],
    Tier.standard: [Tier.standard, Tier.cheap],
    Tier.strong: [Tier.strong, Tier.standard, Tier.cheap],
}


class OpenAIGateway:
    """The single doorway to every LLM. Modes:

    live   — real API calls (retry + circuit breaker + tier degradation)
    record — live, then persist the response into the cassette store
    replay — serve from cassette; optional deterministic stub on miss
    stub   — deterministic fake responses (dev/CI; banner-marked, never demoable)
    """

    def __init__(self, settings: Settings, usage_sink: UsageSink | None = None):
        self._s = settings
        self._sink = usage_sink
        self._cassette = Cassette(settings.gateway_cassette_dir)
        self._clients: dict[Tier, AsyncOpenAI] = {}
        self._breakers: dict[Tier, CircuitBreaker] = {
            t: CircuitBreaker(
                window_seconds=settings.breaker_window_seconds,
                min_samples=settings.breaker_min_samples,
                failure_rate=settings.breaker_failure_rate,
                open_seconds=settings.breaker_open_seconds,
            )
            for t in Tier
        }

    # ---------- public API ----------

    async def chat(self, req: ChatRequest) -> ChatResult:
        started = perf_counter()
        mode = self._s.gateway_mode
        if mode == "stub":
            result = self._stub_result(req, started)
        elif mode == "replay":
            result = self._replay_result(req, started)
        else:  # live / record
            result = await self._live_chat(req, started)
            if mode == "record":
                self._cassette.put(
                    self._cassette_key(req),
                    {
                        "text": result.text,
                        "model": result.model,
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                    },
                )
        await self._emit_usage(req, result)
        return result

    async def stream_chat(
        self, req: ChatRequest
    ) -> AsyncIterator[StreamDelta | ChatResult]:
        mode = self._s.gateway_mode
        if mode in ("stub", "replay"):
            started = perf_counter()
            result = (
                self._stub_result(req, started)
                if mode == "stub"
                else self._replay_result(req, started)
            )
            chunk = max(1, self._s.gateway_stream_chunk_chars)
            delay = self._s.gateway_stream_delay_ms / 1000
            for i in range(0, len(result.text), chunk):
                yield StreamDelta(result.text[i : i + chunk])
                if delay:
                    await asyncio.sleep(delay)
            result.latency_ms = int((perf_counter() - started) * 1000)
            await self._emit_usage(req, result)
            yield result
            return

        async for item in self._live_stream(req):
            yield item

    def breaker_state(self, tier: Tier) -> str:
        return self._breakers[tier].state.value

    # ---------- internals ----------

    def _cassette_key(self, req: ChatRequest) -> str:
        return Cassette.key(
            {
                "tier": req.tier.value,
                "model": self._s.tier(req.tier.value).model,
                "messages": [
                    {"role": m.role, "content": m.content} for m in req.messages
                ],
                "json_mode": req.json_mode,
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
            }
        )

    def _cost(self, tier_cfg: TierConfig, p_tok: int, c_tok: int) -> float:
        return round(
            p_tok / 1e6 * tier_cfg.input_price_per_m
            + c_tok / 1e6 * tier_cfg.output_price_per_m,
            6,
        )

    def _stub_result(self, req: ChatRequest, started: float) -> ChatResult:
        text = stub_text(req)
        p_tok = sum(estimate_tokens(m.content) for m in req.messages)
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
        )

    def _client(self, tier: Tier) -> AsyncOpenAI:
        if tier not in self._clients:
            cfg = self._s.tier(tier.value)
            self._clients[tier] = AsyncOpenAI(
                api_key=cfg.api_key or "missing-key",
                base_url=cfg.base_url,
                timeout=self._s.gateway_timeout_seconds,
                max_retries=0,  # retries are ours (tenacity), not the SDK's
            )
        return self._clients[tier]

    def _completion_kwargs(self, req: ChatRequest, cfg: TierConfig) -> dict:
        kwargs: dict = {
            "model": cfg.model,
            "messages": [
                {"role": m.role, "content": m.content} for m in req.messages
            ],
            "temperature": req.temperature,
        }
        if req.max_tokens:
            kwargs["max_tokens"] = req.max_tokens
        if req.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    async def _call_tier(self, req: ChatRequest, tier: Tier) -> ChatResult:
        cfg = self._s.tier(tier.value)
        if not cfg.api_key:
            raise GatewayError("NO_API_KEY", f"tier {tier.value} 未配置 API key")
        started = perf_counter()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._s.gateway_max_retries),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        ):
            with attempt:
                resp = await self._client(tier).chat.completions.create(
                    **self._completion_kwargs(req, cfg)
                )
        usage = resp.usage
        p_tok = usage.prompt_tokens if usage else 0
        c_tok = usage.completion_tokens if usage else 0
        return ChatResult(
            text=resp.choices[0].message.content or "",
            model=resp.model or cfg.model,
            tier=tier.value,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cost_cny=self._cost(cfg, p_tok, c_tok),
            latency_ms=int((perf_counter() - started) * 1000),
            mode="live",
            degraded=tier != req.tier,
        )

    async def _live_chat(self, req: ChatRequest, started: float) -> ChatResult:
        last_error: Exception | None = None
        for tier in _DEGRADE[req.tier]:
            breaker = self._breakers[tier]
            if not breaker.allow():
                log.warning("breaker_open_skip", tier=tier.value)
                continue
            try:
                result = await self._call_tier(req, tier)
                breaker.record(True)
                if result.degraded:
                    log.warning(
                        "tier_degraded", requested=req.tier.value, used=tier.value
                    )
                return result
            except Exception as exc:  # noqa: BLE001 — 每个 provider 失败都要继续降级链
                breaker.record(False)
                last_error = exc
                log.error("tier_call_failed", tier=tier.value, error=str(exc))
        # all live providers failed -> cassette, then (optionally) stub
        hit = self._cassette.get(self._cassette_key(req))
        if hit is not None:
            log.warning("live_failed_served_from_cassette")
            result = self._replay_result(req, started)
            result.degraded = True
            return result
        if self._s.gateway_replay_fallback_stub and self._s.app_env != "prod":
            log.error("live_failed_fallback_stub", error=str(last_error))
            result = self._stub_result(req, started)
            result.degraded = True
            return result
        raise GatewayError(
            "ALL_PROVIDERS_FAILED", f"全部模型通道失败: {last_error}"
        )

    async def _live_stream(
        self, req: ChatRequest
    ) -> AsyncIterator[StreamDelta | ChatResult]:
        started = perf_counter()
        for tier in _DEGRADE[req.tier]:
            breaker = self._breakers[tier]
            if not breaker.allow():
                continue
            cfg = self._s.tier(tier.value)
            if not cfg.api_key:
                breaker.record(False)
                continue
            try:
                kwargs = self._completion_kwargs(req, cfg)
                stream = await self._client(tier).chat.completions.create(
                    stream=True, stream_options={"include_usage": True}, **kwargs
                )
                parts: list[str] = []
                p_tok = c_tok = 0
                async for chunk in stream:
                    if chunk.usage:
                        p_tok = chunk.usage.prompt_tokens
                        c_tok = chunk.usage.completion_tokens
                    if chunk.choices and chunk.choices[0].delta.content:
                        delta = chunk.choices[0].delta.content
                        parts.append(delta)
                        yield StreamDelta(delta)
                breaker.record(True)
                text = "".join(parts)
                if not p_tok:
                    p_tok = sum(estimate_tokens(m.content) for m in req.messages)
                    c_tok = estimate_tokens(text)
                result = ChatResult(
                    text=text,
                    model=cfg.model,
                    tier=tier.value,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    cost_cny=self._cost(cfg, p_tok, c_tok),
                    latency_ms=int((perf_counter() - started) * 1000),
                    mode="live",
                    degraded=tier != req.tier,
                )
                if self._s.gateway_mode == "record":
                    self._cassette.put(
                        self._cassette_key(req),
                        {
                            "text": text,
                            "model": result.model,
                            "prompt_tokens": p_tok,
                            "completion_tokens": c_tok,
                        },
                    )
                await self._emit_usage(req, result)
                yield result
                return
            except Exception as exc:  # noqa: BLE001
                breaker.record(False)
                log.error("stream_tier_failed", tier=tier.value, error=str(exc))
        # degrade to non-stream fallback path (cassette/stub), chunked out
        fallback = await self._live_chat(req, started)
        chunk_n = max(1, self._s.gateway_stream_chunk_chars)
        for i in range(0, len(fallback.text), chunk_n):
            yield StreamDelta(fallback.text[i : i + chunk_n])
        await self._emit_usage(req, fallback)
        yield fallback

    async def _emit_usage(self, req: ChatRequest, result: ChatResult) -> None:
        if self._sink is None:
            return
        await self._sink(
            UsageEvent(
                request_id=req.request_id,
                tenant_id=req.tenant_id,
                user_id=req.user_id,
                feature=req.feature,
                model=result.model,
                tier=result.tier,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cached_tokens=0,
                cost_cny=result.cost_cny,
                latency_ms=result.latency_ms,
                mode=result.mode,
            )
        )
