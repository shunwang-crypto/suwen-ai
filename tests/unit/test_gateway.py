from types import SimpleNamespace

import pytest
from eduagents_core.config import Settings
from eduagents_core.gateway import (
    Cassette,
    ChatMessage,
    ChatRequest,
    ChatResult,
    GatewayError,
    OpenAIGateway,
    StreamDelta,
    Tier,
    UsageEvent,
)


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        gateway_cassette_dir=str(tmp_path / "cassettes"),
        gateway_stream_delay_ms=0,
        **overrides,
    )


def req(text: str = "什么是泰勒公式?", tier: Tier = Tier.standard) -> ChatRequest:
    return ChatRequest(
        messages=(ChatMessage(role="user", content=text),),
        tier=tier,
        tenant_id="11111111-1111-1111-1111-111111111111",
        user_id="11111111-1111-1111-1111-111111111111",
        request_id="req-test",
    )


def test_thinking_switch_is_sent_only_when_explicitly_configured(tmp_path):
    settings = make_settings(
        tmp_path,
        cheap_enable_thinking=False,
        standard_enable_thinking=False,
        strong_enable_thinking=True,
        plan_enable_thinking=None,
    )
    gw = OpenAIGateway(settings)

    cheap_kwargs = gw._completion_kwargs(req(tier=Tier.cheap), settings.tier("cheap"))
    strong_kwargs = gw._completion_kwargs(req(tier=Tier.strong), settings.tier("strong"))
    plan_kwargs = gw._completion_kwargs(req(tier=Tier.plan), settings.tier("plan"))

    assert cheap_kwargs["extra_body"] == {"enable_thinking": False}
    assert strong_kwargs["extra_body"] == {"enable_thinking": True}
    assert "extra_body" not in plan_kwargs


def test_cassette_key_distinguishes_thinking_mode(tmp_path):
    plain = OpenAIGateway(make_settings(tmp_path, standard_enable_thinking=False))
    thinking = OpenAIGateway(make_settings(tmp_path, standard_enable_thinking=True))

    assert plain._cassette_key(req()) != thinking._cassette_key(req())


async def test_stub_mode_is_deterministic_and_banner_marked(tmp_path):
    gw = OpenAIGateway(make_settings(tmp_path, gateway_mode="stub"))
    r1 = await gw.chat(req())
    r2 = await gw.chat(req())
    assert r1.text == r2.text  # 同请求同输出, 可复现
    assert "非真实模型输出" in r1.text  # 永远无法冒充真实回答
    assert r1.mode == "stub"
    assert r1.cost_cny == 0.0
    assert r1.prompt_tokens > 0


async def test_stub_json_mode_returns_valid_json(tmp_path):
    import json

    gw = OpenAIGateway(make_settings(tmp_path, gateway_mode="stub"))
    result = await gw.chat(
        ChatRequest(
            messages=(ChatMessage(role="user", content="出一道题"),),
            tier=Tier.cheap,
            json_mode=True,
        )
    )
    parsed = json.loads(result.text)
    assert parsed["stub"] is True


async def test_replay_hits_cassette(tmp_path):
    settings = make_settings(tmp_path, gateway_mode="replay")
    gw = OpenAIGateway(settings)
    key = gw._cassette_key(req("固定问题"))
    Cassette(settings.gateway_cassette_dir).put(
        key,
        {
            "text": "录制的真实回答",
            "model": "deepseek-chat",
            "prompt_tokens": 12,
            "completion_tokens": 34,
            "finish_reason": "length",
        },
    )
    result = await gw.chat(req("固定问题"))
    assert result.text == "录制的真实回答"
    assert result.mode == "replay"
    assert result.prompt_tokens == 12
    assert result.finish_reason == "length"


async def test_replay_miss_falls_back_to_stub_when_allowed(tmp_path):
    gw = OpenAIGateway(
        make_settings(tmp_path, gateway_mode="replay", gateway_replay_fallback_stub=True)
    )
    result = await gw.chat(req("没录制过的问题"))
    assert result.mode == "stub"


async def test_replay_miss_raises_when_stub_disallowed(tmp_path):
    gw = OpenAIGateway(
        make_settings(tmp_path, gateway_mode="replay", gateway_replay_fallback_stub=False)
    )
    with pytest.raises(GatewayError) as exc_info:
        await gw.chat(req("没录制过的问题"))
    assert exc_info.value.code == "CASSETTE_MISS"


