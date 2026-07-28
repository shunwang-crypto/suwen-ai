"""plan_graph (F3 学习规划) 单测: FakeGateway 脚本化 + fake retrieval/web, 无网络。"""

import json

from eduagents_core.config import Settings
from eduagents_core.gateway.base import ChatRequest, ChatResult
from eduagents_core.graphs.plan_graph import build_plan_graph
from eduagents_core.graphs.state import GraphDeps
from eduagents_core.retrieval.base import RetrievalResult, RetrievedChunk
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command


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


async def fake_web_search(query: str) -> list[dict]:
    return [{"title": f"{query} 教程", "url": f"https://example.com/{query}", "content": "..."}]


PARSE = {"subject": "高等数学", "key_topics": ["极限", "导数"], "level_hint": "零基础"}
DIAGNOSE = {"diagnosis": {"极限": 0.3, "导数": 0.3}}
GOOD_DRAFT = {
    "stages": [
        {
            "name": "夯实基础",
            "days": [
                {"day": 1, "tasks": [{"title": "精读极限", "minutes": 60, "kc_names": ["极限"], "resource_ref": 0}]},
                {"day": 2, "tasks": [{"title": "练习导数", "minutes": 60, "kc_names": ["导数"], "resource_ref": None}]},
            ],
        }
    ]
}


def make_deps(gateway, retrieval=None, web=None) -> GraphDeps:
    return GraphDeps(
        settings=Settings(_env_file=None), gateway=gateway, retrieval=retrieval, web_search=web
    )


def base_input(**overrides) -> dict:
    payload = {
        "tenant_id": "t-1",
        "user_id": "u-1",
        "request_id": "r-1",
        "kb_id": "kb-1",
        "goal": "两周入门高等数学",
        "budget_hours_per_day": 1.0,
        "total_days": 2,
    }
    payload.update(overrides)
    return payload


def make_script(draft=GOOD_DRAFT) -> dict:
    return {"plan_parse_goal": [PARSE], "plan_diagnose": [DIAGNOSE], "plan_draft": [draft]}


async def run_to_interrupt(script, *, retrieval=None, web=None, input_overrides=None):
    gateway = FakeGateway(script)
    graph = build_plan_graph(make_deps(gateway, retrieval, web)).compile(
        checkpointer=MemorySaver()
    )
    config = {"configurable": {"thread_id": "plan-t1"}}
    state = await graph.ainvoke(base_input(**(input_overrides or {})), config)
    return gateway, graph, config, state


def flat_days(plan: dict) -> list[dict]:
    return [day for stage in plan["stages"] for day in stage["days"]]


def fix_events(state: dict) -> list[dict]:
    return [e for e in state["trace"] if e.get("node") == "draft_plan" and e.get("event") == "fix"]


# ---------------------------------------------------------------- 正常流 / interrupt


async def test_flow_hits_interrupt_then_approve_confirms():
    retrieval = FakeRetrieval()
    gateway, graph, config, state = await run_to_interrupt(
        make_script(), retrieval=retrieval, web=fake_web_search
    )
    # 挂起在 confirm_plan 的 interrupt 上
    assert "__interrupt__" in state
    payload = state["__interrupt__"][0].value
    assert payload["kind"] == "plan_confirm"
    assert payload["plan"]["stages"]
    assert state.get("plan_confirmed") is not True
    # kb + web 资源都收集到了 (kb: 2 topic x top3; web: 每 topic 1 条)
    kb_items = [r for r in state["resources"] if r["type"] == "kb"]
    web_items = [r for r in state["resources"] if r["type"] == "web"]
    assert len(kb_items) == 6 and all(r["chunk_id"] and r["doc_id"] for r in kb_items)
    assert len(web_items) == 2 and all(r["url"].startswith("https://") for r in web_items)
    assert retrieval.queries == ["极限", "导数"]

    final = await graph.ainvoke(Command(resume={"approved": True}), config)
    assert final["plan_confirmed"] is True
    assert final["plan"]["stages"]
    assert "__interrupt__" not in final


async def test_reject_leaves_plan_unconfirmed():
    _, graph, config, state = await run_to_interrupt(make_script())
    assert "__interrupt__" in state
    final = await graph.ainvoke(Command(resume={"approved": False}), config)
    assert final.get("plan_confirmed") is not True
    assert any(e.get("event") == "plan_rejected" for e in final["trace"])


