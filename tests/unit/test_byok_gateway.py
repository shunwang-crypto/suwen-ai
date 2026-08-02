from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from eduagents_core.config import Settings
from eduagents_core.gateway import (
    ChatMessage,
    ChatRequest,
    CredentialSource,
    GatewayError,
    OpenAIGateway,
    ResolvedCredential,
    Tier,
)


class _Guard:
    def __init__(self):
        self.prechecks: list[str] = []
        self.settles = 0

    async def precheck(self, tenant_id: str, user_id: str, tier: str = "standard"):
        self.prechecks.append(tier)
        return None

    async def settle(self, event, reserved_tier=None):
        self.settles += 1


class _Client:
    calls: list[str] = []
    fail_user = False

    def __init__(self, *, api_key: str, **_kwargs):
        self.api_key = api_key
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **_kwargs):
        self.calls.append(self.api_key)
        if self.fail_user and self.api_key == "user-key":
            import httpx
            from openai import APIConnectionError

            raise APIConnectionError(request=httpx.Request("POST", "https://fake"))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="来自用户 Key"))],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3),
            model="deepseek-chat",
        )

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_user_text_uses_request_scoped_key_and_skips_platform_budget(monkeypatch, tmp_path):
    from eduagents_core.gateway import openai_gateway

    _Client.calls = []
    monkeypatch.setattr(openai_gateway, "AsyncOpenAI", _Client)
    settings = Settings(_env_file=None, gateway_mode="live", gateway_cassette_dir=str(tmp_path))
    guard = _Guard()
    events = []

    async def resolver(_req):
        return ResolvedCredential(
            source=CredentialSource.user,
            provider="deepseek",
            api_key="user-key",
            base_url="https://fake-user/v1",
            model="deepseek-chat",
        )

    async def sink(event):
        events.append(event)

    gateway = OpenAIGateway(
        settings, usage_sink=sink, budget_guard=guard, credential_resolver=resolver
    )
    result = await gateway.chat(
        ChatRequest(
            messages=(ChatMessage(role="user", content="hello"),),
            tier=Tier.standard,
            tenant_id="tenant",
            user_id="user",
        )
    )
    assert result.credential_source == "user"
    assert result.provider == "deepseek"
    assert result.cost_cny == 0
    assert _Client.calls == ["user-key"]
    assert guard.prechecks == []
    assert guard.settles == 1
    assert events[0].credential_source == "user"
    assert events[0].platform_billed_cost_cny == 0


@pytest.mark.asyncio
async def test_byok_failure_without_opt_in_never_touches_platform(monkeypatch, tmp_path):
    from eduagents_core.gateway import openai_gateway

    _Client.calls = []
    _Client.fail_user = True
    monkeypatch.setattr(openai_gateway, "AsyncOpenAI", _Client)
    settings = Settings(_env_file=None, gateway_mode="live", gateway_cassette_dir=str(tmp_path), gateway_max_retries=1)

    async def resolver(_req):
        return ResolvedCredential(
            source=CredentialSource.user,
            provider="deepseek",
            api_key="user-key",
            base_url="https://fake-user/v1",
            model="deepseek-chat",
            fallback_to_platform=False,
        )

    gateway = OpenAIGateway(settings, credential_resolver=resolver)
    with pytest.raises(GatewayError) as exc:
        await gateway.chat(ChatRequest(messages=(ChatMessage(role="user", content="hello"),)))
    assert exc.value.code == "BYOK_PROVIDER_UNAVAILABLE"
    assert _Client.calls == ["user-key"]
    _Client.fail_user = False


