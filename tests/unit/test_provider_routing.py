from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from eduagents_core.config import Settings
from eduagents_core.gateway import (
    BusinessRoute,
    ChatMessage,
    ChatRequest,
    GatewayError,
    OpenAIGateway,
    StreamDelta,
    Tier,
)
from openai import APIStatusError


class FakeProvider:
    instances: list[FakeProvider] = []
    calls: list[dict] = []
    failures: dict[str, BaseException] = {}

    def __init__(self, *, api_key: str, base_url: str, **_kwargs):
        self.api_key = api_key
        self.base_url = base_url
        self.instances.append(self)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append({"api_key": self.api_key, "base_url": self.base_url, **kwargs})
        failure = self.failures.get(self.api_key)
        if failure is not None:
            raise failure
        if kwargs.get("stream"):
            async def chunks():
                yield SimpleNamespace(
                    usage=None,
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="答案"))],
                )
                yield SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
                    choices=[],
                )

            return chunks()
        text = "{\"ok\":true}" if kwargs.get("response_format") else f"来自 {kwargs['model']}"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
            model="provider-reported-model",
        )

    async def close(self):
        return None


class Guard:
    def __init__(self, *, reject=False):
        self.reject = reject
        self.prechecks: list[str] = []
        self.settles: list[tuple[str, str | None]] = []

    async def precheck(self, _tenant, _user, tier="standard"):
        self.prechecks.append(tier)
        if self.reject:
            from eduagents_core.gateway import BudgetExceeded

            raise BudgetExceeded()
        return None

    async def settle(self, event, reserved_tier=None):
        self.settles.append((event.model, reserved_tier))


def settings(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        gateway_mode="live",
        gateway_max_retries=1,
        gateway_cassette_dir=str(tmp_path / "cassettes"),
        model_routing_enabled=True,
        opencode_zen_api_key="opencode-key",
        text_fallback_api_key="silicon-text-key",
        text_fallback_model="Qwen/text-model",
        vision_fallback_api_key="silicon-vision-key",
        vision_fallback_model="Qwen/VL-model",
        plan_primary_api_key="deepseek-key",
        plan_secondary_api_key="opencode-plan-key",
        plan_tertiary_api_key="silicon-plan-key",
        plan_tertiary_model="Qwen/plan-model",
        **overrides,
    )


def request(*, tier=Tier.standard, content="hello", **kwargs):
    return ChatRequest(
        messages=(ChatMessage(role="user", content=content),),
        tier=tier,
        tenant_id="tenant",
        user_id="user",
        request_id="request-1",
        **kwargs,
    )