async def test_approve_with_edited_plan_takes_user_version():
    _, graph, config, _ = await run_to_interrupt(
        make_script(),
        input_overrides={"kb_id": "", "total_days": 3, "kc_order": ["极限", "导数"]},
    )
    edited = {
        "stages": [
            {
                "name": "用户改过",
                "days": [
                    {
                        "day": 9,
                        "tasks": [
                            {
                                "title": "先学导数",
                                "minutes": 120,
                                "kc_names": ["导数"],
                                "resource_ref": 99,
                            }
                        ],
                    },
                    {
                        "day": 3,
                        "tasks": [
                            {
                                "title": "再学极限",
                                "minutes": 10,
                                "kc_names": ["极限"],
                                "resource_ref": None,
                            }
                        ],
                    },
                ],
            }
        ]
    }
    final = await graph.ainvoke(Command(resume={"approved": True, "plan": edited}), config)
    assert final["plan_confirmed"] is True
    days = flat_days(final["plan"])
    assert len(days) == 3
    assert [day["day"] for day in days] == [1, 2, 3]
    assert days[0]["tasks"][0]["kc_names"] == ["极限"]
    assert days[1]["tasks"][0]["kc_names"] == ["导数"]
    assert all(
        abs(sum(task["minutes"] for task in day["tasks"]) - 60) < 0.15 * 60
        for day in days
    )
    total_minutes = sum(task["minutes"] for day in days for task in day["tasks"])
    assert abs(total_minutes - 3 * 60) < 0.15 * 3 * 60
    assert days[1]["tasks"][0]["resource_ref"] is None
    assert any(e.get("event") == "plan_approved_with_edits" for e in final["trace"])
    confirm_fixes = [
        e
        for e in final["trace"]
        if e.get("node") == "confirm_plan" and e.get("event") == "fix"
    ]
    assert {e["fix"] for e in confirm_fixes} >= {
        "total_days_padded",
        "kc_order_deferred",
        "resource_ref_nulled",
        "day_minutes_scaled",
    }


async def test_approve_with_empty_edited_day_fills_task():
    _, graph, config, _ = await run_to_interrupt(make_script())
    edited = {
        "stages": [
            {
                "name": "用户改过",
                "days": [
                    {"day": 1, "tasks": []},
                    {
                        "day": 2,
                        "tasks": [
                            {
                                "title": "保留任务",
                                "minutes": 60,
                                "kc_names": [],
                                "resource_ref": None,
                            }
                        ],
                    },
                ],
            }
        ]
    }
    final = await graph.ainvoke(Command(resume={"approved": True, "plan": edited}), config)
    days = flat_days(final["plan"])
    assert days[0]["tasks"] == [
        {"title": "自主复习", "minutes": 60, "kc_names": [], "resource_ref": None}
    ]
    assert any(
        e.get("node") == "confirm_plan"
        and e.get("event") == "fix"
        and e.get("fix") == "empty_day_filled"
        for e in final["trace"]
    )


async def test_approve_with_invalid_edited_plan_keeps_hardened_draft():
    _, graph, config, state = await run_to_interrupt(make_script())
    original = state["plan"]
    final = await graph.ainvoke(
        Command(resume={"approved": True, "plan": {"stages": []}}), config
    )
    assert final["plan_confirmed"] is True
    assert final["plan"] == original
    assert any(
        e.get("event") == "plan_approved_invalid_edits_ignored" for e in final["trace"]
    )


# ---------------------------------------------------------------- 程序性硬校验


async def test_overbudget_day_minutes_scaled_into_range():
    draft = {
        "stages": [
            {
                "name": "超预算",
                "days": [
                    {
                        "day": 1,
                        "tasks": [
                            {"title": "任务A", "minutes": 120, "kc_names": [], "resource_ref": None},
                            {"title": "任务B", "minutes": 120, "kc_names": [], "resource_ref": None},
                        ],
                    },
                    {"day": 2, "tasks": [{"title": "任务C", "minutes": 10, "kc_names": [], "resource_ref": None}]},
                ],
            }
        ]
    }
    _, _, _, state = await run_to_interrupt(make_script(draft))
    budget = 1.0 * 60
    for day in flat_days(state["plan"]):
        total = sum(t["minutes"] for t in day["tasks"])
        assert abs(total - budget) < 0.15 * budget, day
    # 比例缩放: 120/120 -> 30/30
    day1 = flat_days(state["plan"])[0]
    assert [t["minutes"] for t in day1["tasks"]] == [30, 30]
    scaled = [e for e in fix_events(state) if e["fix"] == "day_minutes_scaled"]
    assert len(scaled) == 2  # 超预算日 + 欠预算日都被修正并记录 trace


