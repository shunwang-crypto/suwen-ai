"""计划里的临时概念（0034）：纯逻辑，零 DB。

职责边界只有一条，但必须说死：**临时概念不是知识点**。

计划任务上的 ``kc_names`` 里，凡是匹配不到本库正式 ``knowledge_components`` 的，
就是模型给的自然语言概念 —— 它可能不是稳定知识点、可能与之后 enrich 出来的 KC
重复、它的先修关系是猜的。这些东西：

- **可以**用于计划展示（让学生知道这份计划打算覆盖什么）与将来的归并候选；
- **不可以**进入掌握度 / BKT / 证据 / 推荐 / 成长报告的任何路径 —— 那些一律只认
  正式 KC 的外键（守卫见 ``tests/unit/test_plan_concepts.py``）。

v1 刻意不做的两件事：不存先修边（模型猜的关系一旦画出来就会被当成真实课程结构），
不自动升级为正式 KC（升级只能经资料归并或用户确认，属后续波次）。
"""

from __future__ import annotations

from typing import Any

from eduagents_core.kc.types import normalize_kc_name

#: 单个计划里保留的临时概念上限（避免一份计划刷出几百条噪声）。
MAX_PLAN_CONCEPTS = 60
NAME_MAX = 255


def collect_plan_concepts(
    structure: dict | None, known_kc_names: list[str] | set[str]
) -> list[dict[str, str]]:
    """从计划结构里挑出"没有正式 KC 承接"的概念名。

    :param structure: 计划 ``structure_json``（stages → days → tasks → kc_names）。
    :param known_kc_names: 本库正式 KC 的名字集合（会按 ``normalize_kc_name`` 归一后比对）。
    :return: ``[{display_name, normalized_name}]``，按首次出现顺序去重。

    归一化只用于比对与同计划内去重；这里不做任何模糊匹配 —— 拿不准的概念宁可留作
    临时概念，也不要冒充成某个正式 KC。
    """
    known = {normalize_kc_name(name) for name in (known_kc_names or []) if isinstance(name, str)}
    known.discard("")

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for task in _iter_tasks(structure):
        for raw_name in task.get("kc_names") or []:
            if not isinstance(raw_name, str):
                continue
            display = raw_name.strip()[:NAME_MAX]
            if not display:
                continue
            normalized = normalize_kc_name(display)
            if not normalized or normalized in known or normalized in seen:
                continue
            seen.add(normalized)
            result.append({"display_name": display, "normalized_name": normalized[:NAME_MAX]})
            if len(result) >= MAX_PLAN_CONCEPTS:
                return result
    return result


def _iter_tasks(structure: dict | None) -> list[dict[str, Any]]:
    """轻量遍历（不依赖 plan_adjust，便于纯单测与被 services 复用）。"""
    tasks: list[dict[str, Any]] = []
    if not isinstance(structure, dict):
        return tasks
    for stage in structure.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        for day in stage.get("days") or []:
            if not isinstance(day, dict):
                continue
            for task in day.get("tasks") or []:
                if isinstance(task, dict):
                    tasks.append(task)
    return tasks