@pytest.mark.asyncio
async def test_byok_failure_with_explicit_opt_in_uses_platform_and_budget(monkeypatch, tmp_path):
    from eduagents_core.gateway import openai_gateway

    _Client.calls = []
    _Client.fail_user = True
    monkeypatch.setattr(openai_gateway, "AsyncOpenAI", _Client)
    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        gateway_cassette_dir=str(tmp_path),
        gateway_max_retries=1,
        standard_api_key="platform-key",
        standard_base_url="https://fake-platform/v1",
    )
    guard = _Guard()

    async def resolver(_req):
        return ResolvedCredential(
            source=CredentialSource.user,
            provider="deepseek",
            api_key="user-key",
            base_url="https://fake-user/v1",
            model="deepseek-chat",
            fallback_to_platform=True,
        )

    gateway = OpenAIGateway(settings, budget_guard=guard, credential_resolver=resolver)
    result = await gateway.chat(
        ChatRequest(
            messages=(ChatMessage(role="user", content="hello"),),
            tenant_id="tenant",
            user_id="user",
        )
    )
    assert result.credential_source == "platform"
    assert result.platform_billed_cost_cny == result.cost_cny
    assert _Client.calls == ["user-key", "platform-key"]
    assert guard.prechecks == ["standard"]
    assert guard.settles == 1
    _Client.fail_user = False


@pytest.mark.asyncio
async def test_strong_stream_downgrades_to_users_normal_model(monkeypatch, tmp_path):
    import httpx
    from eduagents_core.gateway import openai_gateway
    from openai import APIConnectionError

    class StreamClient:
        calls = []

        def __init__(self, *, api_key: str, **_kwargs):
            self.api_key = api_key
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            self.calls.append((self.api_key, kwargs["model"], kwargs.get("stream", False)))
            if kwargs["model"] == "deepseek-reasoner":
                raise APIConnectionError(
                    request=httpx.Request("POST", "https://fake-user/v1/chat/completions")
                )

            async def chunks():
                yield SimpleNamespace(
                    usage=None,
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="用户流式回答"))],
                )
                yield SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3),
                    choices=[],
                )

            return chunks()

        async def close(self):
            return None

    monkeypatch.setattr(openai_gateway, "AsyncOpenAI", StreamClient)
    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        gateway_cassette_dir=str(tmp_path),
        gateway_max_retries=1,
    )

    async def resolver(_req):
        return ResolvedCredential(
            source=CredentialSource.user,
            provider="deepseek",
            api_key="user-key",
            base_url="https://fake-user/v1",
            model="deepseek-reasoner",
            fallback_model="deepseek-chat",
        )

    gateway = OpenAIGateway(settings, credential_resolver=resolver)
    items = [
        item
        async for item in gateway.stream_chat(
            ChatRequest(
                messages=(ChatMessage(role="user", content="hello"),),
                tier=Tier.strong,
                tenant_id="tenant",
                user_id="user",
            )
        )
    ]
    assert items[0].text == "用户流式回答"
    assert items[-1].model == "deepseek-chat"
    assert items[-1].credential_source == "user"
    assert items[-1].degraded is True
    assert StreamClient.calls == [
        ("user-key", "deepseek-reasoner", True),
        ("user-key", "deepseek-chat", True),
    ]


@pytest.mark.asyncio
async def test_platform_only_vision_policy_never_resolves_user_key(monkeypatch, tmp_path):
    from eduagents_core.gateway import openai_gateway

    _Client.calls = []
    monkeypatch.setattr(openai_gateway, "AsyncOpenAI", _Client)
    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        gateway_cassette_dir=str(tmp_path),
        standard_api_key="platform-key",
        standard_base_url="https://fake-platform/v1",
        standard_model="fake-vision",
    )
    resolver_calls = 0

    async def resolver(_req):
        nonlocal resolver_calls
        resolver_calls += 1
        return ResolvedCredential(
            source=CredentialSource.user,
            provider="deepseek",
            api_key="user-key",
            base_url="https://fake-user/v1",
            model="deepseek-chat",
        )

    gateway = OpenAIGateway(settings, credential_resolver=resolver)
    result = await gateway.chat(
        ChatRequest(
            messages=(ChatMessage(role="user", content="recognize image"),),
            tier=Tier.standard,
            credential_policy="platform",
            model_override="fake-vision",
        )
    )
    assert resolver_calls == 0
    assert _Client.calls == ["platform-key"]
    assert result.credential_source == "platform"


