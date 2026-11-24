"""计划个性化 (P1-3) 与 tutor 画像注入 (P1-4) 单测: FakeGateway 脚本化, 无网络无库。

覆盖三组验收:
① 证据语义注入 (G1): 只有 observed KC 带定性掌握档，未知主题安排基线练习，
   diagnose 节点绝不调用 LLM 猜概率;
② 先修 DAG 拓扑约束 (G2, F3-A2 口径): 先修边 -> 拓扑序注入 kc_order 后,
   修正后的计划中无任务先于其未掌握前置 KC;
③ tutor 画像注入 (G9): 四级 hint prompt 实收画像段, 开场关联薄弱 KC,
   且 hint0-2 的结构性禁答令 (F4-A1) 不受画像影响。
"""

import json

import pytest
from eduagents_api.routers.plans import (
    PlanCreate,
    _diagnosis_from_rows,
    _kc_order_from_edges,
    _qualitative_weak_kcs,
)
from eduagents_core.config import Settings
from eduagents_core.education.context import EducationDeps
from eduagents_core.education.plan import PlanTools, normalize_goal, verified_diagnosis
from eduagents_core.education.tutor import TutorTools
from eduagents_core.gateway.base import ChatRequest, ChatResult
from eduagents_core.harness.task import TaskRecord
from pydantic import ValidationError


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


def make_deps(gateway) -> EducationDeps:
    return EducationDeps(settings=Settings(_env_file=None), gateway=gateway)


