"""成本安全线横切单测 (P0-2a / P1-9 / P1-10 / P1-15):

- worker 网关工厂: 进程级单例, 带 usage_sink + budget_guard (P0-2a/P1-15);
- 降级链: cheap 不升价 ([cheap]), standard/strong 只降不升 (P1-10);
- qa_graph: cheap 辅助调用遇 BudgetExceeded 必须冒头, 其余 GatewayError 照旧降级 (P1-9);
- API 错误信封: BudgetExceeded -> 429 + Retry-After; 新增三个错误码中文文案 (P2-15)。
"""

from __future__ import annotations

import pytest
from eduagents_core.config import Settings
from eduagents_core.education.context import EducationDeps
from eduagents_core.gateway import BudgetExceeded, GatewayError, Tier
from eduagents_core.gateway.openai_gateway import _DEGRADE, _TIER_RANK, _warn_if_upcost
from eduagents_core.harness.task import AgentTask
from eduagents_core.retrieval.base import RetrievalResult, RetrievedChunk

# ---------- worker 网关单例 (P0-2a / P1-15) ----------


def _settings() -> Settings:
    return Settings(_env_file=None, user_daily_budget_cny=5.0)


@pytest.fixture
def _reset_worker_gateway():
    import eduagents_worker.enrichment as enrichment

    enrichment._worker_gateway = None
    yield
    enrichment._worker_gateway = None


def test_worker_gateway_singleton_with_guard_and_sink(_reset_worker_gateway):
    from eduagents_api.budget import RedisBudgetGuard
    from eduagents_api.usage import persist_usage
    from eduagents_api.wallet import WalletBudgetGuard
    from eduagents_worker.enrichment import get_worker_gateway

    settings = _settings()
    gw1 = get_worker_gateway(settings)
    gw2 = get_worker_gateway(settings)
    assert gw1 is gw2  # 进程级单例: 断路器状态跨任务共享 (P1-15)
    assert gw1._sink is persist_usage  # 后台支出进 usage_log (P0-2a)
    # 0006: 后台任务受 钱包(402)+预算(429) 组合约束 — 组合 guard 内层仍是预算 guard
    assert isinstance(gw1._guard, WalletBudgetGuard)
    assert isinstance(gw1._guard._inner, RedisBudgetGuard)
    # reflection / study_guide 引用的是同一工厂
    from eduagents_worker.reflection import reflect_session  # noqa: F401
    from eduagents_worker.study_guide import build_study_guide  # noqa: F401


# ---------- 降级链 (P1-10) ----------


def test_degrade_chain_never_upgrades_price():
    generic_rank = {tier: rank for tier, rank in _TIER_RANK.items() if rank is not None}
    assert _DEGRADE[Tier.cheap] == [Tier.cheap]  # cheap 降级不得升价 5.7~23 倍
    for requested in (Tier.cheap, Tier.standard, Tier.strong):
        chain = _DEGRADE[requested]
        assert chain[0] == requested
        assert all(generic_rank[t] <= generic_rank[requested] for t in chain)

    # plan is an independent business tier, not a claim that its dedicated
    # provider is always more expensive than strong. Its fallback path is an
    # explicit contract and must be reviewed as a whole when changed.
    assert _TIER_RANK[Tier.plan] is None
    assert _DEGRADE[Tier.plan] == [Tier.plan, Tier.strong, Tier.standard, Tier.cheap]


def test_plan_fallback_cost_warning_uses_active_prices(monkeypatch):
    from eduagents_core.gateway import openai_gateway_mixins

    settings = Settings(
        _env_file=None,
        plan_input_price_per_m=1.0,
        plan_output_price_per_m=2.0,
        strong_input_price_per_m=4.0,
        strong_output_price_per_m=16.0,
        standard_input_price_per_m=2.0,
        standard_output_price_per_m=8.0,
    )
    warnings: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        openai_gateway_mixins.log,
        "warning",
        lambda event, **fields: warnings.append((event, fields)),
    )

    _warn_if_upcost(settings, Tier.plan, Tier.strong)
    assert len(warnings) == 1  # strong output is more expensive in current config
    assert warnings[0][0] == "degraded_upcost"
    assert warnings[0][1]["comparable_rank"] is False

    warnings.clear()
    _warn_if_upcost(settings, Tier.plan, Tier.cheap)
    assert warnings == []  # cheap is below plan in both configured units


