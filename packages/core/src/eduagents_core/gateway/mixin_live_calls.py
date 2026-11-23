"""候选路由与档位直连的真实调用/流式/结算 (从 openai_gateway_mixins.py 按域拆出, 纯搬移零行为变更).

共享常量 (_RETRYABLE/_DEGRADE/_TIER_RANK/_ROUTING_RETRYABLE_CODES) 与
_warn_if_upcost 只被本 mixin 的实现使用, 随迁至本模块。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from time import perf_counter
from typing import TYPE_CHECKING

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
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

from ..config import TierConfig
from ..logging import get_logger
from ..observability import observe_generation
from .base import (
    BudgetExceeded,
    ChatMessage,
    ChatResult,
    CredentialSource,
    GatewayError,
    StreamDelta,
    Tier,
    UsageEvent,
    message_text,
)
from .routing import BusinessRoute, route_candidates
from .stub import estimate_tokens
from .thought_filter import ThoughtFilter, strip_thought

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from openai import AsyncOpenAI

    from ..config import Settings
    from .base import BudgetGuard, ChatRequest, ResolvedCredential, UsageSink
    from .circuit import CircuitBreaker
    from .faults import FaultInjector
    from .recorder import Cassette
    from .routing import ProviderCandidate

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

_ROUTING_RETRYABLE_CODES = {
    "PROVIDER_UNAVAILABLE",
    "MODEL_UNAVAILABLE",
    "MODEL_NOT_FOUND",
    "NO_API_KEY",
    "PUBLIC_AUTH_UNAVAILABLE",
    "UPSTREAM_UNAVAILABLE",
    "UPSTREAM_TIMEOUT",
    "UPSTREAM_RATE_LIMITED",
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


class GatewayLiveCallsMixin:
    """候选路由与档位直连的真实调用/流式/结算实现 (纯搬移)."""

    if TYPE_CHECKING:
        # 宿主 OpenAIGateway.__init__ 注入的状态 (隐式契约显式化);
        # 跨 mixin 的方法 (_cost/_stub_result/_cassette_key 在 GatewayReplayMixin,
        # _client/_completion_kwargs 等在宿主) 由最终 MRO 解析。
        _s: Settings
        _sink: UsageSink | None
        _guard: BudgetGuard | None
        _breakers: dict[Tier, CircuitBreaker]
        _candidate_breakers: dict[str, CircuitBreaker]
        _candidate_failures: dict[str, str | None]
        _cassette: Cassette
        _faults: FaultInjector
        _clients: dict[Tier, AsyncOpenAI]
        _candidate_clients: dict[str, AsyncOpenAI]

    def _candidate_capable(self, req: ChatRequest, candidate: ProviderCandidate, *, streaming: bool) -> bool:
        if self._has_image_parts(req) and not candidate.supports_vision:
            return False
        if req.json_mode and not candidate.supports_json:
            return False
        if streaming and not candidate.supports_stream:
            return False
        return True

    async def _call_candidate(self, req: ChatRequest, candidate: ProviderCandidate) -> ChatResult:
        """Call one concrete provider candidate with its own endpoint/model."""
        if not candidate.configured:
            raise GatewayError("NO_API_KEY", "provider candidate is not configured")
        if not self._candidate_capable(req, candidate, streaming=False):
            raise GatewayError("MODEL_CAPABILITY_UNSUPPORTED", "provider candidate lacks request capability")
        started = perf_counter()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._s.gateway_max_retries),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        ):
            with attempt:
                await self._faults.apply(candidate.id, candidate.base_url)
                resp = await self._candidate_client(candidate).chat.completions.create(
                    **self._completion_kwargs(req, TierConfig(model=candidate.model), candidate)
                )
        usage = resp.usage
        p_tok = usage.prompt_tokens if usage else 0
        c_tok = usage.completion_tokens if usage else 0
        cost = self._cost(candidate, p_tok, c_tok)
        return ChatResult(
            text=strip_thought(resp.choices[0].message.content or ""),
            model=candidate.model,
            tier=req.tier.value,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cost_cny=cost,
            latency_ms=int((perf_counter() - started) * 1000),
            mode="live",
            degraded=False,
            credential_source=CredentialSource.platform.value,
            provider=candidate.provider,
            route=candidate.route.value,
            platform_billed_cost_cny=cost,
            finish_reason=getattr(resp.choices[0], "finish_reason", None),
            reasoning_content=getattr(resp.choices[0].message, "reasoning_content", None),
            tool_calls=tuple(
                call.model_dump(exclude_none=True)
                for call in (getattr(resp.choices[0].message, "tool_calls", None) or [])
            ),
        )

    async def _create_candidate_stream(self, req: ChatRequest, candidate: ProviderCandidate):
        if not candidate.configured:
            raise GatewayError("NO_API_KEY", "provider candidate is not configured")
        if not self._candidate_capable(req, candidate, streaming=True):
            raise GatewayError("MODEL_CAPABILITY_UNSUPPORTED", "provider candidate lacks request capability")
        stream = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._s.gateway_max_retries),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        ):
            with attempt:
                await self._faults.apply(candidate.id, candidate.base_url)
                stream = await self._candidate_client(candidate).chat.completions.create(
                    stream=True,
                    stream_options={"include_usage": True},
                    **self._completion_kwargs(req, TierConfig(model=candidate.model), candidate),
                )
        return stream

    async def _routed_chat(self, req: ChatRequest, *, allow_cassette: bool = False) -> ChatResult:
        del allow_cassette  # routed platform responses never enter shared cassettes
        route = self._route_for(req)
        try:
            if route == BusinessRoute.vision and req.json_mode:
                # MiMo/Qwen-VL is deliberately used as a faithful Markdown
                # transcriber.  A second text call performs JSON structuring, so
                # response_format is never sent to a visual candidate that lacks it.
                vision_req = replace(req, json_mode=False, allow_tier_fallback=False)
                vision_result = await self._routed_chat_single(vision_req, BusinessRoute.vision)
                await self._emit_usage(
                    vision_req,
                    vision_result,
                    reserved_tier=vision_result.reserved_tier,
                    billing_reference=(
                        f"{req.billing_reference or req.request_id}:vision"
                        if req.billing_reference or req.request_id else ""
                    ),
                )
                plain_messages = tuple(
                    ChatMessage(role=message.role, content=message_text(message.content))
                    for message in req.messages
                    if message_text(message.content)
                )
                structure_messages = plain_messages + (
                    ChatMessage(
                        role="user",
                        content=(
                            "请根据以下视觉模型忠实转写的 Markdown，完成原请求要求的结构化输出。"
                            "只输出符合 JSON schema 的 JSON，不要添加解释。\n\n"
                            + vision_result.text
                        ),
                    ),
                )
                structure_req = replace(
                    req,
                    messages=structure_messages,
                    model_override=None,
                    json_mode=True,
                    credential_policy="platform",
                )
                return await self._routed_chat_single(structure_req, BusinessRoute.text)
            return await self._routed_chat_single(req, route)
        except GatewayError as exc:
            # Keep the long-standing development fault-injection contract for
            # the explicit provider routes too.  A real production request
            # never becomes a stub response, and request-shape/auth failures
            # still surface as errors instead of being masked.
            if (
                exc.code.endswith("_ROUTE_UNAVAILABLE")
                and self._s.gateway_replay_fallback_stub
                and self._s.app_env != "prod"
            ):
                log.error("routed_live_failed_fallback_stub", route=route.value)
                result = self._stub_result(req, perf_counter())
                result.degraded = True
                return result
            raise

    async def _routed_chat_single(
        self, req: ChatRequest, route: BusinessRoute
    ) -> ChatResult:
        candidates = route_candidates(self._s, route)
        if not candidates:
            raise self._route_unavailable(route)
        last_failure_code: str | None = None
        for candidate in candidates:
            if not self._candidate_capable(req, candidate, streaming=False):
                continue
            breaker = self._candidate_breakers[candidate.id]
            if not breaker.allow():
                continue
            reserved_tier: str | None = None
            try:
                candidate_req = req
                if not candidate.free:
                    # Free candidates must be attempted before this call.  A
                    # paid candidate gets its own wallet/budget precheck and
                    # reservation only at the moment it is about to run.
                    candidate_req, reserved_tier = await self._budget_precheck(
                        req,
                        reserve_tier=candidate.reserve_tier,
                        apply_soft_downgrade=False,
                    )
                result = await self._call_candidate(candidate_req, candidate)
                breaker.record(True)
                result.reserved_tier = reserved_tier
                result.degraded = candidate.id != candidates[0].id
                return result
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 — classify before fallback
                breaker.record(False)
                last_failure_code = self._failure_code(exc)
                self._candidate_failures[candidate.id] = last_failure_code
                if reserved_tier is not None:
                    await self._refund_reservation(req, reserved_tier)
                log.warning(
                    "provider_candidate_failed",
                    candidate_id=candidate.id,
                    route=route.value,
                    failure_code=last_failure_code,
                )
                if not self._is_routing_retryable(exc):
                    raise self._route_request_error(route) from exc
        raise self._route_unavailable(route, last_failure_code)

    async def _routed_stream(self, req: ChatRequest) -> AsyncIterator[StreamDelta | ChatResult]:
        route = self._route_for(req)
        if route == BusinessRoute.vision and req.json_mode:
            vision_req = replace(req, json_mode=False, allow_tier_fallback=False)
            vision_result = await self._routed_chat_single(vision_req, BusinessRoute.vision)
            await self._emit_usage(
                vision_req,
                vision_result,
                reserved_tier=vision_result.reserved_tier,
                billing_reference=(
                    f"{req.billing_reference or req.request_id}:vision"
                    if req.billing_reference or req.request_id else ""
                ),
            )
            plain_messages = tuple(
                ChatMessage(role=message.role, content=message_text(message.content))
                for message in req.messages
                if message_text(message.content)
            )
            structure_req = replace(
                req,
                messages=plain_messages
                + (
                    ChatMessage(
                        role="user",
                        content="请将以下视觉转写 Markdown 结构化为 JSON：\n\n" + vision_result.text,
                    ),
                ),
                model_override=None,
                credential_policy="platform",
            )
            async for item in self._routed_stream_single(structure_req, BusinessRoute.text):
                yield item
            return
        async for item in self._routed_stream_single(req, route):
            yield item

    async def _routed_stream_single(
        self, req: ChatRequest, route: BusinessRoute
    ) -> AsyncIterator[StreamDelta | ChatResult]:
        candidates = route_candidates(self._s, route)
        if not candidates:
            raise self._route_unavailable(route)
        last_failure_code: str | None = None
        for candidate in candidates:
            if not self._candidate_capable(req, candidate, streaming=True):
                continue
            breaker = self._candidate_breakers[candidate.id]
            if not breaker.allow():
                continue
            reserved_tier: str | None = None
            parts: list[str] = []
            started = perf_counter()
            try:
                candidate_req = req
                if not candidate.free:
                    candidate_req, reserved_tier = await self._budget_precheck(
                        req,
                        reserve_tier=candidate.reserve_tier,
                        apply_soft_downgrade=False,
                    )
                stream = await self._create_candidate_stream(candidate_req, candidate)
                tf = ThoughtFilter()
                p_tok = c_tok = 0
                async for chunk in stream:
                    if chunk.usage:
                        p_tok = chunk.usage.prompt_tokens
                        c_tok = chunk.usage.completion_tokens
                    if chunk.choices and chunk.choices[0].delta.content:
                        delta = tf.feed(chunk.choices[0].delta.content)
                        if delta:
                            parts.append(delta)
                            yield StreamDelta(delta)
                parts.append(tf.flush())
                breaker.record(True)
                text = "".join(parts)
                if not p_tok:
                    p_tok = sum(estimate_tokens(message_text(m.content)) for m in req.messages)
                    c_tok = estimate_tokens(text)
                cost = self._cost(candidate, p_tok, c_tok)
                result = ChatResult(
                    text=text,
                    model=candidate.model,
                    tier=req.tier.value,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    cost_cny=cost,
                    latency_ms=int((perf_counter() - started) * 1000),
                    mode="live",
                    degraded=candidate.id != candidates[0].id,
                    credential_source=CredentialSource.platform.value,
                    provider=candidate.provider,
                    route=route.value,
                    platform_billed_cost_cny=cost,
                    reserved_tier=reserved_tier,
                )
                await self._emit_usage(req, result, reserved_tier=reserved_tier)
                yield result
                return
            except BudgetExceeded:
                raise
            except BaseException as exc:
                # Once a delta escaped, retrying would duplicate visible text.
                if parts:
                    if not isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                        breaker.record(False)
                        self._candidate_failures[candidate.id] = self._failure_code(exc)
                    if reserved_tier is not None:
                        await self._refund_reservation(req, reserved_tier)
                    await self._settle_partial_candidate(
                        req, candidate, "".join(parts), started, reserved_tier=None
                    )
                    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                        raise
                    raise self._route_stream_error(route) from exc
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                    if reserved_tier is not None:
                        await self._refund_reservation(req, reserved_tier)
                    raise
                if reserved_tier is not None:
                    await self._refund_reservation(req, reserved_tier)
                breaker.record(False)
                last_failure_code = self._failure_code(exc)
                self._candidate_failures[candidate.id] = last_failure_code
                if not self._is_routing_retryable(exc):
                    raise self._route_request_error(route) from exc
        raise self._route_unavailable(route, last_failure_code)

    async def _settle_partial_candidate(
        self,
        req: ChatRequest,
        candidate: ProviderCandidate,
        text: str,
        started: float,
        reserved_tier: str | None,
    ) -> None:
        p_tok = sum(estimate_tokens(message_text(m.content)) for m in req.messages)
        c_tok = estimate_tokens(text)
        cost = self._cost(candidate, p_tok, c_tok)
        result = ChatResult(
            text=text,
            model=candidate.model,
            tier=req.tier.value,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cost_cny=cost,
            latency_ms=int((perf_counter() - started) * 1000),
            mode="live",
            degraded=True,
            credential_source=CredentialSource.platform.value,
            provider=candidate.provider,
            route=candidate.route.value,
            platform_billed_cost_cny=cost,
        )
        await self._emit_usage(req, result, reserved_tier=reserved_tier)

    @staticmethod
    def _failure_code(exc: BaseException) -> str:
        if isinstance(exc, GatewayError):
            return exc.code
        status = getattr(exc, "status_code", None)
        if status == 429:
            return "UPSTREAM_RATE_LIMITED"
        if status in (401, 403):
            return "PUBLIC_AUTH_UNAVAILABLE"
        if status == 404:
            return "MODEL_NOT_FOUND"
        if isinstance(exc, APITimeoutError) or isinstance(exc, asyncio.TimeoutError):
            return "UPSTREAM_TIMEOUT"
        if isinstance(exc, APIConnectionError):
            return "UPSTREAM_UNAVAILABLE"
        if isinstance(exc, APIStatusError) and status is not None and status >= 500:
            return "UPSTREAM_UNAVAILABLE"
        return "MODEL_REQUEST_INVALID"

    @staticmethod
    def _is_routing_retryable(exc: BaseException) -> bool:
        if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
            return True
        if isinstance(exc, asyncio.TimeoutError):
            return True
        if isinstance(exc, AuthenticationError):
            return True
        if isinstance(exc, APIStatusError):
            status = exc.status_code
            # A generic 403 may be content-safety rejection. Only the SDK's
            # explicit AuthenticationError (or a sanitized GatewayError) is
            # eligible for provider fallback.
            return status == 404 or status == 429 or status == 401 or status >= 500
        return isinstance(exc, GatewayError) and (
            exc.code in _ROUTING_RETRYABLE_CODES or exc.code.startswith("UPSTREAM_")
        )

    @staticmethod
    def _route_unavailable(route: BusinessRoute, failure_code: str | None = None) -> GatewayError:
        del failure_code
        codes = {
            BusinessRoute.text: ("MODEL_ROUTE_UNAVAILABLE", "文本模型通道暂时不可用，请稍后重试"),
            BusinessRoute.vision: ("VISION_ROUTE_UNAVAILABLE", "视觉模型通道暂时不可用，请稍后重试"),
            BusinessRoute.plan: ("PLAN_ROUTE_UNAVAILABLE", "学习计划模型通道暂时不可用，请稍后重试"),
        }
        code, message = codes[route]
        return GatewayError(code, message)

    @staticmethod
    def _route_request_error(route: BusinessRoute) -> GatewayError:
        del route
        return GatewayError("MODEL_REQUEST_INVALID", "模型请求格式无效，请修改后重试")

    @staticmethod
    def _route_stream_error(route: BusinessRoute) -> GatewayError:
        del route
        return GatewayError("MODEL_STREAM_INTERRUPTED", "模型输出已中断，请重试")

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
            text=strip_thought(resp.choices[0].message.content or ""),
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
            finish_reason=getattr(resp.choices[0], "finish_reason", None),
            reasoning_content=getattr(resp.choices[0].message, "reasoning_content", None),
            tool_calls=tuple(
                call.model_dump(exclude_none=True)
                for call in (getattr(resp.choices[0].message, "tool_calls", None) or [])
            ),
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
                text=strip_thought(resp.choices[0].message.content or ""),
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
                finish_reason=getattr(resp.choices[0], "finish_reason", None),
                reasoning_content=getattr(resp.choices[0].message, "reasoning_content", None),
                tool_calls=tuple(
                    call.model_dump(exclude_none=True)
                    for call in (getattr(resp.choices[0].message, "tool_calls", None) or [])
                ),
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
            if self._routing_for_request(req):
                result = await self._routed_chat(req, allow_cassette=False)
                return result
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
        if self._routing_for_request(req):
            return await self._routed_chat(req, allow_cassette=allow_cassette)
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
                log.error("tier_call_failed", tier=tier.value, failure_code=self._failure_code(exc))
        # all live providers failed -> cassette, then (optionally) stub
        hit = self._cassette.get(self._cassette_key(req)) if allow_cassette else None
        if hit is not None:
            log.warning("live_failed_served_from_cassette")
            result = self._replay_result(req, started)
            result.degraded = True
            return result
        if self._s.gateway_replay_fallback_stub and self._s.app_env != "prod":
            log.error("live_failed_fallback_stub", failure_code=self._failure_code(last_error) if last_error else None)
            result = self._stub_result(req, started)
            result.degraded = True
            return result
        raise GatewayError("ALL_PROVIDERS_FAILED", "全部模型通道失败，请稍后重试")

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
                tf = ThoughtFilter()
                p_tok = c_tok = 0
                async for chunk in stream:
                    if chunk.usage:
                        p_tok = chunk.usage.prompt_tokens
                        c_tok = chunk.usage.completion_tokens
                    if chunk.choices and chunk.choices[0].delta.content:
                        delta = tf.feed(chunk.choices[0].delta.content)
                        if delta:
                            parts.append(delta)
                            yield StreamDelta(delta)
                if not p_tok:
                    p_tok = sum(estimate_tokens(message_text(m.content)) for m in req.messages)
                    c_tok = estimate_tokens("".join(parts))
                result = ChatResult(
                    text=strip_thought("".join(parts)),
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
                if self._routing_for_request(req):
                    async for item in self._routed_stream(req):
                        yield item
                    return
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
                    tf = ThoughtFilter()
                    p_tok = c_tok = 0
                    async for chunk in stream:
                        if chunk.usage:
                            p_tok = chunk.usage.prompt_tokens
                            c_tok = chunk.usage.completion_tokens
                        if chunk.choices and chunk.choices[0].delta.content:
                            delta = tf.feed(chunk.choices[0].delta.content)
                            if delta:
                                parts.append(delta)
                                yield StreamDelta(delta)
                    parts.append(tf.flush())
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
                    breaker.record(False)
                    log.error(
                        "stream_tier_failed",
                        tier=tier.value,
                        failure_code=self._failure_code(exc),
                    )
                    if parts:
                        await self._settle_partial_stream(
                            req, tier, "".join(parts), started, reserved_tier
                        )
                        settled = True
                        raise GatewayError(
                            "MODEL_STREAM_INTERRUPTED", "模型输出已中断，请重试"
                        ) from exc
                    streaming_tier = None  # 本档尚未输出, 才允许进入兼容降级链
                    if not self._is_routing_retryable(exc):
                        raise GatewayError(
                            "MODEL_REQUEST_INVALID", "模型请求格式无效，请修改后重试"
                        ) from exc
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
        billing_reference: str | None = None,
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
            route=result.route or self._route_for(req).value,
            platform_billed_cost_cny=(
                result.cost_cny
                if result.credential_source == CredentialSource.platform.value
                else result.platform_billed_cost_cny
            ),
            billing_reference=billing_reference or req.billing_reference or req.request_id,
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
