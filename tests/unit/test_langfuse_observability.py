"""M3-D1 Langfuse 接线单测: 全程 mock langfuse 客户端, 零网络。

覆盖硬约束:
(a) 未配置 -> 不创建客户端、不 import langfuse;
(b) 配置后一次网关调用产生一条 generation 且字段与网关真实计量一致;
(c) 上报抛异常不影响网关返回;
以及 P2-7 脱敏: 原文默认不上报、user 哈希不可逆、画像数据无条件不出现。
"""

import contextlib
import hashlib
import sys
import types

import eduagents_core.observability as obs
import pytest
from eduagents_core.config import Settings
from eduagents_core.gateway import ChatMessage, ChatRequest, OpenAIGateway, Tier, UsageEvent
from eduagents_core.observability import flush_langfuse, observe_stages

_LF_KEYS = {
    "langfuse_public_key": "pk-lf-test",
    "langfuse_secret_key": "sk-lf-test",
    "langfuse_host": "http://langfuse.local",
}


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        gateway_cassette_dir=str(tmp_path / "cassettes"),
        gateway_stream_delay_ms=0,
        **overrides,
    )


def req(text: str = "什么是泰勒公式?") -> ChatRequest:
    return ChatRequest(
        messages=(ChatMessage(role="user", content=text),),
        tier=Tier.standard,
        tenant_id="11111111-1111-1111-1111-111111111111",
        user_id="22222222-2222-2222-2222-222222222222",
        request_id="req-lf-test",
    )


# ---- fake langfuse (SDK 形状与 4.14.1 一致的最小面) ----


class FakeObservation:
    def __init__(self, kwargs: dict):
        self.kwargs = kwargs
        self.children: list[FakeObservation] = []
        self.ended = False

    def start_observation(self, **kw):
        child = FakeObservation(kw)
        self.children.append(child)
        return child

    def end(self, **_kw):
        self.ended = True
        return self


class FakeLangfuse:
    last: "FakeLangfuse | None" = None

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.observations: list[FakeObservation] = []
        self.flushed = False
        FakeLangfuse.last = self

    @staticmethod
    def create_trace_id(*, seed=None) -> str:
        return hashlib.sha256((seed or "").encode()).hexdigest()[:32]

    def start_observation(self, **kw):
        one = FakeObservation(kw)
        self.observations.append(one)
        return one

    def flush(self):
        self.flushed = True


class ExplodingLangfuse(FakeLangfuse):
    def start_observation(self, **kw):
        raise RuntimeError("boom")


def install_fake_langfuse(monkeypatch, client_cls=FakeLangfuse):
    mod = types.ModuleType("langfuse")
    mod.Langfuse = client_cls
    mod.propagated = []

    @contextlib.contextmanager
    def propagate_attributes(**kwargs):
        mod.propagated.append(kwargs)
        yield

    mod.propagate_attributes = propagate_attributes
    monkeypatch.setitem(sys.modules, "langfuse", mod)
    return mod


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """单例隔离 + 环境净化: 每测重置客户端, 保证门控只看测试内的 Settings。"""
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
                "LANGFUSE_LOG_CONTENT"):
        monkeypatch.delenv(var, raising=False)
    obs.reset_langfuse_client()
    yield
    obs.reset_langfuse_client()
    FakeLangfuse.last = None


# ---- (a) 未配置: 零客户端、零 import ----


async def test_disabled_creates_no_client_and_never_imports(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "langfuse", raising=False)
    settings = make_settings(tmp_path)  # 三个 LANGFUSE_* 均为空
    gw = OpenAIGateway(settings)
    result = await gw.chat(req())
    assert result.mode == "stub"  # 业务照常
    assert "langfuse" not in sys.modules  # 懒加载: 未启用不 import
    assert obs.get_langfuse_client(settings) is None
    assert obs._client is None and obs._client_failed is False


async def test_partial_env_still_disabled(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "langfuse", raising=False)
    # 只给 2/3 变量: 门控要求三者齐全
    settings = make_settings(
        tmp_path, langfuse_public_key="pk", langfuse_secret_key="sk"
    )
    assert obs.langfuse_enabled(settings) is False
    assert obs.get_langfuse_client(settings) is None
    assert "langfuse" not in sys.modules


