"""计划个性化 (P1-3) 与 tutor 画像注入 (P1-4) 单测: FakeGateway 脚本化, 无网络无库。

覆盖三组验收:
① 真实掌握度注入 (G1): diagnose 节点不再让 LLM 猜有真实记录的 KC, LLM 只兜底无记录主题,
   且 LLM 输出不得覆盖真实 BKT 值;
② 先修 DAG 拓扑约束 (G2, F3-A2 口径): 先修边 -> 拓扑序注入 kc_order 后,
   修正后的计划中无任务先于其未掌握前置 KC;
③ tutor 画像注入 (G9): 四级 hint prompt 实收画像段, 开场关联薄弱 KC,
   且 hint0-2 的结构性禁答令 (F4-A1) 不受画像影响。
"""

import json

from eduagents_api.routers.plans import _diagnosis_from_rows, _kc_order_from_edges
from eduagents_core.config import Settings
from eduagents_core.gateway.base import ChatRequest, ChatResult
from eduagents_core.graphs.plan_graph import build_plan_graph
from eduagents_core.graphs.state import GraphDeps
from eduagents_core.graphs.tutor_graph import build_tutor_graph
from langgraph.checkpoint.memory import MemorySaver


class FakeGateway:
    """脚本化网关: 按 feature 排队出词 (末项粘性可复用), 记录全部请求供断言 prompt。"""

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


def make_deps(gateway) -> GraphDeps:
    return GraphDeps(settings=Settings(_env_file=None), gateway=gateway)


# ---------------------------------------------------------------- ① 真实掌握度注入 (G1)

PARSE = {"subject": "高等数学", "key_topics": ["极限", "导数"], "level_hint": "零基础"}
DRAFT = {
    "stages": [
        {
            "name": "夯实基础",
            "days": [
                {"day": 1, "tasks": [{"title": "精读极限", "minutes": 60, "kc_names": ["极限"], "resource_ref": None}]},
                {"day": 2, "tasks": [{"title": "练习导数", "minutes": 60, "kc_names": ["导数"], "resource_ref": None}]},
            ],
        }
    ]
}


async def run_plan_to_interrupt(script: dict, input_overrides: dict | None = None):
    gateway = FakeGateway(script)
    graph = build_plan_graph(make_deps(gateway)).compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "plan-p1"}}
    payload = {
        "tenant_id": "t-1",
        "user_id": "u-1",
        "request_id": "r-1",
        "kb_id": "",
        "goal": "两周入门高等数学",
        "budget_hours_per_day": 1.0,
        "total_days": 2,
    }
    payload.update(input_overrides or {})
    state = await graph.ainvoke(payload, config)
    return gateway, state


async def test_diagnose_llm_only_backfills_kcs_without_records():
    """有真实 BKT 记录的 KC ("极限") 不再进 LLM 摸底 prompt, LLM 只兜底无记录的 "导数";
    即使 LLM 越权回传了有记录 KC 的估值, 也不得覆盖真实值。"""
    gateway, state = await run_plan_to_interrupt(
        {
            "plan_parse_goal": [PARSE],
            # LLM 越权回传 "极限": 0.05 (真实值 0.72 不得被覆盖)
            "plan_diagnose": [{"diagnosis": {"极限": 0.05, "导数": 0.4}}],
            "plan_draft": [DRAFT],
        },
        input_overrides={"diagnosis": {"极限": 0.72}},
    )
    diag_reqs = [r for r in gateway.requests if r.feature == "plan_diagnose"]
    assert len(diag_reqs) == 1
    prompt = diag_reqs[0].messages[0].content
    assert "导数" in prompt  # 无记录主题仍走 LLM 保守摸底
    assert "极限" not in prompt  # 有真实记录的 KC 不再让 LLM 猜
    # 真实值保留, LLM 值只落在无记录主题上
    assert state["diagnosis"] == {"极限": 0.72, "导数": 0.4}
    events = [e for e in state["trace"] if e.get("node") == "diagnose"]
    assert events[0]["event"] == "external_plus_conservative"
    assert events[0]["known_count"] == 1 and events[0]["backfilled_count"] == 1


async def test_diagnose_full_external_coverage_skips_llm_entirely():
    """真实掌握度覆盖全部主题: 完全不调 plan_diagnose LLM。"""
    gateway, state = await run_plan_to_interrupt(
        {"plan_parse_goal": [PARSE], "plan_draft": [DRAFT]},
        input_overrides={"diagnosis": {"极限": 0.72, "导数": 0.18}},
    )
    assert all(r.feature != "plan_diagnose" for r in gateway.requests)
    assert state["diagnosis"] == {"极限": 0.72, "导数": 0.18}
    assert any(e.get("event") == "external_diagnosis_used" for e in state["trace"])


