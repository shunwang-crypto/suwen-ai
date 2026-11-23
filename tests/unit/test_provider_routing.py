from __future__ import annotations

from types import SimpleNamespace

import pytest
from eduagents_core.config import Settings
from eduagents_core.gateway import BusinessRoute, ChatMessage, ChatRequest, OpenAIGateway, Tier
from eduagents_core.gateway.routing import configured_candidates


class FakeProvider:
    calls: list[dict] = []

    def __init__(self, *, api_key: str, base_url: str, **_kwargs):
        self.api_key = api_key
        self.base_url = base_url
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append({"api_key": self.api_key, "base_url": self.base_url, **kwargs})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            model="muse-spark-1.2-contributor",
        )

    async def close(self):
        return None


def settings(tmp_path):
    return Settings(
        _env_file=None,
        gateway_mode="live",
        gateway_cassette_dir=str(tmp_path / "cassettes"),
        model_routing_enabled=True,
        trenddao_base_url="https://trenddao.com/v1",
        trenddao_api_key="trenddao-key",
        trenddao_text_model="muse-spark-1.2-contributor",
        trenddao_vision_model="muse-spark-1.2-contributor",
    )


def request(*, tier=Tier.standard, json_mode=False):
    return ChatRequest(
        messages=(ChatMessage(role="user", content="hello"),),
        tier=tier,
        json_mode=json_mode,
        tenant_id="tenant",
        user_id="user",
        request_id="request-1",
    )


@pytest.fixture(autouse=True)
def reset_fake():
    FakeProvider.calls = []


async def test_every_route_uses_only_trenddao(monkeypatch, tmp_path):
    import eduagents_core.gateway.openai_gateway as module

    monkeypatch.setattr(module, "AsyncOpenAI", FakeProvider)
    gateway = OpenAIGateway(settings(tmp_path))
    for tier in (Tier.cheap, Tier.standard, Tier.plan):
        result = await gateway.chat(request(tier=tier, json_mode=True))
        assert result.provider == "trenddao"
        assert result.model == "muse-spark-1.2-contributor"
    assert len(FakeProvider.calls) == 3
    assert {call["model"] for call in FakeProvider.calls} == {"muse-spark-1.2-contributor"}
    assert all(call["response_format"] == {"type": "json_object"} for call in FakeProvider.calls)


def test_route_candidates_have_no_fallbacks(tmp_path):
    candidates = [candidate for route in BusinessRoute for candidate in configured_candidates(settings(tmp_path), route)]
    assert [candidate.id for candidate in candidates] == [
        "trenddao-text",
        "trenddao-vision",
        "trenddao-plan",
    ]
    assert all(candidate.provider == "trenddao" for candidate in candidates)


def test_production_requires_the_single_provider():
    with pytest.raises(ValueError, match="routes: text, vision, plan"):
        Settings(_env_file=None, app_env="prod", model_routing_enabled=True, trenddao_api_key="")


async def test_visual_json_activities_meter_transcription_and_structuring_independently(monkeypatch, tmp_path):
    from dataclasses import replace

    import eduagents_core.gateway.openai_gateway as module

    monkeypatch.setattr(module, "AsyncOpenAI", FakeProvider)
    events = []

    async def sink(event):
        events.append(event)

    gateway = OpenAIGateway(settings(tmp_path), usage_sink=sink)
    visual_request = replace(
        request(json_mode=True),
        credential_policy="platform",
        messages=(ChatMessage("user", [
            {"type": "text", "text": "识别并结构化题目"},
            {"type": "image_url", "image_url": {"url": "https://test.invalid/question.png"}},
        ]),),
    )
    for activity in ("activity-1", "activity-2"):
        await gateway.chat(replace(visual_request, billing_reference=activity))
    assert len(FakeProvider.calls) == len(events) == 4
    assert len({event.billing_reference for event in events}) == 4
    assert all(event.request_id == "request-1" for event in events)