# ---- (b) 配置后: 一次网关调用 -> 一条 generation, 字段与真实计量一致 ----


async def test_gateway_chat_emits_one_generation_with_real_metering(tmp_path, monkeypatch):
    install_fake_langfuse(monkeypatch)
    settings = make_settings(tmp_path, **_LF_KEYS)
    gw = OpenAIGateway(settings)
    result = await gw.chat(req())

    client = FakeLangfuse.last
    assert client is not None
    assert client.init_kwargs["public_key"] == "pk-lf-test"
    assert client.init_kwargs["host"] == "http://langfuse.local"
    assert len(client.observations) == 1  # 恰一条 generation

    gen = client.observations[0].kwargs
    assert gen["as_type"] == "generation"
    assert gen["name"] == "gateway.chat"
    assert gen["model"] == result.model
    # tokens/cost/latency 与网关计量 (ChatResult) 同源, 非编造
    assert gen["usage_details"] == {
        "input": result.prompt_tokens,
        "output": result.completion_tokens,
        "total": result.prompt_tokens + result.completion_tokens,
    }
    meta = gen["metadata"]
    assert meta["mode"] == "stub"  # stub 也如实上报
    assert meta["requested_tier"] == "standard"
    assert meta["used_tier"] == result.tier
    assert meta["degraded"] is False
    assert meta["cache_hit"] is False
    assert meta["cost_cny"] == result.cost_cny
    assert meta["latency_ms"] == result.latency_ms
    assert meta["request_id"] == "req-lf-test"
    # 同一 request_id -> 确定性 trace_id (请求级汇聚)
    assert gen["trace_context"] == {
        "trace_id": FakeLangfuse.create_trace_id(seed="req-lf-test")
    }
    assert client.observations[0].ended is True


async def test_content_not_reported_by_default_and_user_hashed(tmp_path, monkeypatch):
    install_fake_langfuse(monkeypatch)
    settings = make_settings(tmp_path, **_LF_KEYS)
    gw = OpenAIGateway(settings)
    result = await gw.chat(req("我妈得了乳腺癌, 帮我讲讲化疗"))

    gen = FakeLangfuse.last.observations[0].kwargs
    # 原文不出现在任何上报字段 (只上 <pii:hash:len> 指纹与长度)
    dumped = repr(gen)
    assert "乳腺癌" not in dumped
    assert result.text not in dumped
    assert str(gen["input"]).startswith("<pii:")
    assert str(gen["output"]).startswith("<pii:")
    assert gen["metadata"]["completion_chars"] == len(result.text)
    # user 标识不可逆哈希: 非原值、确定性、trace 级同样只见哈希
    user_hash = gen["metadata"]["user_hash"]
    assert user_hash.startswith("u_") and "2222" not in user_hash
    assert user_hash == obs.hash_user("22222222-2222-2222-2222-222222222222")
    lf_mod = sys.modules["langfuse"]
    assert lf_mod.propagated and lf_mod.propagated[0]["user_id"] == user_hash


async def test_content_reported_only_when_explicitly_enabled(tmp_path, monkeypatch):
    install_fake_langfuse(monkeypatch)
    settings = make_settings(tmp_path, langfuse_log_content=True, **_LF_KEYS)
    gw = OpenAIGateway(settings)
    result = await gw.chat(req("什么是泰勒公式?"))
    gen = FakeLangfuse.last.observations[0].kwargs
    assert gen["input"] == [{"role": "user", "content": "什么是泰勒公式?"}]
    assert gen["output"] == result.text


# ---- (c) 上报抛异常: 网关返回与 usage sink 不受影响 ----


async def test_reporting_failure_does_not_break_gateway(tmp_path, monkeypatch):
    install_fake_langfuse(monkeypatch, client_cls=ExplodingLangfuse)
    events: list[UsageEvent] = []

    async def sink(e: UsageEvent) -> None:
        events.append(e)

    settings = make_settings(tmp_path, **_LF_KEYS)
    gw = OpenAIGateway(settings, usage_sink=sink)
    result = await gw.chat(req())
    assert result.mode == "stub" and result.text  # 正常返回
    assert len(events) == 1  # usage 计量不受上报异常影响