# ---------------------------------------------------------------- ② 先修 DAG 拓扑约束 (G2)


def test_kc_order_from_edges_topological_and_constrained_only():
    """先修边 -> KC 名拓扑序; 无先修约束的 KC 不进序 (其任务顺序不被人为强排)。"""
    # names_by_id 插入顺序 (查询按 created_at) 故意与拓扑序相反
    names_by_id = {"i3": "积分", "i2": "导数", "i0": "三角函数", "i1": "极限"}
    edge_pairs = [("i2", "i3"), ("i1", "i2")]  # 导数->积分, 极限->导数
    order = _kc_order_from_edges(names_by_id, edge_pairs)
    assert order == ["极限", "导数", "积分"]
    assert "三角函数" not in order


def test_kc_order_from_edges_empty_without_edges():
    assert _kc_order_from_edges({"i1": "极限", "i2": "导数"}, []) == []
    # 自环/端点缺失的脏边被忽略
    assert _kc_order_from_edges({"i1": "极限"}, [("i1", "i1"), ("i1", "ghost")]) == []


def test_diagnosis_from_rows_kb_scope_with_fallback():
    rows = [("极限", 0.2, "kb-a"), ("导数", 0.9, "kb-b")]
    assert _diagnosis_from_rows(rows, "kb-a") == {"极限": 0.2}
    assert _diagnosis_from_rows(rows, "") == {"极限": 0.2, "导数": 0.9}
    # 绑库过滤后为空 -> 回退全量 (跨库学习者不因绑库丢真实掌握度)
    assert _diagnosis_from_rows(rows, "kb-c") == {"极限": 0.2, "导数": 0.9}


PARSE3 = {"subject": "高等数学", "key_topics": ["极限", "导数", "积分"], "level_hint": ""}
# 乱序草稿: 积分(day1) -> 导数(day2) -> 极限(day3), 与先修链 极限->导数->积分 完全颠倒
TOPO_DRAFT = {
    "stages": [
        {
            "name": "违序",
            "days": [
                {"day": 1, "tasks": [{"title": "先学积分", "minutes": 60, "kc_names": ["积分"], "resource_ref": None}]},
                {"day": 2, "tasks": [{"title": "再学导数", "minutes": 60, "kc_names": ["导数"], "resource_ref": None}]},
                {"day": 3, "tasks": [{"title": "最后极限", "minutes": 60, "kc_names": ["极限"], "resource_ref": None}]},
            ],
        }
    ]
}


async def test_no_task_before_unmastered_prerequisite():
    """F3-A2 口径: 注入先修拓扑序后, 修正后的计划中无任务先于其未掌握前置 KC。"""
    edge_pairs = [("i1", "i2"), ("i2", "i3")]  # 极限->导数, 导数->积分
    names_by_id = {"i3": "积分", "i2": "导数", "i1": "极限"}
    kc_order = _kc_order_from_edges(names_by_id, edge_pairs)
    _, state = await run_plan_to_interrupt(
        {"plan_parse_goal": [PARSE3], "plan_draft": [TOPO_DRAFT]},
        input_overrides={
            "total_days": 3,
            # 三个 KC 全部未掌握 (真实 BKT 低值) -> 前置约束必须生效
            "diagnosis": {"极限": 0.2, "导数": 0.25, "积分": 0.1},
            "kc_order": kc_order,
        },
    )
    tasks = [t for stage in state["plan"]["stages"] for day in stage["days"] for t in day["tasks"]]
    first_idx = {}
    for i, task in enumerate(tasks):
        for name in task["kc_names"]:
            first_idx.setdefault(name, i)
    edges_by_name = [("极限", "导数"), ("导数", "积分")]
    for frm, to in edges_by_name:
        for i, task in enumerate(tasks):
            if to in task["kc_names"]:
                assert first_idx[frm] < i, f"{to} 的任务出现在其未掌握前置 {frm} 之前: {tasks}"
    deferred = [
        e
        for e in state["trace"]
        if e.get("node") == "draft_plan" and e.get("event") == "fix" and e.get("fix") == "kc_order_deferred"
    ]
    assert len(deferred) == 1


# ---------------------------------------------------------------- ③ tutor 画像注入 (G9)

