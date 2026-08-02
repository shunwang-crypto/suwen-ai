from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from time import perf_counter

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
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
from ..observability import observe_generation
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
    message_text,
)
from .circuit import CircuitBreaker
from .faults import FaultInjector, get_fault_injector
from .recorder import Cassette
from .stub import estimate_tokens, stub_text

log = get_logger("gateway")

_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)

# Degradation chain per requested tier: real different endpoints, tried in order.
# P1-10: cheap 不得降级到 standard (降级即涨价 5.7~23 倍, 高峰抖动时成本反向放大);
# cheap 失败直接走 cassette/stub 兜底。standard/strong 链只降不升；plan 是
# 独立业务档，DeepSeek 不可用时回退到通用 strong/standard/cheap 档。
_DEGRADE: dict[Tier, list[Tier]] = {
    Tier.cheap: [Tier.cheap],
    Tier.standard: [Tier.standard, Tier.cheap],
    Tier.strong: [Tier.strong, Tier.standard, Tier.cheap],
    Tier.plan: [Tier.plan, Tier.strong, Tier.standard, Tier.cheap],
}

# 档位序只表达 generic cheap/standard/strong 的历史顺序，供审计和契约检查。
# plan 是独立业务档，不能用静态序号假定它总比 strong 贵；实际是否升价由
# 当前配置的输入/输出单价判断。
_TIER_RANK: dict[Tier, int | None] = {
    Tier.cheap: 0,
    Tier.standard: 1,
    Tier.strong: 2,
    Tier.plan: None,
}


def _warn_if_upcost(settings: Settings, requested: Tier, used: Tier) -> None:
    """Warn when fallback can cost more under the active provider config.

    A plan fallback crosses a provider/business boundary, so its cost cannot
    be inferred from the generic tier ordinal. Comparing both token prices and
    warning when either is higher is conservative: no possible up-cost is
    silently treated as safe.
    """
    if requested == used:
        return
    requested_cfg = settings.tier(requested.value)
    used_cfg = settings.tier(used.value)
    input_upcost = used_cfg.input_price_per_m > requested_cfg.input_price_per_m
    output_upcost = used_cfg.output_price_per_m > requested_cfg.output_price_per_m
    if input_upcost or output_upcost:
        log.warning(
            "degraded_upcost",
            requested=requested.value,
            used=used.value,
            requested_input_price_per_m=requested_cfg.input_price_per_m,
            requested_output_price_per_m=requested_cfg.output_price_per_m,
            used_input_price_per_m=used_cfg.input_price_per_m,
            used_output_price_per_m=used_cfg.output_price_per_m,
            comparable_rank=_TIER_RANK[requested] is not None
            and _TIER_RANK[used] is not None,
        )