async def test_exact_fifteen_percent_budget_boundary_is_scaled():
    draft = {
        "stages": [
            {
                "name": "边界预算",
                "days": [
                    {
                        "day": 1,
                        "tasks": [
                            {
                                "title": "下边界",
                                "minutes": 51,
                                "kc_names": [],
                                "resource_ref": None,
                            }
                        ],
                    },
                    {
                        "day": 2,
                        "tasks": [
                            {
                                "title": "上边界",
                                "minutes": 69,
                                "kc_names": [],
                                "resource_ref": None,
                            }
                        ],
                    },
                ],
            }
        ]
    }
    _, _, _, state = await run_to_interrupt(make_script(draft))
    assert [sum(t["minutes"] for t in day["tasks"]) for day in flat_days(state["plan"])] == [
        60,
        60,
    ]
    assert len([e for e in fix_events(state) if e["fix"] == "day_minutes_scaled"]) == 2


async def test_total_days_padded_when_draft_too_short():
    draft = {
        "stages": [
            {"name": "只有一天", "days": [{"day": 1, "tasks": [{"title": "任务A", "minutes": 60, "kc_names": [], "resource_ref": None}]}]}
        ]
    }
    _, _, _, state = await run_to_interrupt(make_script(draft))
    days = flat_days(state["plan"])
    assert len(days) == 2
    assert [d["day"] for d in days] == [1, 2]
    assert any(e["fix"] == "total_days_padded" for e in fix_events(state))


async def test_total_days_trimmed_when_draft_too_long():
    draft = {
        "stages": [
            {
                "name": "太多天",
                "days": [
                    {"day": n, "tasks": [{"title": f"任务{n}", "minutes": 60, "kc_names": [], "resource_ref": None}]}
                    for n in range(1, 5)
                ],
            }
        ]
    }
    _, _, _, state = await run_to_interrupt(make_script(draft))
    days = flat_days(state["plan"])
    assert len(days) == 2
    assert [d["day"] for d in days] == [1, 2]
    assert any(e["fix"] == "total_days_trimmed" for e in fix_events(state))


async def test_resource_ref_out_of_range_nulled():
    draft = {
        "stages": [
            {
                "name": "越界引用",
                "days": [
                    {"day": 1, "tasks": [{"title": "任务A", "minutes": 60, "kc_names": [], "resource_ref": 99}]},
                    {"day": 2, "tasks": [{"title": "任务B", "minutes": 60, "kc_names": [], "resource_ref": 0}]},
                ],
            }
        ]
    }
    # 无 retrieval / 无 web -> resources 为空, 任何 resource_ref 均越界
    _, _, _, state = await run_to_interrupt(make_script(draft), input_overrides={"kb_id": ""})
    tasks = [t for day in flat_days(state["plan"]) for t in day["tasks"]]
    assert all(t["resource_ref"] is None for t in tasks)
    nulled = [e for e in fix_events(state) if e["fix"] == "resource_ref_nulled"]
    assert len(nulled) == 2


async def test_kc_order_violation_deferred():
    draft = {
        "stages": [
            {
                "name": "违序",
                "days": [
                    {"day": 1, "tasks": [{"title": "先学导数", "minutes": 60, "kc_names": ["导数"], "resource_ref": None}]},
                    {"day": 2, "tasks": [{"title": "再学极限", "minutes": 60, "kc_names": ["极限"], "resource_ref": None}]},
                ],
            }
        ]
    }
    _, _, _, state = await run_to_interrupt(
        make_script(draft), input_overrides={"kb_id": "", "kc_order": ["极限", "导数"]}
    )
    tasks = [t for day in flat_days(state["plan"]) for t in day["tasks"]]
    # 违序任务被顺延: 首次出现顺序符合 kc_order
    assert tasks[0]["kc_names"] == ["极限"]
    assert tasks[1]["kc_names"] == ["导数"]
    deferred = [e for e in fix_events(state) if e["fix"] == "kc_order_deferred"]
    assert len(deferred) == 1 and "先学导数" in deferred[0]["moved"]


# ---------------------------------------------------------------- 降级路径


async def test_no_retrieval_no_web_plan_still_generated_resources_pending():
    _, _, _, state = await run_to_interrupt(make_script(), input_overrides={"kb_id": ""})
    assert state["resources"] == []
    assert state["plan"]["stages"]  # 计划仍生成, 资源标"待补充"
    assert any(e.get("event") == "resources_pending" for e in state["trace"])


async def test_external_diagnosis_skips_diagnose_llm_call():
    gateway, _, _, state = await run_to_interrupt(
        make_script(), input_overrides={"diagnosis": {"极限": 0.6, "导数": 0.2}}
    )
    assert state["diagnosis"] == {"极限": 0.6, "导数": 0.2}
    assert all(req.feature != "plan_diagnose" for req in gateway.requests)
    assert any(e.get("event") == "external_diagnosis_used" for e in state["trace"])