BAN = "绝对禁止给出最终答案"  # F4-A1 结构性禁令 (代码注入 system prompt)
PROFILE_HEADER = "学生画像"
OPENING_MARK = "这正是你之前卡过的"
PROFILE_SUMMARY = "该学生已掌握: 三角函数; 薄弱点: 导数定义(较弱)、极值判定(待巩固); 近期错因偏向: 概念混淆。"
WEAK_POINTS = ["导数定义", "极值判定"]
HIT_PROBLEM = "用导数定义求 f(x)=x^2 在 x=1 处的导数"  # 命中薄弱 KC "导数定义"
MISS_PROBLEM = "求 1+2+...+100 的和"  # 不涉及任何薄弱 KC


def classify(stuck="concept", asked=False, solved=False) -> dict:
    return {"stuck_type": stuck, "asked_for_answer": asked, "solved": solved}


def make_tutor_graph():
    gateway = FakeGateway({"tutor_classify": [classify()], "tutor_respond": ["回复"]})
    graph = build_tutor_graph(make_deps(gateway)).compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "tutor-p1"}}
    return gateway, graph, config


async def run_tutor_round(graph, config, text: str, *, problem=HIT_PROBLEM, summary=PROFILE_SUMMARY):
    return await graph.ainvoke(
        {
            "tenant_id": "t-1",
            "user_id": "u-1",
            "request_id": "r-1",
            "problem": problem,
            "messages": [{"role": "user", "content": text}],
            "profile_summary": summary,
            "profile_weak_points": WEAK_POINTS if summary else [],
        },
        config,
    )


def respond_systems(gateway: FakeGateway) -> list[str]:
    """每次 respond 调用送入网关的 system prompt (按轮次顺序)。"""
    return [
        req.messages[0].content
        for req in gateway.requests
        if req.feature == "tutor_respond" and req.messages[0].role == "system"
    ]


async def test_tutor_hint0_to_3_receive_profile_and_ban_unaffected():
    """四级 hint prompt 都实收画像段; hint0-2 禁答令仍在且压轴, hint3 照常无禁令。"""
    gateway, graph, config = make_tutor_graph()
    state = {}
    for text in ["第1轮", "第2轮", "第3轮", "第4轮"]:
        state = await run_tutor_round(graph, config, text)
    assert state["hint_level"] == 3
    systems = respond_systems(gateway)
    assert len(systems) == 4
    # 画像段实收: 摘要原文 + 措辞纪律 (仅调讲法, 不降苏格拉底约束)
    assert all(PROFILE_HEADER in s and PROFILE_SUMMARY in s for s in systems)
    assert all("不得因画像提前给出答案" in s for s in systems)
    # F4-A1 不受画像影响: hint0-2 禁令都在且位于画像段之后 (禁答令压轴), hint3 无禁令
    assert all(BAN in s for s in systems[:3])
    assert all(s.rfind(BAN) > s.rfind(PROFILE_HEADER) for s in systems[:3])
    assert BAN not in systems[3]


async def test_tutor_opening_names_weak_kc_only_on_first_round():
    """开场轮题目命中薄弱 KC -> 主动点明 "这正是你之前卡过的X"; 后续轮不再重复。"""
    gateway, graph, config = make_tutor_graph()
    state = await run_tutor_round(graph, config, "这道题我不会")
    await run_tutor_round(graph, config, "还是没思路")
    systems = respond_systems(gateway)
    assert OPENING_MARK in systems[0] and "导数定义" in systems[0]
    assert OPENING_MARK not in systems[1]  # 关联指令只在开场轮
    replied = [e for e in state["trace"] if e.get("node") == "respond"]
    assert replied[0]["profile_injected"] is True
    assert replied[0]["weak_opening_kc"] == "导数定义"


async def test_tutor_opening_no_callout_when_problem_misses_weak_kc():
    """题目不涉及薄弱 KC: 画像段仍注入, 但不硬造开场关联。"""
    gateway, graph, config = make_tutor_graph()
    state = await run_tutor_round(graph, config, "不会做", problem=MISS_PROBLEM)
    systems = respond_systems(gateway)
    assert PROFILE_HEADER in systems[0]
    assert OPENING_MARK not in systems[0]
    replied = [e for e in state["trace"] if e.get("node") == "respond"]
    assert replied[0]["weak_opening_kc"] == ""


async def test_tutor_empty_profile_leaves_no_section_or_placeholder():
    """无画像用户: 不注入画像段, 占位符也不残留, 禁令照常。"""
    gateway, graph, config = make_tutor_graph()
    await run_tutor_round(graph, config, "不会做", summary="")
    system = respond_systems(gateway)[0]
    assert PROFILE_HEADER not in system
    assert "{{PROFILE_SECTION}}" not in system
    assert BAN in system


# ---------- ④ 审查修复轮: 脱敏出境 / 相关性过滤 / 归一化匹配 / 保守合并 ----------


