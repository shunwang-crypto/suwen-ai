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


class ModelGateway(Protocol):
    """Every LLM call in the platform goes through this. Bypassing it breaks
    metering, budget, degradation and record/replay — forbidden by §14."""

    async def chat(self, req: ChatRequest) -> ChatResult: ...

    def stream_chat(
        self, req: ChatRequest
    ) -> AsyncIterator[StreamDelta | ChatResult]:
        """Yields StreamDelta items, then exactly one final ChatResult."""
        ...