async def test_client_init_failure_is_tried_once_and_silent(tmp_path, monkeypatch):
    mod = types.ModuleType("langfuse")

    class BrokenInit:
        def __init__(self, **kwargs):
            raise RuntimeError("no network")

    mod.Langfuse = BrokenInit
    monkeypatch.setitem(sys.modules, "langfuse", mod)
    settings = make_settings(tmp_path, **_LF_KEYS)
    gw = OpenAIGateway(settings)
    result = await gw.chat(req())
    assert result.mode == "stub"
    assert obs._client_failed is True  # 失败只试一次, 不反复拖慢热路径
    assert obs.get_langfuse_client(settings) is None


# ---- 请求级 span 汇聚 (qa 图 state.trace) + 画像铁律 ----


async def test_observe_stages_hangs_spans_on_request_trace(tmp_path, monkeypatch):
    install_fake_langfuse(monkeypatch)
    settings = make_settings(tmp_path, **_LF_KEYS)
    stages = [
        {"type": "router", "route": "kb", "reason": "含术语, 建议查库", "rule_hit": False},
        {"type": "retrieval", "queries": 2, "returned": 5, "confident": True},
        {"type": "rewrite", "from": "原始问题", "to": "改写问题", "retry_count": 1},
        {"type": "profile", "weak_points": ["导数", "极限"], "chars": 120},
    ]
    observe_stages(settings, request_id="req-lf-test", stages=stages, user_id="u-1")

    client = FakeLangfuse.last
    assert len(client.observations) == 1
    root = client.observations[0]
    assert root.kwargs["as_type"] == "span"
    assert root.kwargs["name"] == "chat.qa"
    # 与网关 generation 同种子 -> 同 trace (请求级汇聚)
    assert root.kwargs["trace_context"] == {
        "trace_id": FakeLangfuse.create_trace_id(seed="req-lf-test")
    }
    assert [c.kwargs["name"] for c in root.children] == [
        "chat.qa.router", "chat.qa.retrieval", "chat.qa.rewrite", "chat.qa.profile",
    ]
    assert root.ended and all(c.ended for c in root.children)

    dumped = repr([c.kwargs for c in root.children])
    # 画像数据不得出现 (P2-7 铁律); 数值/布尔照常; 内容字符串被指纹化
    assert "导数" not in dumped and "极限" not in dumped
    assert "原始问题" not in dumped and "改写问题" not in dumped
    profile_meta = root.children[3].kwargs["metadata"]
    assert profile_meta == {"type": "profile", "weak_points_count": 2, "chars": 120}
    rewrite_meta = root.children[2].kwargs["metadata"]
    assert rewrite_meta["retry_count"] == 1
    assert rewrite_meta["from"].startswith("<pii:")
    retrieval_meta = root.children[1].kwargs["metadata"]
    assert retrieval_meta == {
        "type": "retrieval", "queries": 2, "returned": 5, "confident": True,
    }


async def test_profile_data_never_reported_even_with_log_content(tmp_path, monkeypatch):
    install_fake_langfuse(monkeypatch)
    settings = make_settings(tmp_path, langfuse_log_content=True, **_LF_KEYS)
    observe_stages(
        settings,
        request_id="req-lf-test",
        stages=[{"type": "profile", "weak_points": ["导数"], "chars": 9,
                 "mastery": [0.35]}],
    )
    root = FakeLangfuse.last.observations[0]
    meta = root.children[0].kwargs["metadata"]
    assert meta == {"type": "profile", "weak_points_count": 1, "chars": 9,
                    "mastery_count": 1}
    assert "导数" not in repr(meta) and "0.35" not in repr(meta)


async def test_flush_on_shutdown_and_noop_when_disabled(tmp_path, monkeypatch):
    flush_langfuse()  # 未启用: 零操作不抛
    install_fake_langfuse(monkeypatch)
    settings = make_settings(tmp_path, **_LF_KEYS)
    assert obs.get_langfuse_client(settings) is not None
    flush_langfuse()
    assert FakeLangfuse.last.flushed is True
