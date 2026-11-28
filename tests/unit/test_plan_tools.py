"""plan 领域工具的业务权限、校验与修复；原生执行覆盖见 test_native_agent_tasks.py。"""

import copy
import json

import pytest
from eduagents_core.config import Settings
from eduagents_core.education.context import EducationDeps
from eduagents_core.education.plan import PlanTools
from eduagents_core.gateway.base import ChatRequest, ChatResult
from eduagents_core.harness.task import TaskRecord
from eduagents_core.retrieval.base import RetrievalResult, RetrievedChunk


class FakeGateway:
    """脚本化网关: 按 feature 排队出词 (末项粘性可复用), 记录全部请求供断言。"""

    def __init__(self, script: dict[str, list] | None = None):
        self.requests: list[ChatRequest] = []
        self._script = {k: list(v) for k, v in (script or {}).items()}

    async def chat(self, req: ChatRequest) -> ChatResult:
        self.requests.append(req)
        queue = self._script.get(req.feature, [])
        if not queue:
            payload = "{}"
        elif len(queue) == 1:
            payload = queue[0]
        else:
            payload = queue.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return ChatResult(
            text=text,
            model="fake",
            tier=str(req.tier),
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="replay",
        )


class FakeRetrieval:
    def __init__(self):
        self.queries: list[str] = []

    async def retrieve(self, tenant_id: str, kb_id: str, query: str) -> RetrievalResult:
        self.queries.append(query)
        chunks = [
            RetrievedChunk(
                chunk_id=f"{query}-c{i}",
                text=f"{query} 的讲解 {i}",
                heading_path=f"第1章 > {query} 小节{i}",
                doc_id="doc-1",
                page_start=1,
                page_end=2,
                score=0.9,
                source="fused",
            )
            for i in range(3)
        ]
        return RetrievalResult(chunks=chunks, dense_hits=3, confident=True)


PARSE = {"subject": "高等数学", "key_topics": ["极限", "导数"], "level_hint": "零基础"}
GOOD_DRAFT = {
    "stages": [
        {
            "name": "夯实基础",
            "days": [
                {
                    "day": 1,
                    "tasks": [
                        {
                            "title": "精读极限",
                            "minutes": 60,
                            "kc_names": ["极限"],
                            "resource_ref": 0,
                        }
                    ],
                },
                {
                    "day": 2,
                    "tasks": [
                        {
                            "title": "练习导数",
                            "minutes": 60,
                            "kc_names": ["导数"],
                            "resource_ref": None,
                        }
                    ],
                },
            ],
        }
    ]
}


def make_deps(gateway, retrieval=None, web=None) -> EducationDeps:
    return EducationDeps(
        settings=Settings(_env_file=None), gateway=gateway, retrieval=retrieval, web_search=web
    )


def base_input(**overrides) -> dict:
    payload = {
        "tenant_id": "t-1",
        "user_id": "u-1",
        "request_id": "r-1",
        "plan_mode": "course_bound",
        "kb_id": "kb-1",
        "goal": "两周入门高等数学",
        "budget_hours_per_day": 1.0,
        "total_days": 2,
    }
    payload.update(overrides)
    return payload


def flat_days(plan: dict) -> list[dict]:
    return [day for stage in plan["stages"] for day in stage["days"]]


def plan_tools(**overrides):
    record = TaskRecord(state=base_input(**overrides), status="running")
    tools = PlanTools(make_deps(FakeGateway()), record, lambda _: None)
    return (tools, record)


@pytest.mark.parametrize("days,budget", [(1, 0.25), (2, 1), (7, 2), (30, 0.5)])
async def test_plan_hardens_days_budget_and_user_confirmation(days, budget):
    tools, record = plan_tools(total_days=days, budget_hours_per_day=budget)
    legacy = copy.deepcopy(GOOD_DRAFT)
    stage = legacy["stages"][0]
    del stage["name"]
    del stage["days"][0]["day"]
    stage["days"][0]["tasks"][0]["minutes"] = "60"
    await tools.handlers["submit_plan_draft"]({"plan": legacy, "goal_outline": PARSE})
    assert record.status == "awaiting_confirmation"
    actual = flat_days(record.state["plan"])
    assert len(actual) == days and [d["day"] for d in actual] == list(range(1, days + 1))
    assert all(
        abs(sum(t["minutes"] for t in d["tasks"]) - budget * 60) <= budget * 60 * 0.15
        for d in actual
    )
    tools.confirm({"approved": True})
    next_tools = PlanTools(tools.deps, record, lambda _: None)
    assert set(next_tools.handlers) == {"read_learning_context", "publish_plan"}
    record.status = "running"
    await next_tools.handlers["publish_plan"]({})
    assert record.status == "completed"


@pytest.mark.parametrize(
    "edited", [None, {}, {"stages": [{"name": "编辑", "days": [{"day": 1, "tasks": []}]}]}]
)
async def test_user_edits_are_hardened_and_invalid_edits_keep_original(edited):
    tools, record = plan_tools()
    await tools.submit({"plan": copy.deepcopy(GOOD_DRAFT)})
    tools.confirm({"approved": True, "plan": edited})
    assert len(flat_days(record.state["plan"])) == 2
    assert all(day["tasks"] for day in flat_days(record.state["plan"]))


async def test_plan_rejection_cannot_publish():
    tools, record = plan_tools()
    await tools.submit({"plan": copy.deepcopy(GOOD_DRAFT)})
    tools.confirm({"approved": False})
    assert record.status == "cancelled"
    assert not (await tools.publish({}))["accepted"]


@pytest.mark.parametrize(
    "mode,allowed", [("course_bound", "search_knowledge_base"), ("goal_only", "web_search")]
)
async def test_plan_resource_scope_enforced_on_restored_sessions(mode, allowed):
    tools, record = plan_tools(
        plan_mode=mode,
        resources=[
            {"type": "kb", "kb_id": "wrong", "chunk_id": "bad"},
            {"type": "web", "url": "http://127.0.0.1/private"},
            {"type": "kb", "kb_id": "kb-1", "chunk_id": "ok", "title": "教材"},
            {"type": "web", "url": "https://example.com/guide", "title": "教程"},
        ],
    )
    tools = PlanTools(
        make_deps(FakeGateway(), FakeRetrieval(), fake_web_search), record, lambda _: None
    )
    assert allowed in tools.handlers
    assert len(tools.resources()) == 1
    assert tools.resources()[0]["type"] == ("kb" if mode == "course_bound" else "web")


async def test_plan_prerequisite_order_and_invalid_resource_refs_are_fixed():
    tools, record = plan_tools(kc_order=["极限", "导数"])
    reverse = copy.deepcopy(GOOD_DRAFT)
    reverse["stages"][0]["days"].reverse()
    for d in reverse["stages"][0]["days"]:
        for t in d["tasks"]:
            t["resource_ref"] = 999
    await tools.submit({"plan": reverse})
    assert all(
        t["resource_ref"] is None for d in flat_days(record.state["plan"]) for t in d["tasks"]
    )
    names = [
        kc for d in flat_days(record.state["plan"]) for t in d["tasks"] for kc in t["kc_names"]
    ]
    assert names.index("极限") <= names.index("导数")


async def fake_web_search(query):
    return []