class OpenAIGateway:
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

    def breaker_state(self, tier: Tier) -> str:
        return self._breakers[tier].state.value

    def breaker_states(self) -> dict[str, str]:
        """全部档位的熔断器快照 (admin 故障注入面板展示用)。"""
        return {t.value: self._breakers[t].state.value for t in Tier}

    def trip_breaker(self, tier: Tier) -> None:
        """M5-D2: 一键熔断某一档 (不真的打 provider, 直接把断路器拉开)。"""
        self._breakers[tier].trip()
        log.warning("breaker_tripped_manually", tier=tier.value)

    def reset_breakers(self) -> None:
        for breaker in self._breakers.values():
            breaker.reset()
        log.warning("breakers_reset_manually")

    @property
    def faults(self) -> FaultInjector:
        return self._faults

    async def _resolve_credential(self, req: ChatRequest) -> ResolvedCredential | None:
        if self._credential_resolver is None or req.credential_policy != "text":
            return None
        return await self._credential_resolver(req)

    def _is_visual_request(self, req: ChatRequest) -> bool:
        if self._s.vision_model and req.model_override == self._s.vision_model:
            return True
        return any(
            isinstance(message.content, list)
            and any(
                part.get("type") == "image_url" or "image_url" in part
                for part in message.content
            )
            for message in req.messages
        )

    @staticmethod
    def _enforce_visual_policy(req: ChatRequest, visual_request: bool) -> None:
        if visual_request and req.credential_policy != "platform":
            raise GatewayError(
                "VISION_PLATFORM_CREDENTIAL_REQUIRED",
                "视觉请求必须显式使用平台视觉凭据",
            )

    # ---------- internals ----------

    async def _budget_precheck(
        self, req: ChatRequest, *, skip: bool = False
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
        reserved_tier = req.tier.value
        try:
            verdict = await self._guard.precheck(req.tenant_id, req.user_id, tier=reserved_tier)
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 — 预算预检故障不阻断业务, 只记日志
            log.exception("budget_precheck_failed_open")
            return req, None
        if verdict == "cheap":
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

    def _cost(self, tier_cfg: TierConfig, p_tok: int, c_tok: int) -> float:
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

    async def _user_client(self, credential: ResolvedCredential) -> AsyncOpenAI:
        """Create a request-scoped client; never cache plaintext user keys."""
        return AsyncOpenAI(
            api_key=credential.api_key,
            base_url=credential.base_url,
            timeout=self._s.gateway_timeout_seconds,
            max_retries=0,
        )

    def _completion_kwargs(self, req: ChatRequest, cfg: TierConfig) -> dict:
        kwargs: dict = {
            "model": req.model_override or cfg.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "temperature": req.temperature,
        }
        if req.max_tokens:
            kwargs["max_tokens"] = req.max_tokens
        if req.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if cfg.enable_thinking is not None:
            # SiliconFlow exposes Qwen's dual-mode switch as an extension to
            # the OpenAI-compatible request body. Omit it for other providers.
            kwargs["extra_body"] = {"enable_thinking": cfg.enable_thinking}
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
                # 故障注入 (M5-D2): 只在真正要发网络请求的这一刻生效。抛的是
                # _RETRYABLE 里的 APIConnectionError -> 重试→熔断→降级链完整可演示。
                await self._faults.apply(tier.value, cfg.base_url)
                resp = await self._client(tier).chat.completions.create(
                    **self._completion_kwargs(req, cfg)
                )
        usage = resp.usage
        p_tok = usage.prompt_tokens if usage else 0
        c_tok = usage.completion_tokens if usage else 0
        cost = self._cost(cfg, p_tok, c_tok)
        return ChatResult(
            text=resp.choices[0].message.content or "",
            model=resp.model or req.model_override or cfg.model,
            tier=tier.value,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cost_cny=cost,
            latency_ms=int((perf_counter() - started) * 1000),
            mode="live",
            degraded=tier != req.tier,
            credential_source=CredentialSource.platform.value,
            provider="platform",
            platform_billed_cost_cny=cost,
        )

    async def _call_user_tier(
        self, req: ChatRequest, tier: Tier, credential: ResolvedCredential, *, model: str | None = None
    ) -> ChatResult:
        """Call a user's provider without ever entering the platform chain."""
        from openai import AuthenticationError

        cfg = TierConfig(
            api_key=credential.api_key,
            base_url=credential.base_url,
            model=model or credential.model,
            enable_thinking=None,
        )
        client = await self._user_client(credential)
        started = perf_counter()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._s.gateway_max_retries),
                wait=wait_exponential(multiplier=0.5, max=8),
                retry=retry_if_exception_type(_RETRYABLE),
                reraise=True,
            ):
                with attempt:
                    await self._faults.apply(tier.value, credential.base_url)
                    resp = await client.chat.completions.create(
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
                cost_cny=0.0,
                latency_ms=int((perf_counter() - started) * 1000),
                mode="live",
                credential_source=CredentialSource.user.value,
                provider=credential.provider,
                platform_billed_cost_cny=0.0,
            )
        except AuthenticationError as exc:
            raise GatewayError("BYOK_KEY_INVALID", "自带 DeepSeek API Key 无效") from exc
        except RateLimitError as exc:
            raise GatewayError("BYOK_RATE_LIMITED", "自带 DeepSeek 请求过于频繁") from exc
        except APIStatusError as exc:
            if exc.status_code in (401, 403):
                raise GatewayError("BYOK_KEY_INVALID", "自带 DeepSeek API Key 无效") from exc
            if exc.status_code == 402:
                raise GatewayError("BYOK_BALANCE_INSUFFICIENT", "自带 DeepSeek 账户余额不足") from exc
            raise GatewayError("BYOK_PROVIDER_UNAVAILABLE", "自带 DeepSeek 服务暂时不可用") from exc
        except (APITimeoutError, APIConnectionError, InternalServerError) as exc:
            raise GatewayError("BYOK_PROVIDER_UNAVAILABLE", "自带 DeepSeek 服务暂时不可用") from exc
        finally:
            await client.close()

    async def _live_user_chat(
        self, req: ChatRequest, started: float, credential: ResolvedCredential
    ) -> ChatResult:
        try:
            return await self._call_user_tier(req, req.tier, credential)
        except GatewayError:
            # A reasoning request may use the user's normal model as an
            # intra-user fallback. It still never touches a platform key.
            if req.tier == Tier.strong and credential.fallback_model:
                try:
                    return await self._call_user_tier(
                        req, req.tier, credential, model=credential.fallback_model
                    )
                except GatewayError:
                    pass
            if not credential.fallback_to_platform:
                raise
            # Only an explicit opt-in reaches the original platform chain.
            req, reserved = await self._budget_precheck(req)
            try:
                result = await self._live_chat(
                    req, started, credential=None, allow_cassette=False
                )
            except BaseException:
                await self._refund_reservation(req, reserved)
                raise
            result.reserved_tier = reserved
            return result

    async def _live_chat(
        self,
        req: ChatRequest,
        started: float,
        credential: ResolvedCredential | None = None,
        *,
        allow_cassette: bool = True,
    ) -> ChatResult:
        if credential is not None and credential.source == CredentialSource.user:
            return await self._live_user_chat(req, started, credential)
        last_error: Exception | None = None
        tiers = _DEGRADE[req.tier] if req.allow_tier_fallback else (req.tier,)
        for tier in tiers:
            breaker = self._breakers[tier]
            if not breaker.allow():
                log.warning("breaker_open_skip", tier=tier.value)
                continue
            try:
                result = await self._call_tier(req, tier)
                breaker.record(True)
                if result.degraded:
                    log.warning("tier_degraded", requested=req.tier.value, used=tier.value)
                    _warn_if_upcost(self._s, req.tier, tier)
                return result
            except Exception as exc:  # noqa: BLE001 — 每个 provider 失败都要继续降级链
                breaker.record(False)
                last_error = exc
                log.error("tier_call_failed", tier=tier.value, error=str(exc))
        # all live providers failed -> cassette, then (optionally) stub
        hit = self._cassette.get(self._cassette_key(req)) if allow_cassette else None
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
        raise GatewayError("ALL_PROVIDERS_FAILED", f"全部模型通道失败: {last_error}")

    async def _live_user_stream(
        self, req: ChatRequest, credential: ResolvedCredential
    ) -> AsyncIterator[StreamDelta | ChatResult]:
        """Streaming BYOK path with the same source and fallback semantics."""
        started = perf_counter()
        # A strong request may first try the user's reasoning model and then
        # the user's normal model.  The second attempt is still BYOK; platform
        # credentials are considered only after this list and explicit opt-in.
        models = [credential.model]
        if req.tier == Tier.strong and credential.fallback_model:
            models.append(credential.fallback_model)

        for model_index, model in enumerate(models):
            client = await self._user_client(credential)
            cfg = TierConfig(
                api_key=credential.api_key,
                base_url=credential.base_url,
                model=model,
                enable_thinking=None,
            )
            parts: list[str] = []
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(self._s.gateway_max_retries),
                    wait=wait_exponential(multiplier=0.5, max=8),
                    retry=retry_if_exception_type(_RETRYABLE),
                    reraise=True,
                ):
                    with attempt:
                        await self._faults.apply(req.tier.value, credential.base_url)
                        stream = await client.chat.completions.create(
                            stream=True,
                            stream_options={"include_usage": True},
                            **self._completion_kwargs(req, cfg),
                        )
                p_tok = c_tok = 0
                async for chunk in stream:
                    if chunk.usage:
                        p_tok = chunk.usage.prompt_tokens
                        c_tok = chunk.usage.completion_tokens
                    if chunk.choices and chunk.choices[0].delta.content:
                        delta = chunk.choices[0].delta.content
                        parts.append(delta)
                        yield StreamDelta(delta)
                if not p_tok:
                    p_tok = sum(estimate_tokens(message_text(m.content)) for m in req.messages)
                    c_tok = estimate_tokens("".join(parts))
                result = ChatResult(
                    text="".join(parts),
                    model=model,
                    tier=req.tier.value,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    cost_cny=0.0,
                    latency_ms=int((perf_counter() - started) * 1000),
                    mode="live",
                    degraded=model_index > 0,
                    credential_source=CredentialSource.user.value,
                    provider=credential.provider,
                    platform_billed_cost_cny=0.0,
                )
                await self._emit_usage(req, result)
                yield result
                return
            except (AuthenticationError, RateLimitError, APIStatusError, APITimeoutError, APIConnectionError, InternalServerError, GatewayError) as exc:
                error = self._byok_error(exc)
                if parts:
                    await self._emit_user_partial(req, credential, model, parts, started)
                    raise error from exc
                if model_index + 1 < len(models):
                    continue
                if not credential.fallback_to_platform:
                    raise error from exc
                req, reserved = await self._budget_precheck(req)
                async for item in self._live_stream(
                    req, reserved, allow_cassette=False, allow_record=False
                ):
                    yield item
                return
            finally:
                await client.close()

        # The loop always returns or raises.  Keep a defensive error in case a
        # future change makes the model list empty.
        raise GatewayError("BYOK_PROVIDER_UNAVAILABLE", "自带 DeepSeek 服务暂时不可用")

    @staticmethod
    def _byok_error(exc: BaseException) -> GatewayError:
        """Map provider failures without exposing provider response details."""
        if isinstance(exc, GatewayError):
            return exc
        if isinstance(exc, AuthenticationError):
            return GatewayError("BYOK_KEY_INVALID", "自带 DeepSeek API Key 无效")
        if isinstance(exc, RateLimitError):
            return GatewayError("BYOK_RATE_LIMITED", "自带 DeepSeek 请求过于频繁")
        if isinstance(exc, APIStatusError):
            if exc.status_code in (401, 403):
                return GatewayError("BYOK_KEY_INVALID", "自带 DeepSeek API Key 无效")
            if exc.status_code == 402:
                return GatewayError("BYOK_BALANCE_INSUFFICIENT", "自带 DeepSeek 账户余额不足")
        return GatewayError("BYOK_PROVIDER_UNAVAILABLE", "自带 DeepSeek 服务暂时不可用")

    async def _emit_user_partial(
        self,
        req: ChatRequest,
        credential: ResolvedCredential,
        model: str,
        parts: list[str],
        started: float,
    ) -> None:
        """Account a partially received user-provider stream without charging platform."""
        partial = ChatResult(
            text="".join(parts),
            model=model,
            tier=req.tier.value,
            prompt_tokens=sum(estimate_tokens(message_text(m.content)) for m in req.messages),
            completion_tokens=estimate_tokens("".join(parts)),
            cost_cny=0.0,
            latency_ms=int((perf_counter() - started) * 1000),
            mode="live",
            degraded=True,
            credential_source=CredentialSource.user.value,
            provider=credential.provider,
            platform_billed_cost_cny=0.0,
        )
        await self._emit_usage(req, partial)

    async def _live_stream(
        self,
        req: ChatRequest,
        reserved_tier: str | None = None,
        *,
        allow_cassette: bool = True,
        allow_record: bool = True,
    ) -> AsyncIterator[StreamDelta | ChatResult]:
        started = perf_counter()
        # P0-6 (计费半段): 客户端断开时 sse_starlette 关闭本生成器 (GeneratorExit/
        # CancelledError 落在 yield 点), 上游 token 已消耗必须照样结算 —— 记录
        # 流式进行中的 tier 与已收内容, finally 里对未结算的部分兜底计费。
        settled = False
        streaming_tier: Tier | None = None
        parts: list[str] = []
        try:
            tiers = _DEGRADE[req.tier] if req.allow_tier_fallback else (req.tier,)
            for tier in tiers:
                breaker = self._breakers[tier]
                if not breaker.allow():
                    continue
                cfg = self._s.tier(tier.value)
                if not cfg.api_key:
                    breaker.record(False)
                    continue
                try:
                    await self._faults.apply(tier.value, cfg.base_url)  # M5-D2
                    kwargs = self._completion_kwargs(req, cfg)
                    stream = await self._client(tier).chat.completions.create(
                        stream=True, stream_options={"include_usage": True}, **kwargs
                    )
                    parts = []
                    streaming_tier = tier
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
                    if tier != req.tier:
                        _warn_if_upcost(self._s, req.tier, tier)
                    text = "".join(parts)
                    if not p_tok:
                        p_tok = sum(estimate_tokens(message_text(m.content)) for m in req.messages)
                        c_tok = estimate_tokens(text)
                    cost = self._cost(cfg, p_tok, c_tok)
                    result = ChatResult(
                        text=text,
                        model=req.model_override or cfg.model,
                        tier=tier.value,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        cost_cny=cost,
                        latency_ms=int((perf_counter() - started) * 1000),
                        mode="live",
                        degraded=tier != req.tier,
                        credential_source=CredentialSource.platform.value,
                        provider="platform",
                        platform_billed_cost_cny=cost,
                    )
                    if self._s.gateway_mode == "record" and allow_record:
                        self._cassette.put(
                            self._cassette_key(req),
                            {
                                "text": text,
                                "model": result.model,
                                "prompt_tokens": p_tok,
                                "completion_tokens": c_tok,
                            },
                        )
                    await self._emit_usage(req, result, reserved_tier=reserved_tier)
                    settled = True
                    yield result
                    return
                except Exception as exc:  # noqa: BLE001
                    streaming_tier = None  # 本档已失败计入降级链, 不再按部分消耗结算
                    breaker.record(False)
                    log.error("stream_tier_failed", tier=tier.value, error=str(exc))
            # degrade to non-stream fallback path (cassette/stub), chunked out
            streaming_tier = None
            try:
                fallback = await self._live_chat(
                    req, started, allow_cassette=allow_cassette
                )
            except BaseException:
                await self._refund_reservation(req, reserved_tier)
                settled = True  # 预扣已返还, finally 无需再兜底
                raise
            chunk_n = max(1, self._s.gateway_stream_chunk_chars)
            for i in range(0, len(fallback.text), chunk_n):
                yield StreamDelta(fallback.text[i : i + chunk_n])
            await self._emit_usage(req, fallback, reserved_tier=reserved_tier)
            settled = True
            yield fallback
        finally:
            if not settled:
                if streaming_tier is not None:
                    # 半途中断 (断网/切后台/切会话): 按已收内容估算结算, 不再漏计费。
                    coro = self._settle_partial_stream(
                        req, streaming_tier, "".join(parts), started, reserved_tier
                    )
                else:
                    # 中断发生在降级兜底分块输出期间 (无真实消耗): 只返还预扣
                    coro = self._refund_reservation(req, reserved_tier)
                # ensure_future + shield: 即便本生成器正被取消, 结算协程也跑完
                task = asyncio.ensure_future(coro)
                try:
                    await asyncio.shield(task)
                except BaseException:  # noqa: BLE001 — 取消传播中, 结算任务后台完成
                    pass

    async def _settle_partial_stream(
        self,
        req: ChatRequest,
        tier: Tier,
        text: str,
        started: float,
        reserved_tier: str | None,
    ) -> None:
        """客户端断流的部分消耗结算 (P0-6): token 用估算 (上游 usage 帧未到达),
        degraded=True 标记这是部分/非常规结果; 走 _emit_usage 统一进预算与 usage_log。"""
        cfg = self._s.tier(tier.value)
        p_tok = sum(estimate_tokens(message_text(m.content)) for m in req.messages)
        c_tok = estimate_tokens(text)
        cost = self._cost(cfg, p_tok, c_tok)
        partial = ChatResult(
            text=text,
            model=req.model_override or cfg.model,
            tier=tier.value,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cost_cny=cost,
            latency_ms=int((perf_counter() - started) * 1000),
            mode="live",
            degraded=True,  # partial: 客户端中断, token 为估算值
            credential_source=CredentialSource.platform.value,
            provider="platform",
            platform_billed_cost_cny=cost,
        )
        log.warning(
            "stream_partial_settled",
            tier=tier.value,
            chars=len(text),
            feature=req.feature,
        )
        await self._emit_usage(req, partial, reserved_tier=reserved_tier)

    async def _emit_usage(
        self,
        req: ChatRequest,
        result: ChatResult,
        reserved_tier: str | None = None,
    ) -> None:
        event = UsageEvent(
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
            credential_source=result.credential_source,
            provider=result.provider,
            platform_billed_cost_cny=(
                result.cost_cny
                if result.credential_source == CredentialSource.platform.value
                else result.platform_billed_cost_cny
            ),
        )
        # 预算结算: live 结果按实际成本与预扣差额结算; 非 live 但有预扣 (降级兜底
        # stub/cassette, cost=0) 也必须结算一次以返还预扣 (P1-12)。
        # 结算失败只记日志, 不打断已完成的响应。
        if self._guard is not None and (result.mode == "live" or reserved_tier is not None):
            try:
                await self._guard.settle(event, reserved_tier=reserved_tier)
            except Exception:  # noqa: BLE001
                log.exception("budget_settle_failed")
        # M3-D1 Langfuse: 每次网关调用一条 generation (未配置时内部短路;
        # 全 try/except + OTel 后台批量导出, 不阻塞也不影响业务返回)。
        # _emit_usage 是所有完成调用 (chat/stream/部分结算/stub/replay) 的
        # 唯一汇点, 上报字段与 usage 计量同源。
        observe_generation(self._s, req, result)
        if self._sink is None:
            return
        await self._sink(event)