PARSE = {"subject": "高等数学", "key_topics": ["极限", "导数"], "level_hint": "零基础"}
DRAFT = {
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
                            "resource_ref": None,
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


def test_kc_order_from_edges_topological_and_constrained_only():
    """先修边 -> KC 名拓扑序; 无先修约束的 KC 不进序 (其任务顺序不被人为强排)。"""
    names_by_id = {"i3": "积分", "i2": "导数", "i0": "三角函数", "i1": "极限"}
    edge_pairs = [("i2", "i3"), ("i1", "i2")]
    order = _kc_order_from_edges(names_by_id, edge_pairs)
    assert order == ["极限", "导数", "积分"]
    assert "三角函数" not in order


def test_kc_order_from_edges_empty_without_edges():
    assert _kc_order_from_edges({"i1": "极限", "i2": "导数"}, []) == []
    assert _kc_order_from_edges({"i1": "极限"}, [("i1", "i1"), ("i1", "ghost")]) == []


def test_diagnosis_from_rows_uses_distinct_evidence_state():
    rows = [("极限", 0.2, 3), ("导数", 0.9, 2), ("积分", 0.3, 0)]
    assert _diagnosis_from_rows(rows) == {
        "极限": {"evidence_state": "observed", "mastery_band": "较弱"},
        "导数": {"evidence_state": "estimated"},
        "积分": {"evidence_state": "unobserved"},
    }


PARSE3 = {"subject": "高等数学", "key_topics": ["极限", "导数", "积分"], "level_hint": ""}
TOPO_DRAFT = {
    "stages": [
        {
            "name": "违序",
            "days": [
                {
                    "day": 1,
                    "tasks": [
                        {
                            "title": "先学积分",
                            "minutes": 60,
                            "kc_names": ["积分"],
                            "resource_ref": None,
                        }
                    ],
                },
                {
                    "day": 2,
                    "tasks": [
                        {
                            "title": "再学导数",
                            "minutes": 60,
                            "kc_names": ["导数"],
                            "resource_ref": None,
                        }
                    ],
                },
                {
                    "day": 3,
                    "tasks": [
                        {
                            "title": "最后极限",
                            "minutes": 60,
                            "kc_names": ["极限"],
                            "resource_ref": None,
                        }
                    ],
                },
            ],
        }
    ]
}
BAN = "绝对禁止给出最终答案"
PROFILE_HEADER = "学生画像"
OPENING_MARK = "这正是你之前卡过的"
PROFILE_SUMMARY = (
    "该学生已掌握: 三角函数; 薄弱点: 导数定义(较弱)、极值判定(待巩固); 近期错因偏向: 概念混淆。"
)
WEAK_POINTS = ["导数定义", "极值判定"]
HIT_PROBLEM = "用导数定义求 f(x)=x^2 在 x=1 处的导数"
MISS_PROBLEM = "求 1+2+...+100 的和"


def classify(stuck="concept", asked=False, solved=False) -> dict:
    return {"stuck_type": stuck, "asked_for_answer": asked, "solved": solved}


def respond_systems(gateway: FakeGateway) -> list[str]:
    """每次 respond 调用送入网关的 system prompt (按轮次顺序)。"""
    return [
        req.messages[0].content
        for req in gateway.requests
        if req.feature == "tutor_respond" and req.messages[0].role == "system"
    ]


def test_render_diagnosis_qualitative_no_decimals():
    """P2-7: DIAGNOSIS 出境只给定性档, 精确概率与小数一律不出现。"""
    from eduagents_core.education.plan import _render_diagnosis

    text = _render_diagnosis(
        {
            "导数定义": {"evidence_state": "observed", "mastery_band": "较弱"},
            "泰勒公式": {"evidence_state": "observed", "mastery_band": "已掌握"},
            "极值判定": {"evidence_state": "estimated"},
        },
        ["导数"],
        ["级数"],
    )
    assert "导数定义: 较弱（真实作答证据充分）" in text
    assert "极值判定: 证据不足，先安排短验证" in text
    assert "泰勒公式: 已掌握（真实作答证据充分）" in text
    assert "级数: 尚未验证，先安排基线练习" in text
    for decimal in ("0.24", "0.31", "0.92", "0."):
        assert decimal not in text


def test_render_diagnosis_relevance_first_and_capped():
    """目标相关条目排最前; 超过 20 条截断 (防跨学科全量膨胀 prompt)。"""
    from eduagents_core.education.plan import _DIAGNOSIS_LIMIT, _render_diagnosis

    diagnosis = {
        f"无关KC{i:02d}": {"evidence_state": "observed", "mastery_band": "待巩固"}
        for i in range(25)
    }
    diagnosis["导数定义"] = {"evidence_state": "observed", "mastery_band": "已掌握"}
    lines = _render_diagnosis(diagnosis, ["导数"]).splitlines()
    assert len(lines) == _DIAGNOSIS_LIMIT
    assert lines[0] == "- 导数定义: 已掌握（真实作答证据充分）"


def test_fallback_plan_ignores_unrelated_mastery():
    """降级计划以目标主题为准, 跨学科掌握度记录不得污染 (审查 P2 场景: 日语计划出现导数)。"""
    from eduagents_core.education.plan import _fallback_plan

    state = {
        "goal": "两周入门日语",
        "parsed_goal": {"key_topics": ["五十音", "基础语法"]},
        "diagnosis": {
            "导数定义": {"evidence_state": "observed", "mastery_band": "较弱"},
            "泰勒公式": {"evidence_state": "observed", "mastery_band": "已掌握"},
        },
    }
    plan = _fallback_plan(state, total_days=4, budget_minutes=60)
    titles = [t["title"] for d in plan["stages"][0]["days"] for t in d["tasks"]]
    assert all("导数" not in t and "泰勒" not in t for t in titles)
    assert any("五十音" in t for t in titles)


def test_fallback_plan_keeps_related_mastery_topics():
    """目标相关的掌握度 KC 仍进降级计划轮转池 (绑库数学计划不丢 KC 覆盖)。"""
    from eduagents_core.education.plan import _fallback_plan

    state = {
        "goal": "补齐微积分",
        "parsed_goal": {"key_topics": ["导数"]},
        "diagnosis": {"导数定义": {"evidence_state": "observed", "mastery_band": "较弱"}},
    }
    plan = _fallback_plan(state, total_days=2, budget_minutes=60)
    titles = [t["title"] for d in plan["stages"][0]["days"] for t in d["tasks"]]
    assert any("导数定义" in t for t in titles)


def test_fix_kc_order_matches_normalized_variants():
    """真实模型输出全角/空格/大小写变体时先修约束仍生效 (匹配走 normalize_kc_name)。"""
    from eduagents_core.education.plan import _fix_kc_order

    plan = {
        "stages": [
            {
                "name": "s",
                "days": [
                    {
                        "day": 1,
                        "tasks": [
                            {
                                "title": "后置",
                                "minutes": 30,
                                "kc_names": ["泰勒 公式"],
                                "resource_ref": None,
                            }
                        ],
                    },
                    {
                        "day": 2,
                        "tasks": [
                            {
                                "title": "前置",
                                "minutes": 30,
                                "kc_names": ["导数定义"],
                                "resource_ref": None,
                            }
                        ],
                    },
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
    from eduagents_core.education.plan import _fix_kc_order

    plan = {
        "stages": [
            {
                "name": "s",
                "days": [
                    {
                        "day": 1,
                        "tasks": [
                            {
                                "title": "无关",
                                "minutes": 30,
                                "kc_names": ["五十音"],
                                "resource_ref": None,
                            }
                        ],
                    }
                ],
            }
        ]
    }
    fixes: list[dict] = []
    _fix_kc_order(plan, ["导数定义", "泰勒公式"], fixes)
    assert fixes == [{"fix": "kc_order_unmatched", "kc_count": 2}]


def test_diagnosis_from_rows_conservative_on_name_collision():
    """同库脏数据里的同名 KC 取更弱档；证据不足优先于乐观结论。"""
    rows = [("导数定义", 0.7, 3), ("导数定义", 0.3, 3)]
    assert _diagnosis_from_rows(rows) == {
        "导数定义": {"evidence_state": "observed", "mastery_band": "待巩固"}
    }
    assert _diagnosis_from_rows([*rows, ("导数定义", 0.9, 1)]) == {
        "导数定义": {"evidence_state": "estimated"}
    }


def test_skill_gap_analysis_normalizes_sorts_deduplicates_and_stays_qualitative():
    from eduagents_core.education.plan import skill_gap_analysis

    skills = [
        {"name": "极限 运算", "required_level": 2, "reason": "是导数基础"},
        {"name": "导数", "required_level": 3, "reason": "用于解决极值问题"},
        {"name": "ＤＡＴＡ", "required_level": 1, "reason": "英语大小写归一化"},
        {"name": "导数", "required_level": 1, "reason": "重复项不应重复输出"},
        {"name": "矩阵", "required_level": 3, "reason": "目标所需但并非薄弱点"},
    ]
    weak = [
        {"name": "data", "zone": "待巩固", "p_mastery": 0.55},
        {"name": "导数", "zone": "较弱", "kc_id": "secret-id"},
        {"name": "极限运算", "zone": "待巩固"},
    ]
    gaps = skill_gap_analysis(skills, weak)
    assert [item["skill"] for item in gaps] == ["导数", "极限 运算", "ＤＡＴＡ"]
    assert gaps[0]["required_level"] == 3
    assert all(item["zone"] in {"较弱", "待巩固"} for item in gaps)
    assert all("p_mastery" not in item and "kc_id" not in item for item in gaps)
    assert all("0.55" not in item["gap_reason"] for item in gaps)


def test_skill_gap_analysis_rejects_non_intersection_and_invalid_level():
    from eduagents_core.education.plan import skill_gap_analysis

    assert skill_gap_analysis([], [{"name": "导数", "zone": "较弱"}]) == []
    assert skill_gap_analysis([{"name": "矩阵", "required_level": 3}], []) == []
    assert (
        skill_gap_analysis(
            [{"name": "导数", "required_level": 99}], [{"name": "导数", "zone": "待巩固"}]
        )[0]["required_level"]
        == 2
    )


def test_qualitative_weak_kcs_removes_internal_fields_and_normalizes_duplicates():
    items = [
        {
            "name": "ＤＡＴＡ",
            "zone": "较弱",
            "p_mastery": 0.12,
            "kc_id": "internal",
            "recent_error": True,
        },
        {"name": "data", "zone": "待巩固"},
        {"name": "已掌握项", "zone": "已掌握"},
    ]
    assert _qualitative_weak_kcs(items) == [{"name": "ＤＡＴＡ", "zone": "较弱"}]


def test_fallback_plan_prioritizes_skill_gaps_without_creating_mastery():
    from eduagents_core.education.plan import _fallback_plan

    state = {
        "goal": "补齐微积分",
        "parsed_goal": {"key_topics": ["微积分"]},
        "diagnosis": {"导数": {"evidence_state": "observed", "mastery_band": "较弱"}},
        "skill_gaps": [{"skill": "导数", "required_level": 3, "zone": "较弱"}],
    }
    before = dict(state["diagnosis"])
    plan = _fallback_plan(state, total_days=2, budget_minutes=60)
    first_task = plan["stages"][0]["days"][0]["tasks"][0]
    assert "导数" in first_task["title"]
    assert state["diagnosis"] == before


def test_plan_create_trims_optional_goal_context():
    body = PlanCreate(
        goal="  极限与导数  ",
        plan_mode="goal_only",
        current_level="  会基本代数  ",
        target_outcome="  能独立完成基础题  ",
        success_criteria="  10 题正确 8 题  ",
        budget_hours_per_day=1.0,
        total_days=2,
    )
    assert body.goal == "极限与导数"
    assert body.current_level == "会基本代数"
    assert body.target_outcome == "能独立完成基础题"
    assert body.success_criteria == "10 题正确 8 题"
    assert (
        PlanCreate(
            goal="导数",
            plan_mode="goal_only",
            current_level="  ",
            budget_hours_per_day=1.0,
            total_days=2,
        ).current_level
        is None
    )


def test_plan_create_requires_explicit_schedule_values():
    with pytest.raises(ValidationError):
        PlanCreate(goal="导数", plan_mode="goal_only", current_level="  ")


@pytest.mark.parametrize(
    "evidence,band", [("unobserved", None), ("estimated", None), ("observed", "较弱")]
)
def test_learning_context_removes_probabilities_and_unknown_evidence(evidence, band):
    state = {
        "parsed_goal": PARSE,
        "diagnosis": {
            "极限": {"evidence_state": evidence, "mastery_band": "较弱", "p_mastery": 0.22},
            "伪造主题": {"p_mastery": 1},
        },
    }
    checked = verified_diagnosis(state)
    assert "p_mastery" not in json.dumps(checked)
    assert "伪造主题" not in checked["diagnosis"]
    assert checked["diagnosis"]["极限"].get("mastery_band") == band
    assert ("极限" in checked["baseline_topics"]) == (evidence != "observed")


@pytest.mark.parametrize("level", range(4))
def test_tutor_profile_never_overrides_answer_permission(level):
    record = TaskRecord(
        state={
            "problem": "导数定义是什么",
            "hint_level": level,
            "profile_summary": "导数定义较弱",
            "profile_weak_points": ["导数定义"],
        }
    )
    policy = TutorTools(make_deps(FakeGateway()), record, lambda _: None).policy()
    assert "导数定义较弱" in policy
    assert ("绝对禁止给出最终答案" in policy) == (level < 3)


async def test_qualitative_learning_context_has_no_internal_ids_or_probability():
    record = TaskRecord(
        state={
            "weak_kcs": [{"name": "导数", "zone": "较弱", "p_mastery": 0.2, "kc_id": "private"}],
            "diagnosis": {
                "导数": {"evidence_state": "observed", "mastery_band": "较弱", "p_mastery": 0.2}
            },
        },
        status="running",
    )
    context = await PlanTools(make_deps(FakeGateway()), record, lambda _: None).handlers[
        "read_learning_context"
    ]({})
    assert "private" not in json.dumps(context) and "p_mastery" not in json.dumps(context)
    assert context["weak_kcs"] == [{"name": "导数", "zone": "较弱"}]


def test_explicit_goal_context_wins_over_model_and_skill_gap_is_not_mastery():
    state = {
        "goal": "导数",
        "current_level": "会代数",
        "target_outcome": "能解基础题",
        "success_criteria": "十题对八题",
        "weak_kcs": [{"name": "导数", "zone": "较弱"}],
    }
    result = normalize_goal(
        state,
        {
            "key_topics": ["导数"],
            "level_hint": "精通",
            "skills": [{"name": "导数", "required_level": 3}],
        },
    )
    assert result["parsed_goal"]["level_hint"] == "会代数"
    assert result["skill_gaps"] and "diagnosis" not in result
    assert result["goal_analysis"]["outcomes"] == ["能解基础题"]
