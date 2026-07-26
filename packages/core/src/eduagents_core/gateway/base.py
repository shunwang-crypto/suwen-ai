from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


class Tier(StrEnum):
    cheap = "cheap"
    standard = "standard"
    strong = "strong"


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
    role: Literal["system", "user", "assistant"]
    content: str


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


UsageSink = Callable[[UsageEvent], Awaitable[None]]


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

    async def settle(
        self, event: UsageEvent, reserved_tier: str | None = None
    ) -> None: ...


class ModelGateway(Protocol):
    """Every LLM call in the platform goes through this. Bypassing it breaks
    metering, budget, degradation and record/replay — forbidden by §14."""

    async def chat(self, req: ChatRequest) -> ChatResult: ...

    def stream_chat(
        self, req: ChatRequest
    ) -> AsyncIterator[StreamDelta | ChatResult]:
        """Yields StreamDelta items, then exactly one final ChatResult."""
        ...
