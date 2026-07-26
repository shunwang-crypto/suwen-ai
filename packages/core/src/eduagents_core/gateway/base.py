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
    """用户日预算强制 — 挂在网关入口, LLM 调用唯一 choke point (ACTION_PLAN 技术决策表)。

    precheck 返回值语义: None=正常; "cheap"=软限降档 (网关将请求降为 cheap 档并压
    max_tokens); 预算 ≥100% 抛 BudgetExceeded。settle 在调用完成后按实际成本累加;
    网关只对 mode=live 的结果调用 settle, stub/replay 零成本不预检不计数。
    """

    async def precheck(self, tenant_id: str, user_id: str) -> str | None: ...

    async def settle(self, event: UsageEvent) -> None: ...


class ModelGateway(Protocol):
    """Every LLM call in the platform goes through this. Bypassing it breaks
    metering, budget, degradation and record/replay — forbidden by §14."""

    async def chat(self, req: ChatRequest) -> ChatResult: ...

    def stream_chat(
        self, req: ChatRequest
    ) -> AsyncIterator[StreamDelta | ChatResult]:
        """Yields StreamDelta items, then exactly one final ChatResult."""
        ...
