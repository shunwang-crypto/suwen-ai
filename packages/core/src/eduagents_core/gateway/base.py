from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol


class Tier(StrEnum):
    cheap = "cheap"
    standard = "standard"
    strong = "strong"
    plan = "plan"


class CredentialSource(StrEnum):
    platform = "platform"
    user = "user"


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class BudgetExceeded(GatewayError):
    """用户日预算 ≥100%: API 层转 429 + Retry-After, SSE 场景发 event:error。

    retry_after_seconds: 距下一个 Asia/Shanghai 日界的秒数 (Retry-After 头的值)。
    """

    def __init__(
        self,
        message: str = "今日 AI 用量已达上限",
        retry_after_seconds: int | None = None,
    ):
        super().__init__("BUDGET_EXCEEDED", message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    # OpenAI-compatible multimodal messages use a list of text/image_url parts.
    # Existing callers remain plain strings; only chat attachments opt into parts.
    content: str | list[dict[str, Any]]
    tool_call_id: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    reasoning_content: str | None = None

    def to_wire(self) -> dict[str, Any]:
        value: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            value["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            value["tool_calls"] = list(self.tool_calls)
        if self.reasoning_content is not None:
            value["reasoning_content"] = self.reasoning_content
        return value


def message_text(content: str | list[dict[str, Any]]) -> str:
    """Return only textual parts for logging, token estimates and dev stubs."""
    if isinstance(content, str):
        return content
    return "\n".join(
        str(part.get("text", ""))
        for part in content
        if part.get("type") == "text" and part.get("text")
    )


@dataclass(frozen=True)
class ChatRequest:
    messages: tuple[ChatMessage, ...]
    tier: Tier = Tier.standard
    temperature: float = 0.7
    max_tokens: int | None = None
    json_mode: bool = False
    feature: str = "chat"
    tenant_id: str = ""
    user_id: str = ""
    request_id: str = ""
    # Vision requests use the same configured OpenAI-compatible endpoint/key but
    # explicitly select the configured VL model. None keeps the tier model.
    model_override: str | None = None
    # A text-model fallback cannot consume image parts, so multimodal requests
    # must stay on the requested provider tier.
    allow_tier_fallback: bool = True
    # Images, embeddings and other platform-only work must opt out of BYOK.
    # The API/worker sets this explicitly for multimodal requests.
    credential_policy: Literal["text", "platform"] = "text"
    tools: tuple[dict[str, Any], ...] = ()
    tool_choice: Literal["auto", "required", "none"] | None = None
    # Internal per-model-call identity, separate from the public request ID.
    billing_reference: str = ""
    # Optional decoder constraint for local models that support structured outputs.
    response_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedCredential:
    """Request-scoped credential material returned by an injected resolver.

    The API key is intentionally not part of ChatRequest/state/tracing.  It is
    held only while the gateway is making the current provider request.
    """

    source: CredentialSource
    provider: str
    api_key: str
    base_url: str
    model: str
    fallback_model: str = ""
    fallback_to_platform: bool = False
    credential_id: str = ""
    key_version: int = 0


@dataclass(frozen=True)
class BudgetReservation:
    id: str
    date: str
    tier: str


@dataclass
class ChatResult:
    text: str
    model: str
    tier: str
    prompt_tokens: int
    completion_tokens: int
    cost_cny: float
    latency_ms: int
    mode: str  # live | record | replay | stub
    degraded: bool = False
    credential_source: str = CredentialSource.platform.value
    provider: str = ""
    route: str = ""
    platform_billed_cost_cny: float = 0.0
    # Internal hand-off when a BYOK request explicitly falls back to platform
    # after the initial gateway precheck was intentionally skipped.
    reserved_tier: BudgetReservation | str | None = None
    # Provider completion reason when the compatible API exposes it.  This is
    # diagnostic metadata only; response bodies are never persisted here.
    finish_reason: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    reasoning_content: str | None = None


@dataclass
class StreamDelta:
    text: str


@dataclass
class UsageEvent:
    request_id: str
    tenant_id: str
    user_id: str
    feature: str
    model: str
    tier: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_cny: float
    latency_ms: int
    mode: str
    credential_source: str = CredentialSource.platform.value
    provider: str = ""
    route: str = ""
    platform_billed_cost_cny: float = 0.0
    # Internal idempotency reference for multi-stage requests.  It is not a
    # user-facing request ID and never contains credentials or request text.
    billing_reference: str = ""

    settlement_id: str = ""


UsageSink = Callable[[UsageEvent], Awaitable[bool | None]]
CredentialResolver = Callable[[ChatRequest], Awaitable[ResolvedCredential | None]]


class BudgetGuard(Protocol):
    """用户日预算 + 全局日成本熔断 — 挂在网关入口, LLM 调用唯一 choke point。

    precheck 返回值语义: None=正常; "cheap"=软限降档 (网关将请求降为 cheap 档并压
    max_tokens); 超限抛 BudgetExceeded。契约 (P1-12 原子预扣):
    - precheck 正常返回 (不抛) 即视为已按 tier 档位原子预扣估算额;
    - settle 在调用完成后按实际成本与预扣差额结算 (reserved_tier=precheck 时的档位);
    - 调用失败/无实际成本时网关仍须以 reserved_tier 调 settle 返还预扣。
    stub/replay 零成本不预检不计数。
    """

    async def precheck(
        self, tenant_id: str, user_id: str, tier: str = "standard"
    ) -> str | None: ...

    async def settle(self, event: UsageEvent, reserved_tier: BudgetReservation | str | None = None) -> None: ...


class ModelGateway(Protocol):
    """Every LLM call in the platform goes through this. Bypassing it breaks
    metering, budget, degradation and record/replay — forbidden by §14."""

    async def chat(self, req: ChatRequest) -> ChatResult: ...

    def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamDelta | ChatResult]:
        """Yields StreamDelta items, then exactly one final ChatResult."""
        ...
