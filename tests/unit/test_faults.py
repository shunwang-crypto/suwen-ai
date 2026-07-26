"""M5-D2 故障注入的语义锁: 三个旋钮各自的行为 + "stub/replay 永不受影响" 这条铁律。"""

import random

import pytest
from eduagents_core.gateway import FaultInjector, FaultState, get_fault_injector
from eduagents_core.gateway.circuit import BreakerState, CircuitBreaker
from openai import APIConnectionError


def test_default_state_is_inert():
    injector = FaultInjector()
    assert injector.state == FaultState()
    assert injector.snapshot()["active"] is False


async def test_provider_down_raises_retryable_error():
    """必须是 openai 的 APIConnectionError —— 它在网关的 _RETRYABLE 里,
    只有这样才会走完 重试 → 熔断 → 降级 的整条链, 演示才有东西可看。"""
    injector = FaultInjector(FaultState(provider_down=True))
    with pytest.raises(APIConnectionError) as excinfo:
        await injector.apply("standard")
    assert "fault-injection:provider_down" in str(excinfo.value)


async def test_error_rate_is_probabilistic():
    always = FaultInjector(FaultState(error_rate=1.0))
    with pytest.raises(APIConnectionError):
        await always.apply("cheap")

    never = FaultInjector(FaultState(error_rate=0.0))
    await never.apply("cheap")  # 不抛即通过

    # 固定种子下命中率贴近设定值 (0.5 ± 容差), 不是恒真也不是恒假
    half = FaultInjector(FaultState(error_rate=0.5), rng=random.Random(1234))
    failures = 0
    for _ in range(200):
        try:
            await half.apply("cheap")
        except APIConnectionError:
            failures += 1
    assert 70 < failures < 130, failures


async def test_latency_applies_before_error(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("eduagents_core.gateway.faults.asyncio.sleep", fake_sleep)
    injector = FaultInjector(FaultState(provider_down=True, latency_ms=250))
    with pytest.raises(APIConnectionError):
        await injector.apply("strong")
    assert slept == [0.25], "延迟必须在抛错之前生效 (先卡住再失败, 贴近真实劣化)"


def test_update_is_partial_and_clamped():
    injector = FaultInjector()
    injector.update(latency_ms=999_999, error_rate=5.0)
    assert injector.state.latency_ms == 30_000  # MAX_LATENCY_MS
    assert injector.state.error_rate == 1.0
    injector.update(provider_down=True)  # 只改一项, 其余保持
    assert injector.state.latency_ms == 30_000 and injector.state.error_rate == 1.0
    injector.update(error_rate=-1.0)
    assert injector.state.error_rate == 0.0
    assert injector.reset() == FaultState()


def test_process_singleton_shared():
    """admin 端点与网关必须看到同一个开关对象, 否则面板上开了网关不知道。"""
    assert get_fault_injector() is get_fault_injector()


def test_breaker_trip_and_reset():
    breaker = CircuitBreaker(open_seconds=1000)
    assert breaker.allow() is True
    breaker.trip()
    assert breaker.state is BreakerState.open
    assert breaker.allow() is False
    breaker.reset()
    assert breaker.state is BreakerState.closed
    assert breaker.allow() is True


async def test_stub_and_replay_never_touch_faults():
    """降级链最后一环 (stub) 必须永远可用: 全局开关拉满也不影响 stub 模式。"""
    from eduagents_core.config import get_settings
    from eduagents_core.gateway import ChatMessage, ChatRequest, OpenAIGateway, Tier

    injector = FaultInjector(FaultState(provider_down=True, error_rate=1.0))
    settings = get_settings().model_copy(update={"gateway_mode": "stub"})
    gateway = OpenAIGateway(settings, faults=injector)
    result = await gateway.chat(
        ChatRequest(messages=(ChatMessage(role="user", content="hi"),), tier=Tier.cheap)
    )
    assert result.mode == "stub" and result.text