def image_request(**kwargs):
    return request(
        content=[
            {"type": "text", "text": "识别图片"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ],
        credential_policy="platform",
        **kwargs,
    )


@pytest.fixture(autouse=True)
def reset_fake():
    FakeProvider.instances = []
    FakeProvider.calls = []
    FakeProvider.failures = {}


def install_fake(monkeypatch):
    import eduagents_core.gateway.openai_gateway as module

    monkeypatch.setattr(module, "AsyncOpenAI", FakeProvider)


async def test_text_success_does_not_call_fallback(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    gateway = OpenAIGateway(settings(tmp_path))
    result = await gateway.chat(request())
    assert result.provider == "opencode"
    assert result.model == "deepseek-v4-flash-free"
    assert [call["api_key"] for call in FakeProvider.calls] == ["opencode-key"]
    assert FakeProvider.calls[0]["model"] == "deepseek-v4-flash-free"


async def test_cheap_text_uses_explicit_opencode_route(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    gateway = OpenAIGateway(settings(tmp_path))

    result = await gateway.chat(request(tier=Tier.cheap, json_mode=True))

    assert result.provider == "opencode"
    assert result.model == "deepseek-v4-flash-free"
    assert [call["api_key"] for call in FakeProvider.calls] == ["opencode-key"]
    assert FakeProvider.calls[0]["response_format"] == {"type": "json_object"}


async def test_text_429_falls_back_to_siliconflow(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    FakeProvider.failures["opencode-key"] = GatewayError("UPSTREAM_RATE_LIMITED", "hidden")
    gateway = OpenAIGateway(settings(tmp_path))
    result = await gateway.chat(request())
    assert result.provider == "siliconflow"
    assert result.model == "Qwen/text-model"
    assert [call["model"] for call in FakeProvider.calls] == [
        "deepseek-v4-flash-free",
        "Qwen/text-model",
    ]


async def test_bad_request_does_not_fallback_or_expose_upstream(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    FakeProvider.failures["opencode-key"] = APIStatusError(
        message="secret upstream body",
        response=httpx.Response(400, request=httpx.Request("POST", "https://private")),
        body=None,
    )
    gateway = OpenAIGateway(settings(tmp_path))
    with pytest.raises(GatewayError) as exc:
        await gateway.chat(request())
    assert exc.value.code == "MODEL_REQUEST_INVALID"
    assert "secret" not in str(exc.value)
    assert len(FakeProvider.calls) == 1


async def test_vision_fallback_uses_its_own_model_and_two_stage_json(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    FakeProvider.failures["opencode-key"] = GatewayError("UPSTREAM_UNAVAILABLE", "hidden")
    events = []

    async def sink(event):
        events.append(event)

    gateway = OpenAIGateway(settings(tmp_path), usage_sink=sink)
    result = await gateway.chat(image_request(json_mode=True))
    assert result.text == '{"ok":true}'
    assert [call["model"] for call in FakeProvider.calls] == [
        "mimo-v2.5-free",
        "Qwen/VL-model",
        "deepseek-v4-flash-free",
        "Qwen/text-model",
    ]
    assert "response_format" not in FakeProvider.calls[1]
    assert FakeProvider.calls[2]["response_format"] == {"type": "json_object"}
    assert all(
        not any(isinstance(part, dict) and "image_url" in part for part in call["messages"])
        for call in FakeProvider.calls[2:]
    )
    assert [event.route for event in events] == ["vision", "text"]


async def test_plan_chain_paid_free_paid_and_actual_candidate_usage(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    FakeProvider.failures["deepseek-key"] = GatewayError("UPSTREAM_UNAVAILABLE", "hidden")
    guard = Guard()
    gateway = OpenAIGateway(settings(tmp_path), budget_guard=guard)
    result = await gateway.chat(request(tier=Tier.plan))
    assert result.provider == "opencode"
    assert result.model == "deepseek-v4-flash-free"
    assert [call["api_key"] for call in FakeProvider.calls] == ["deepseek-key", "opencode-plan-key"]
    assert guard.prechecks == ["plan"]
    assert guard.settles == [("", "plan"), ("deepseek-v4-flash-free", None)]


async def test_free_candidate_does_not_need_balance_but_paid_fallback_does(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    FakeProvider.failures["opencode-key"] = GatewayError("UPSTREAM_UNAVAILABLE", "hidden")
    guard = Guard(reject=True)
    gateway = OpenAIGateway(settings(tmp_path), budget_guard=guard)
    with pytest.raises(Exception) as exc:
        await gateway.chat(request())
    assert getattr(exc.value, "code", None) == "BUDGET_EXCEEDED"
    assert [call["api_key"] for call in FakeProvider.calls] == ["opencode-key"]
    assert guard.prechecks == ["standard"]


async def test_candidate_breakers_and_clients_are_independent(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    FakeProvider.failures["opencode-key"] = GatewayError("UPSTREAM_UNAVAILABLE", "hidden")
    gateway = OpenAIGateway(settings(tmp_path))
    await gateway.chat(request())
    assert gateway.breaker_state(Tier.standard) == "closed"
    assert gateway.routing_status()[0]["id"] == "opencode-text"
    assert gateway.routing_status()[0]["last_failure_code"] == "UPSTREAM_UNAVAILABLE"
    assert len(gateway._candidate_clients) == 2
    assert gateway._candidate_clients["opencode-text"] is not gateway._candidate_clients["siliconflow-text"]


async def test_stream_falls_back_before_first_token(monkeypatch, tmp_path):
    install_fake(monkeypatch)
    FakeProvider.failures["opencode-key"] = GatewayError("UPSTREAM_TIMEOUT", "hidden")
    gateway = OpenAIGateway(settings(tmp_path))
    items = [item async for item in gateway.stream_chat(request())]
    assert [call["model"] for call in FakeProvider.calls] == [
        "deepseek-v4-flash-free",
        "Qwen/text-model",
    ]
    assert isinstance(items[0], StreamDelta)
    assert items[-1].model == "Qwen/text-model"


async def test_stream_does_not_switch_after_first_token(monkeypatch, tmp_path):
    install_fake(monkeypatch)

    class BrokenProvider(FakeProvider):
        async def create(self, **kwargs):
            if kwargs.get("stream") and self.api_key == "opencode-key":
                self.calls.append({"api_key": self.api_key, "base_url": self.base_url, **kwargs})
                async def chunks():
                    yield SimpleNamespace(
                        usage=None,
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="首段"))],
                    )
                    raise GatewayError("UPSTREAM_UNAVAILABLE", "hidden")

                return chunks()
            return await super().create(**kwargs)

    import eduagents_core.gateway.openai_gateway as module

    monkeypatch.setattr(module, "AsyncOpenAI", BrokenProvider)
    gateway = OpenAIGateway(settings(tmp_path))
    with pytest.raises(GatewayError) as exc:
        async for _ in gateway.stream_chat(request()):
            pass
    assert exc.value.code == "MODEL_STREAM_INTERRUPTED"
    assert [call["model"] for call in FakeProvider.calls] == ["deepseek-v4-flash-free"]


def test_route_candidate_capabilities_are_explicit(tmp_path):
    from eduagents_core.gateway import configured_candidates

    vision = configured_candidates(settings(tmp_path), BusinessRoute.vision)
    assert vision[0].supports_vision is True
    assert vision[0].supports_json is False
    assert vision[0].api_key not in repr(vision[0])


def test_production_routing_rejects_missing_candidate_chains():
    with pytest.raises(ValueError, match="routes: text, vision, plan"):
        Settings(
            _env_file=None,
            app_env="prod",
            model_routing_enabled=True,
            opencode_zen_api_key="",
            text_fallback_api_key="",
            text_fallback_model="",
            vision_fallback_api_key="",
            vision_fallback_model="",
            plan_primary_api_key="",
            plan_secondary_api_key="",
            plan_tertiary_api_key="",
            plan_tertiary_model="",
            cheap_api_key="",
            cheap_model="",
            standard_api_key="",
            vision_model="",
            plan_api_key="",
            plan_model="",
        )


def test_production_routing_accepts_documented_legacy_fallbacks():
    configured = Settings(
        _env_file=None,
        app_env="prod",
        model_routing_enabled=True,
        opencode_zen_api_key="public",
        cheap_api_key="siliconflow-key",
        cheap_model="Qwen/text-model",
        standard_api_key="siliconflow-key",
        vision_model="Qwen/VL-model",
        plan_api_key="deepseek-key",
        plan_model="deepseek-v4-flash",
    )

    assert configured.routing_text_enabled is True
    assert configured.routing_vision_enabled is True
    assert configured.routing_plan_enabled is True