# ---------- qa_graph: BudgetExceeded 冒头 (P1-9) ----------


class _BudgetBlownGateway:
    """任何调用都抛 BudgetExceeded (预算 100% 时网关 precheck 的真实行为)。"""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    async def chat(self, req):
        self.calls += 1
        raise self._exc


class _OneShotRetrieval:
    async def retrieve(self, tenant_id, kb_id, query):
        chunks = [
            RetrievedChunk(
                chunk_id=f"c{i}",
                text="导数定义相关内容",
                heading_path="第3章",
                doc_id="d1",
                page_start=1,
                page_end=1,
                score=0.9,
                source="fused",
            )
            for i in (1, 2)  # ≥2 条: grade 解析失败宽松降级后仍可进 generate
        ]
        return RetrievalResult(
            chunks=chunks,
            dense_hits=2,
            sparse_hits=0,
            used_sparse=False,
            used_rerank=False,
            confident=False,
        )


def _kb_state() -> dict:
    return {
        "query": "什么是导数",  # 无联网关键词 -> 规则路由 kb -> query_plan (cheap 辅助调用)
        "kb_id": "kb-1",
        "tenant_id": "t1",
        "user_id": "u1",
        "request_id": "r1",
        "messages": [],
        "retry_count": 0,
    }


async def test_budget_exceeded_propagates_out_of_cheap_helper():
    gateway = _BudgetBlownGateway(BudgetExceeded(retry_after_seconds=3600))
    deps = EducationDeps(
        settings=Settings(_env_file=None),
        gateway=gateway,
        retrieval=_OneShotRetrieval(),
        web_search=None,
    )
    task = AgentTask("qa", deps)
    with pytest.raises(BudgetExceeded):
        await task.run("cost-test", _kb_state())
    assert gateway.calls == 1  # 第一笔 cheap 调用即熔断, 不再继续烧后续节点


async def test_other_gateway_errors_still_degrade():
    # 非预算类网关错误维持旧语义: cheap 辅助调用失败只降级, 图继续跑
    gateway = _BudgetBlownGateway(GatewayError("ALL_PROVIDERS_FAILED", "x"))
    deps = EducationDeps(
        settings=Settings(_env_file=None),
        gateway=gateway,
        retrieval=_OneShotRetrieval(),
        web_search=None,
    )
    task = AgentTask("qa", deps)
    # generate 节点也会失败 (同一个 gateway), 但那不是 cheap 辅助调用的静默吞没路径:
    # 它以 GatewayError 直接冒头 — 断言不是 BudgetExceeded 即可
    with pytest.raises(GatewayError) as exc_info:
        await task.run("cost-test", _kb_state())
    assert not isinstance(exc_info.value, BudgetExceeded)
    assert gateway.calls == 1  # cheap 辅助调用失败被吞掉降级, 图走到了后续节点


# ---------- API 信封: 429 + Retry-After (P1-9) 与新错误码 (P2-15) ----------


def _mini_app():
    from eduagents_api.middleware import install_error_handlers
    from fastapi import FastAPI, HTTPException

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom-budget")
    async def boom_budget():
        raise BudgetExceeded(retry_after_seconds=7200)

    @app.get("/boom-budget-bare")
    async def boom_budget_bare():
        raise BudgetExceeded()  # 无 retry_after -> handler 按沪时次日零点兜底

    @app.get("/boom-kb")
    async def boom_kb():
        raise HTTPException(status_code=403, detail="KB_FORBIDDEN")

    @app.get("/boom-retrieval")
    async def boom_retrieval():
        raise HTTPException(status_code=503, detail="RETRIEVAL_UNAVAILABLE")

    @app.get("/boom-org")
    async def boom_org():
        raise HTTPException(status_code=403, detail="ORG_READONLY")

    return app