def test_render_diagnosis_qualitative_no_decimals():
    """P2-7: DIAGNOSIS 出境只给定性档, 精确概率与小数一律不出现。"""
    from eduagents_core.graphs.plan_graph import _render_diagnosis

    text = _render_diagnosis({"导数定义": 0.24, "泰勒公式": 0.92, "极值判定": 0.31}, ["导数"])
    assert "导数定义: 较弱" in text
    assert "极值判定: 待巩固" in text
    assert "泰勒公式: 已掌握" in text
    for decimal in ("0.24", "0.31", "0.92", "0."):
        assert decimal not in text


def test_render_diagnosis_relevance_first_and_capped():
    """目标相关条目排最前; 超过 20 条截断 (防跨学科全量膨胀 prompt)。"""
    from eduagents_core.graphs.plan_graph import _DIAGNOSIS_LIMIT, _render_diagnosis

    diagnosis = {f"无关KC{i:02d}": 0.1 + i * 0.01 for i in range(25)}
    diagnosis["导数定义"] = 0.9  # 相关但掌握度高: 仍须排最前
    lines = _render_diagnosis(diagnosis, ["导数"]).splitlines()
    assert len(lines) == _DIAGNOSIS_LIMIT
    assert lines[0] == "- 导数定义: 已掌握"


def test_fallback_plan_ignores_unrelated_mastery():
    """降级计划以目标主题为准, 跨学科掌握度记录不得污染 (审查 P2 场景: 日语计划出现导数)。"""
    from eduagents_core.graphs.plan_graph import _fallback_plan

    state = {
        "goal": "两周入门日语",
        "parsed_goal": {"key_topics": ["五十音", "基础语法"]},
        "diagnosis": {"导数定义": 0.24, "泰勒公式": 0.92},
    }
    plan = _fallback_plan(state, total_days=4, budget_minutes=60)
    titles = [t["title"] for d in plan["stages"][0]["days"] for t in d["tasks"]]
    assert all("导数" not in t and "泰勒" not in t for t in titles)
    assert any("五十音" in t for t in titles)


def test_fallback_plan_keeps_related_mastery_topics():
    """目标相关的掌握度 KC 仍进降级计划轮转池 (绑库数学计划不丢 KC 覆盖)。"""
    from eduagents_core.graphs.plan_graph import _fallback_plan

    state = {
        "goal": "补齐微积分",
        "parsed_goal": {"key_topics": ["导数"]},
        "diagnosis": {"导数定义": 0.24},
    }
    plan = _fallback_plan(state, total_days=2, budget_minutes=60)
    titles = [t["title"] for d in plan["stages"][0]["days"] for t in d["tasks"]]
    assert any("导数定义" in t for t in titles)


def test_fix_kc_order_matches_normalized_variants():
    """真实模型输出全角/空格/大小写变体时先修约束仍生效 (匹配走 normalize_kc_name)。"""
    from eduagents_core.graphs.plan_graph import _fix_kc_order

    plan = {
        "stages": [
            {
                "name": "s",
                "days": [
                    {"day": 1, "tasks": [{"title": "后置", "minutes": 30, "kc_names": ["泰勒 公式"], "resource_ref": None}]},
                    {"day": 2, "tasks": [{"title": "前置", "minutes": 30, "kc_names": ["导数定义"], "resource_ref": None}]},
                ],
            }
        ]
    }
    fixes: list[dict] = []
    _fix_kc_order(plan, ["导数定义", "泰勒公式"], fixes)
    days = plan["stages"][0]["days"]
    assert days[0]["tasks"][0]["kc_names"] == ["导数定义"]
    assert any(f["fix"] == "kc_order_deferred" for f in fixes)


def test_fix_kc_order_unmatched_is_visible():
    """先修序一个任务都没挂上: 必须写 kc_order_unmatched, 不许静默失效 (F3-A2 可审计)。"""
    from eduagents_core.graphs.plan_graph import _fix_kc_order

    plan = {
        "stages": [
            {
                "name": "s",
                "days": [
                    {"day": 1, "tasks": [{"title": "无关", "minutes": 30, "kc_names": ["五十音"], "resource_ref": None}]},
                ],
            }
        ]
    }
    fixes: list[dict] = []
    _fix_kc_order(plan, ["导数定义", "泰勒公式"], fixes)
    assert fixes == [{"fix": "kc_order_unmatched", "kc_count": 2}]


def test_diagnosis_from_rows_conservative_on_name_collision():
    """跨库同名 KC 取保守低值 (宁可多排复习, 不可乐观跳过)。"""
    rows = [("导数定义", 0.3, "kb-a"), ("导数定义", 0.7, "kb-b")]
    assert _diagnosis_from_rows(rows, "") == {"导数定义": 0.3}