@pytest.mark.asyncio
async def test_record_mode_never_writes_byok_or_platform_fallback_to_cassette(
    monkeypatch, tmp_path
):
    from eduagents_core.gateway import openai_gateway

    _Client.calls = []
    _Client.fail_user = False
    monkeypatch.setattr(openai_gateway, "AsyncOpenAI", _Client)
    settings = Settings(
        _env_file=None,
        gateway_mode="record",
        gateway_cassette_dir=str(tmp_path / "cassettes"),
        gateway_max_retries=1,
        standard_api_key="platform-key",
    )
    fallback = False

    async def resolver(_req):
        return ResolvedCredential(
            source=CredentialSource.user,
            provider="deepseek",
            api_key="user-key",
            base_url="https://fake-user/v1",
            model="deepseek-chat",
            fallback_to_platform=fallback,
        )

    gateway = OpenAIGateway(settings, credential_resolver=resolver)
    await gateway.chat(ChatRequest(messages=(ChatMessage(role="user", content="private"),)))
    assert list((tmp_path / "cassettes").rglob("*.json")) == []

    fallback = True
    _Client.fail_user = True
    await gateway.chat(ChatRequest(messages=(ChatMessage(role="user", content="fallback"),)))
    assert list((tmp_path / "cassettes").rglob("*.json")) == []
    _Client.fail_user = False


@pytest.mark.asyncio
async def test_visual_requests_require_platform_policy_and_never_use_cassette(
    monkeypatch, tmp_path
):
    from eduagents_core.gateway import openai_gateway

    _Client.calls = []
    monkeypatch.setattr(openai_gateway, "AsyncOpenAI", _Client)
    settings = Settings(
        _env_file=None,
        gateway_mode="record",
        gateway_cassette_dir=str(tmp_path / "cassettes"),
        standard_api_key="platform-key",
        vision_model="fake-vision",
    )
    gateway = OpenAIGateway(settings)
    visual_content = [
        {"type": "text", "text": "transcribe"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]
    with pytest.raises(GatewayError, match="VISION_PLATFORM_CREDENTIAL_REQUIRED"):
        await gateway.chat(
            ChatRequest(messages=(ChatMessage(role="user", content=visual_content),))
        )

    await gateway.chat(
        ChatRequest(
            messages=(ChatMessage(role="user", content=visual_content),),
            model_override="fake-vision",
            credential_policy="platform",
            allow_tier_fallback=False,
        )
    )
    assert list((tmp_path / "cassettes").rglob("*.json")) == []


@pytest.mark.asyncio
async def test_replay_never_returns_shared_platform_response_to_byok_user(monkeypatch, tmp_path):
    from eduagents_core.gateway import openai_gateway

    _Client.calls = []
    monkeypatch.setattr(openai_gateway, "AsyncOpenAI", _Client)
    record_settings = Settings(
        _env_file=None,
        gateway_mode="record",
        gateway_cassette_dir=str(tmp_path / "cassettes"),
        standard_api_key="platform-key",
    )
    request = ChatRequest(
        messages=(ChatMessage(role="user", content="shared platform answer"),),
        tenant_id="tenant",
        user_id="user",
        request_id="safe-request-id",
    )
    await OpenAIGateway(record_settings).chat(request)
    assert list((tmp_path / "cassettes").rglob("*.json"))

    secret = "user-key"

    async def resolver(_req):
        return ResolvedCredential(
            source=CredentialSource.user,
            provider="deepseek",
            api_key=secret,
            base_url="https://fake-user/v1",
            model="deepseek-chat",
        )

    replay = OpenAIGateway(
        Settings(
            _env_file=None,
            gateway_mode="replay",
            gateway_cassette_dir=str(tmp_path / "cassettes"),
        ),
        credential_resolver=resolver,
    )
    with pytest.raises(GatewayError) as exc:
        await replay.chat(request)
    assert exc.value.code == "BYOK_REPLAY_DISABLED"
    assert _Client.calls == ["platform-key"]
    cassette_key = replay._cassette_key(request)
    assert secret not in cassette_key
    assert hashlib.sha256(secret.encode()).hexdigest() not in cassette_key