def test_budget_exceeded_becomes_429_with_retry_after():
    from fastapi.testclient import TestClient

    client = TestClient(_mini_app())
    resp = client.get("/boom-budget")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "7200"
    body = resp.json()
    assert body["code"] == "BUDGET_EXCEEDED"
    assert "上限" in body["message"]

    resp2 = client.get("/boom-budget-bare")
    assert resp2.status_code == 429
    assert 1 <= int(resp2.headers["Retry-After"]) <= 86400  # 距沪时次日零点


# ---------- 流中断也结算 (P0-6 计费半段) ----------


class _FakeStreamClient:
    """伪 AsyncOpenAI: 产出 N 个 delta chunk 的流 (无 usage 帧, 模拟中断前形态)。"""

    def __init__(self, deltas: list[str]):
        from types import SimpleNamespace

        async def create(**kwargs):
            async def gen():
                for d in deltas:
                    yield SimpleNamespace(
                        usage=None,
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=d))],
                    )

            return gen()

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


async def test_stream_client_disconnect_still_settles_usage(tmp_path, monkeypatch):
    """消费一半后 aclose() (客户端断开): usage_sink 必须收到部分消耗事件。"""
    from eduagents_core.gateway import ChatMessage, ChatRequest, UsageEvent
    from eduagents_core.gateway.openai_gateway import OpenAIGateway

    events: list[UsageEvent] = []

    async def sink(e: UsageEvent) -> None:
        events.append(e)

    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        gateway_cassette_dir=str(tmp_path / "cassettes"),
        standard_api_key="fake-key",
    )
    gw = OpenAIGateway(settings, usage_sink=sink)
    monkeypatch.setattr(
        gw, "_client", lambda tier: _FakeStreamClient(["泰勒", "公式", "展开", "如下"])
    )
    req = ChatRequest(
        messages=(ChatMessage(role="user", content="什么是泰勒公式?"),),
        tier=Tier.standard,
        feature="qa_generate",
        tenant_id="t1",
        user_id="u1",
        request_id="r1",
    )
    stream = gw.stream_chat(req)
    got = [await anext(stream), await anext(stream)]  # 消费一半
    assert [d.text for d in got] == ["泰勒", "公式"]
    await stream.aclose()  # 客户端断开 -> GeneratorExit 进生成器

    assert len(events) == 1  # 部分消耗照样结算, 不再漏计费
    e = events[0]
    assert e.mode == "live"
    assert e.feature == "qa_generate"
    assert e.prompt_tokens > 0 and e.completion_tokens > 0  # 估算 token
    assert e.cost_cny > 0


async def test_stream_full_consumption_settles_exactly_once(tmp_path, monkeypatch):
    from eduagents_core.gateway import ChatMessage, ChatRequest, UsageEvent
    from eduagents_core.gateway.openai_gateway import OpenAIGateway

    events: list[UsageEvent] = []

    async def sink(e: UsageEvent) -> None:
        events.append(e)

    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        gateway_cassette_dir=str(tmp_path / "cassettes"),
        standard_api_key="fake-key",
    )
    gw = OpenAIGateway(settings, usage_sink=sink)
    monkeypatch.setattr(gw, "_client", lambda tier: _FakeStreamClient(["完整", "回答"]))
    req = ChatRequest(
        messages=(ChatMessage(role="user", content="什么是泰勒公式?"),),
        tier=Tier.standard,
        tenant_id="t1",
        user_id="u1",
    )
    items = [item async for item in gw.stream_chat(req)]
    assert items[-1].text == "完整回答"
    assert len(events) == 1  # 正常跑完只结算一次, finally 不重复结算


def test_new_error_codes_have_chinese_envelope():
    from fastapi.testclient import TestClient

    client = TestClient(_mini_app())
    for path, code, status in (
        ("/boom-kb", "KB_FORBIDDEN", 403),
        ("/boom-retrieval", "RETRIEVAL_UNAVAILABLE", 503),
        ("/boom-org", "ORG_READONLY", 403),
    ):
        resp = client.get(path)
        assert resp.status_code == status
        body = resp.json()
        assert body["code"] == code
        assert body["message"] != code  # 不再显示英文裸码
