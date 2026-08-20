from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from time import perf_counter

from openai import (
    AsyncOpenAI,
)

from ..config import Settings, TierConfig
from ..logging import get_logger
from .base import (
    BudgetExceeded,
    BudgetGuard,
    ChatRequest,
    ChatResult,
    CredentialResolver,
    CredentialSource,
    GatewayError,
    ResolvedCredential,
    StreamDelta,
    Tier,
    UsageEvent,
    UsageSink,
)
from .circuit import CircuitBreaker
from .faults import FaultInjector, get_fault_injector
from .mixin_breakers import GatewayBreakersMixin
from .mixin_live_calls import (  # noqa: F401 — 常量转发供 test_cost_safety 既有导入路径
    _DEGRADE,
    _TIER_RANK,
    GatewayLiveCallsMixin,
    _warn_if_upcost,
)
from .mixin_replay import GatewayReplayMixin
from .recorder import Cassette
from .routing import (
    BusinessRoute,
    ProviderCandidate,
    configured_candidates,
)

log = get_logger("gateway")


class OpenAIGateway(
    GatewayBreakersMixin,
    GatewayReplayMixin,
    GatewayLiveCallsMixin,
):
    """The single doorway to every LLM. Modes:

    live   — real API calls (retry + circuit breaker + tier degradation)
    record — live, then persist the response into the cassette store
    replay — serve from cassette; optional deterministic stub on miss
    stub   — deterministic fake responses (dev/CI; banner-marked, never demoable)
    """


    def __init__(
        self,
        settings: Settings,
        usage_sink: UsageSink | None = None,
        budget_guard: BudgetGuard | None = None,
        faults: FaultInjector | None = None,
        credential_resolver: CredentialResolver | None = None,
    ):
        self._s = settings
        self._sink = usage_sink
        self._guard = budget_guard
        self._credential_resolver = credential_resolver
        # 故障注入 (M5-D2): 默认取进程内单例 -> admin 端点改开关立刻作用于本网关。
        self._faults = faults or get_fault_injector()
        self._cassette = Cassette(settings.gateway_cassette_dir)
        self._clients: dict[Tier, AsyncOpenAI] = {}
        self._candidate_clients: dict[str, AsyncOpenAI] = {}
        self._breakers: dict[Tier, CircuitBreaker] = {
            t: CircuitBreaker(
                window_seconds=settings.breaker_window_seconds,
                min_samples=settings.breaker_min_samples,
                failure_rate=settings.breaker_failure_rate,
                open_seconds=settings.breaker_open_seconds,
            )
            for t in Tier
        }
        self._candidate_breakers: dict[str, CircuitBreaker] = {
            candidate.id: self._new_breaker()
            for route in BusinessRoute
            for candidate in configured_candidates(settings, route)
        }
        self._candidate_failures: dict[str, str | None] = {
            candidate_id: None for candidate_id in self._candidate_breakers
        }

    def _new_breaker(self) -> CircuitBreaker:
        return CircuitBreaker(
            window_seconds=self._s.breaker_window_seconds,
            min_samples=self._s.breaker_min_samples,
            failure_rate=self._s.breaker_failure_rate,
            open_seconds=self._s.breaker_open_seconds,
        )

    # ---------- public API ----------

    async def chat(self, req: ChatRequest) -> ChatResult:
        visual_request = self._is_visual_request(req)
        self._enforce_visual_policy(req, visual_request)
        started = perf_counter()
        mode = self._s.gateway_mode
        if mode == "stub":
            result = self._stub_result(req, started)
        elif mode == "replay":
            if visual_request:
                raise GatewayError(
                    "VISION_REPLAY_DISABLED",
                    "视觉识别请求不使用共享回放，请使用平台视觉服务或稍后重试",
                )
            credential = await self._resolve_credential(req)
            if credential is not None and credential.source == CredentialSource.user:
                raise GatewayError(
                    "BYOK_REPLAY_DISABLED",
                    "自带模型请求不使用共享回放，请切换到在线模式",
                )
            result = self._replay_result(req, started)
        else:  # live / record
            credential = await self._resolve_credential(req)
            user_request = (
                credential is not None and credential.source == CredentialSource.user
            )
            if self._routing_for_request(req):
                try:
                    result = await self._live_chat(
                        req,
                        started,
                        credential=credential,
                        allow_cassette=False,
                    )
                except BaseException:
                    raise
                await self._emit_usage(
                    req,
                    result,
                    reserved_tier=result.reserved_tier,
                )
                return result
            req, reserved_tier = await self._budget_precheck(
                req, skip=user_request
            )
            try:
                result = await self._live_chat(
                    req,
                    started,
                    credential=credential,
                    allow_cassette=not user_request and not visual_request,
                )
            except BaseException:
                # 全链失败/取消: 返还预算预扣 (P1-12), 否则失败请求也占额度
                await self._refund_reservation(req, reserved_tier)
                raise
            if mode == "record" and not user_request and not visual_request:
                self._cassette.put(
                    self._cassette_key(req),
                    {
                        "text": result.text,
                        "model": result.model,
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                        "finish_reason": result.finish_reason,
                    },
                )
            await self._emit_usage(
                req,
                result,
                reserved_tier=reserved_tier or result.reserved_tier,
            )
            return result
        await self._emit_usage(req, result)
        return result

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamDelta | ChatResult]:
        visual_request = self._is_visual_request(req)
        self._enforce_visual_policy(req, visual_request)
        mode = self._s.gateway_mode
        if mode in ("stub", "replay"):
            started = perf_counter()
            if mode == "replay":
                if visual_request:
                    raise GatewayError(
                        "VISION_REPLAY_DISABLED",
                        "视觉识别请求不使用共享回放，请使用平台视觉服务或稍后重试",
                    )
                credential = await self._resolve_credential(req)
                if credential is not None and credential.source == CredentialSource.user:
                    raise GatewayError(
                        "BYOK_REPLAY_DISABLED",
                        "自带模型请求不使用共享回放，请切换到在线模式",
                    )
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

        credential = await self._resolve_credential(req)
        if credential is not None and credential.source == CredentialSource.user:
            async for item in self._live_user_stream(req, credential):
                yield item
            return
        if self._routing_for_request(req):
            async for item in self._routed_stream(req):
                yield item
            return
        req, reserved_tier = await self._budget_precheck(req)
        inner = self._live_stream(
            req,
            reserved_tier,
            allow_cassette=not visual_request,
            allow_record=not visual_request,
        )
        try:
            async for item in inner:
                yield item
        finally:
            # P0-6: async-for 提前退出 (客户端断开 aclose 本生成器) 不会自动关闭
            # 内层生成器 —— 显式 aclose 让 _live_stream 的 finally 立即执行
            # (部分消耗结算/预扣返还), 而不是等 GC 异步兜底。
            await inner.aclose()

    async def _resolve_credential(self, req: ChatRequest) -> ResolvedCredential | None:
        # Plan and visual routes are platform-owned even when an old caller
        # forgot to set credential_policy="platform".
        if self._route_for(req) in (BusinessRoute.vision, BusinessRoute.plan):
            return None
        if self._credential_resolver is None or req.credential_policy != "text":
            return None
        return await self._credential_resolver(req)

    def _is_visual_request(self, req: ChatRequest) -> bool:
        vision_models = {self._s.vision_model}
        if self._s.model_routing_enabled:
            vision_models.add(self._s.trenddao_vision_model)
        if req.model_override and req.model_override in vision_models:
            return True
        return any(
            isinstance(message.content, list)
            and any(
                part.get("type") == "image_url" or "image_url" in part
                for part in message.content
            )
            for message in req.messages
        )

    def _route_for(self, req: ChatRequest) -> BusinessRoute:
        if self._is_visual_request(req):
            return BusinessRoute.vision
        if req.tier == Tier.plan:
            return BusinessRoute.plan
        return BusinessRoute.text

    def _routing_for_request(self, req: ChatRequest) -> bool:
        """Use explicit business routes for every platform tier when enabled.

        ``Tier`` remains a budget/quality hint; it must not silently select a
        legacy provider.  In particular, quiz blueprint and critic requests
        use ``Tier.cheap`` and still need to follow the configured text route.
        """
        return self._s.model_routing_enabled

    @staticmethod
    def _enforce_visual_policy(req: ChatRequest, visual_request: bool) -> None:
        if visual_request and req.credential_policy != "platform":
            raise GatewayError(
                "VISION_PLATFORM_CREDENTIAL_REQUIRED",
                "视觉请求必须显式使用平台视觉凭据",
            )

    # ---------- internals ----------

    async def _budget_precheck(
        self,
        req: ChatRequest,
        *,
        skip: bool = False,
        reserve_tier: str | None = None,
        apply_soft_downgrade: bool = True,
    ) -> tuple[ChatRequest, str | None]:
        """live/record 入口预检 (预算强制唯一 choke point; stub/replay 零成本不预检)。

        返回 (req, reserved_tier): reserved_tier 非 None 表示 guard 已按该档位原子
        预扣估算额 (P1-12), 网关必须在结算/失败路径把它带给 settle 以差额返还。

        - None: 正常放行
        - "cheap": 软限降档 — tier 压到 cheap 且 max_tokens 加硬上限 (防降档后长回答穿透预算)
        - BudgetExceeded: 直接向上传播 (API 层转 429 + Retry-After)
        - guard 自身故障 (如 Redis 抖动) fail-open (无预扣), 预算功能不放大不可用性
        """
        if skip or self._guard is None:
            return req, None
        reserved_tier = reserve_tier or req.tier.value
        try:
            verdict = await self._guard.precheck(req.tenant_id, req.user_id, tier=reserved_tier)
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 — 预算预检故障不阻断业务, 只记日志
            log.exception("budget_precheck_failed_open")
            return req, None
        if verdict == "cheap" and apply_soft_downgrade:
            cap = self._s.budget_cheap_max_tokens
            max_tokens = min(req.max_tokens, cap) if req.max_tokens else cap
            target_tier = req.tier if req.model_override else Tier.cheap
            if req.tier != target_tier or req.max_tokens != max_tokens:
                log.warning(
                    "budget_soft_limit_downgrade",
                    user_id=req.user_id,
                    requested_tier=req.tier.value,
                )
                req = replace(req, tier=target_tier, max_tokens=max_tokens)
        return req, reserved_tier

    async def _refund_reservation(self, req: ChatRequest, reserved_tier: str | None) -> None:
        """调用彻底失败时返还 precheck 的预扣额 (settle 收到零成本事件即整额返还)。"""
        if self._guard is None or reserved_tier is None:
            return
        refund_event = UsageEvent(
            request_id=req.request_id,
            tenant_id=req.tenant_id,
            user_id=req.user_id,
            feature=req.feature,
            model="",
            tier=reserved_tier,
            prompt_tokens=0,
            completion_tokens=0,
            cached_tokens=0,
            cost_cny=0.0,
            latency_ms=0,
            mode="error",
        )
        try:
            await self._guard.settle(refund_event, reserved_tier=reserved_tier)
        except Exception:  # noqa: BLE001 — 返还失败只记日志 (方向是多扣, 安全侧)
            log.exception("budget_refund_failed")

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

    def _candidate_client(self, candidate: ProviderCandidate) -> AsyncOpenAI:
        """Cache clients by candidate identity, never by business tier."""
        if candidate.id not in self._candidate_clients:
            self._candidate_clients[candidate.id] = AsyncOpenAI(
                api_key=candidate.api_key or "missing-key",
                base_url=candidate.base_url,
                timeout=self._s.gateway_timeout_seconds,
                max_retries=0,
            )
        return self._candidate_clients[candidate.id]

    async def _user_client(self, credential: ResolvedCredential) -> AsyncOpenAI:
        """Create a request-scoped client; never cache plaintext user keys."""
        return AsyncOpenAI(
            api_key=credential.api_key,
            base_url=credential.base_url,
            timeout=self._s.gateway_timeout_seconds,
            max_retries=0,
        )

    def _completion_kwargs(
        self,
        req: ChatRequest,
        cfg: TierConfig,
        candidate: ProviderCandidate | None = None,
    ) -> dict:
        kwargs: dict = {
            "model": candidate.model if candidate is not None else req.model_override or cfg.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "temperature": req.temperature,
        }
        if req.max_tokens:
            kwargs["max_tokens"] = req.max_tokens
        if req.json_mode and (candidate is None or candidate.supports_json):
            kwargs["response_format"] = {"type": "json_object"}
        if candidate is None and cfg.enable_thinking is not None:
            # SiliconFlow exposes Qwen's dual-mode switch as an extension to
            # the OpenAI-compatible request body. Omit it for other providers.
            kwargs["extra_body"] = {"enable_thinking": cfg.enable_thinking}
        elif (
            candidate is not None
            and candidate.supports_thinking
            and candidate.enable_thinking is not None
        ):
            kwargs["extra_body"] = {"enable_thinking": candidate.enable_thinking}
        return kwargs

    @staticmethod
    def _has_image_parts(req: ChatRequest) -> bool:
        return any(
            isinstance(message.content, list)
            and any(
                part.get("type") == "image_url" or "image_url" in part
                for part in message.content
            )
            for message in req.messages
        )