async def test_live_chat_preserves_provider_finish_reason(tmp_path, monkeypatch):
    settings = make_settings(
        tmp_path,
        gateway_mode="live",
        standard_api_key="standard-test-key",
    )
    gw = OpenAIGateway(settings)

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    return SimpleNamespace(
                        model="fake-standard",
                        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
                        choices=[
                            SimpleNamespace(
                                finish_reason="length",
                                message=SimpleNamespace(content='{"questions": []}'),
                            )
                        ],
                    )

    monkeypatch.setattr(gw, "_client", lambda tier: FakeClient())
    result = await gw._call_tier(req(), Tier.standard)

    assert result.text == '{"questions": []}'
    assert result.finish_reason == "length"


async def test_stream_yields_deltas_then_final_result(tmp_path):
    gw = OpenAIGateway(make_settings(tmp_path, gateway_mode="stub"))
    deltas: list[str] = []
    final = None
    async for item in gw.stream_chat(req()):
        if isinstance(item, StreamDelta):
            deltas.append(item.text)
        else:
            final = item
    assert final is not None and isinstance(final, ChatResult)
    assert "".join(deltas) == final.text
    assert len(deltas) > 1  # 确实是分块流式


async def test_usage_sink_receives_event(tmp_path):
    events: list[UsageEvent] = []

    async def sink(e: UsageEvent) -> None:
        events.append(e)

    gw = OpenAIGateway(make_settings(tmp_path, gateway_mode="stub"), usage_sink=sink)
    await gw.chat(req())
    assert len(events) == 1
    e = events[0]
    assert e.feature == "chat"
    assert e.request_id == "req-test"
    assert e.tier == "standard"
    assert e.prompt_tokens > 0


async def test_plan_config_cost_budget_settlement_and_usage_tier(tmp_path, monkeypatch):
    events: list[UsageEvent] = []
    settlements: list[tuple[UsageEvent, str | None]] = []

    class RecordingGuard:
        def __init__(self):
            self.prechecks: list[str] = []

        async def precheck(self, tenant_id: str, user_id: str, tier: str = "standard"):
            self.prechecks.append(tier)
            return None

        async def settle(self, event: UsageEvent, reserved_tier: str | None = None):
            settlements.append((event, reserved_tier))

    async def sink(event: UsageEvent) -> None:
        events.append(event)

    settings = make_settings(
        tmp_path,
        gateway_mode="live",
        plan_api_key="plan-test-key",
        plan_model="plan-test-model",
        plan_input_price_per_m=1.0,
        plan_output_price_per_m=2.0,
    )
    guard = RecordingGuard()
    gw = OpenAIGateway(settings, usage_sink=sink, budget_guard=guard)

    async def fake_call(request, tier):
        cfg = settings.tier(tier.value)
        return ChatResult(
            text="计划回答",
            model=cfg.model,
            tier=tier.value,
            prompt_tokens=1000,
            completion_tokens=2000,
            cost_cny=gw._cost(cfg, 1000, 2000),
            latency_ms=1,
            mode="live",
        )

    monkeypatch.setattr(gw, "_call_tier", fake_call)
    result = await gw.chat(req("制定学习计划", tier=Tier.plan))

    assert settings.tier("plan").model == "plan-test-model"
    assert settings.tier("plan").input_price_per_m == 1.0
    assert settings.tier("plan").output_price_per_m == 2.0
    assert result.tier == Tier.plan.value
    assert result.cost_cny == pytest.approx(0.005)
    assert guard.prechecks == [Tier.plan.value]
    assert len(settlements) == 1
    settled_event, reserved_tier = settlements[0]
    assert reserved_tier == Tier.plan.value
    assert settled_event.tier == Tier.plan.value
    assert settled_event.cost_cny == pytest.approx(0.005)
    assert len(events) == 1
    assert events[0].tier == Tier.plan.value


async def test_live_without_keys_degrades_to_stub_in_dev(tmp_path):
    # live 模式但没有任何 API key: 链路全失败 -> 开发环境回退 stub (M1-D3 的最底层)
    settings = make_settings(
        tmp_path,
        gateway_mode="live",
        cheap_api_key="",
        standard_api_key="",
        strong_api_key="",
        app_env="dev",
    )
    gw = OpenAIGateway(settings)
    result = await gw.chat(req())
    assert result.mode == "stub"
    assert result.degraded is True


async def test_live_without_keys_raises_in_prod(tmp_path):
    settings = make_settings(
        tmp_path,
        gateway_mode="live",
        cheap_api_key="",
        standard_api_key="",
        strong_api_key="",
        app_env="prod",
        gateway_replay_fallback_stub=False,
    )
    gw = OpenAIGateway(settings)
    with pytest.raises(GatewayError):
        await gw.chat(req())
