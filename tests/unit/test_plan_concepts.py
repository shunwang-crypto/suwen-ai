"""计划临时概念（0034）的契约与模块边界守卫。

最重要的一条不是"功能对不对"，而是**边界**：临时概念绝不能出现在掌握度 / BKT /
证据 / 推荐 / 报告的任何路径上。所以除了纯逻辑测试，这里还有一条静态守卫，
扫生产代码确认没人把 PlanConcept 拿去参与掌握度写入。
"""

from __future__ import annotations

import re
from pathlib import Path

from eduagents_api.plan_concepts import (
    MAX_PLAN_CONCEPTS,
    collect_plan_concepts,
)

API_ROOT = Path(__file__).resolve().parents[2] / "apps/api/src/eduagents_api"

#: 掌握度 / 证据 / 推荐链路的模块。临时概念不许出现在这些文件里。
MASTERY_PATH_FILES = (
    "profile_summary.py",
    "practice_recommendation.py",
    "plan_adjust.py",
    "routers/next_actions.py",
    "routers/reports.py",
    "routers/quiz.py",
    "routers/reviews.py",
)


def _structure(*kc_name_groups: list[str]) -> dict:
    return {
        "stages": [
            {
                "name": "阶段",
                "days": [
                    {
                        "day": index + 1,
                        "tasks": [{"title": f"任务 {index}", "kc_names": names}],
                    }
                    for index, names in enumerate(kc_name_groups)
                ],
            }
        ]
    }


def test_names_matching_a_real_kc_are_not_provisional():
    """有正式 KC 承接的概念不进临时层（否则会和真 KC 重复展示）。"""
    result = collect_plan_concepts(_structure(["泰勒公式", "自研概念"]), ["泰勒公式"])
    assert [item["display_name"] for item in result] == ["自研概念"]


def test_matching_is_normalization_insensitive():
    """全角/空格/大小写差异不该造出一条重复的临时概念。"""
    result = collect_plan_concepts(_structure(["Taylor 公式"]), ["taylor公式"])
    assert result == []


def test_duplicates_within_a_plan_collapse_by_normalized_name():
    result = collect_plan_concepts(_structure(["动态规划"], ["动态 规划"]), [])
    assert [item["display_name"] for item in result] == ["动态规划"]


def test_order_follows_first_appearance():
    result = collect_plan_concepts(_structure(["甲"], ["乙"], ["丙"]), [])
    assert [item["display_name"] for item in result] == ["甲", "乙", "丙"]


def test_blank_and_non_string_names_are_ignored():
    result = collect_plan_concepts(
        {"stages": [{"days": [{"tasks": [{"kc_names": ["  ", None, 42, "有效"]}]}]}]},
        [],
    )
    assert [item["display_name"] for item in result] == ["有效"]


def test_malformed_structure_is_safe():
    assert collect_plan_concepts(None, []) == []
    assert collect_plan_concepts({}, []) == []
    assert collect_plan_concepts({"stages": "坏数据"}, []) == []
    assert collect_plan_concepts({"stages": [{"days": [{"tasks": ["坏任务"]}]}]}, []) == []


def test_concept_count_is_capped():
    many = _structure(*[[f"概念 {index}"] for index in range(MAX_PLAN_CONCEPTS + 10)])
    assert len(collect_plan_concepts(many, [])) == MAX_PLAN_CONCEPTS


def test_no_kb_plan_still_surfaces_its_concepts():
    """无资料计划的全部概念都是临时概念 —— 这正是它当初空转的那部分。"""
    result = collect_plan_concepts(_structure(["Trie", "前缀和"]), [])
    assert [item["display_name"] for item in result] == ["Trie", "前缀和"]


# ---- 模块边界：临时概念不得进入掌握度/证据路径 ----


def test_plan_concepts_never_appear_in_mastery_or_evidence_code():
    offenders = []
    for relative in MASTERY_PATH_FILES:
        path = API_ROOT / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bPlanConcept\b|plan_concepts", text):
            offenders.append(relative)
    assert not offenders, (
        f"临时概念出现在掌握度/证据链路: {offenders}；"
        "它们不是知识点，只能用于计划展示与归并候选"
    )


def test_mastery_write_boundary_only_accepts_real_kc_ids():
    """掌握度唯一写入口的签名仍然只吃 kc_ids（正式 KC 外键），没有临时概念入口。"""
    import inspect

    from eduagents_api.services import apply_mastery_evidence

    params = inspect.signature(apply_mastery_evidence).parameters
    assert "kc_ids" in params
    assert not any("concept" in name for name in params)


def test_plan_concept_model_has_no_kc_foreign_key():
    """刻意不给 kc_id 外键：从模型层面就无法混进 KC 关系。"""
    from eduagents_api.models import PlanConcept

    columns = {column.name for column in PlanConcept.__table__.columns}
    assert "kc_id" not in columns
    foreign_targets = {
        fk.column.table.name for column in PlanConcept.__table__.columns for fk in column.foreign_keys
    }
    assert foreign_targets == {"plans"}
