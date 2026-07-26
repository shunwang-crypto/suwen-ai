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
        {"text": "录制的真实回答", "model": "deepseek-chat", "prompt_tokens": 12, "completion_tokens": 34},
    )
    result = await gw.chat(req("固定问题"))
    assert result.text == "录制的真实回答"
    assert result.mode == "replay"
    assert result.prompt_tokens == 12


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
