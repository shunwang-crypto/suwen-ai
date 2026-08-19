"""知识库级 KC 知识图（Learning Map full 模式）的纯聚合层。

和计划级依赖图（``routers/plans._build_kc_dependency``）的分工：

- 计划级 = "当前计划任务 KC + 递归先修闭包"，回答"下一步学什么、被什么挡着"；
- 库级（本模块）= "这门课全部知识点 + 全部先修边"，回答"我在整门课的什么位置"。
  这是开放学习者模型（OLM）意义上的全局视图，答辩里能一眼看出覆盖面。

两条纪律：

1. **不出掌握度数值**。节点只带 ``evidence_state``（unobserved/estimated/observed）
   与定性档 ``band``；而且 ``band`` 只在 ``observed`` 时给 —— 未观测/估计中的
   KC 不允许被叙述成"薄弱"或"已掌握"（同 ``mastery.thresholds`` 的既有纪律）。
2. **按章聚合而不是一次铺平**。库级 KC 动辄上百个，全铺是毛线球；章节由
   ``kc_hierarchy`` 确定性推导，章序用 ``chapter_sort_key``，同一语料两次结果一致。

本模块零 DB、零 LLM，便于无数据库单测。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from eduagents_core.mastery.thresholds import (
    evidence_state,
    evidence_state_label,
    zone_label,
)

from .kc_hierarchy import FALLBACK_CHAPTER, chapter_sort_key


def build_kb_kc_graph(
    kc_rows: list[tuple[Any, str]],
    edge_rows: list[tuple[Any, Any]],
    chapter_by_kc: dict[str, str],
    mastery_rows: list[tuple[Any, float, int]],
) -> dict[str, Any]:
    """组装库级知识图。

    :param kc_rows: ``(kc_id, canonical_name)``，已按稳定库序（created_at）给出。
    :param edge_rows: ``(from_kc, to_kc)`` 先修边；两端不在 ``kc_rows`` 的边丢弃。
    :param chapter_by_kc: ``kc_hierarchy`` 推导的 ``{kc_id: chapter}``。
    :param mastery_rows: ``(kc_id, p_mastery, evidence_count)``；**p_mastery 只在
        本函数内用于换算定性档，绝不出现在返回值里**。
    """
    names_by_id: dict[str, str] = {}
    for raw_id, name in kc_rows:
        kc_id = str(raw_id)
        if not kc_id or not isinstance(name, str) or not name.strip():
            continue
        names_by_id.setdefault(kc_id, name)
    if not names_by_id:
        return {"kc_count": 0, "chapter_count": 0, "chapters": [], "nodes": [], "edges": []}

    state_by_id: dict[str, tuple[str, str | None]] = {}
    for raw_id, p_mastery, evidence_count in mastery_rows:
        kc_id = str(raw_id)
        if kc_id not in names_by_id:
            continue
        state = evidence_state(int(evidence_count or 0))
        # band 只在证据充分时给: 未观测/估计中不得被叙述成薄弱或已掌握
        band = zone_label(float(p_mastery)) if state == "observed" else None
        state_by_id[kc_id] = (state, band)

    nodes: list[dict[str, Any]] = []
    by_chapter: dict[str, list[str]] = defaultdict(list)
    for kc_id, name in names_by_id.items():
        chapter = (chapter_by_kc.get(kc_id) or "").strip() or FALLBACK_CHAPTER
        state, band = state_by_id.get(kc_id, ("unobserved", None))
        nodes.append(
            {
                "kc_id": kc_id,
                "kc_name": name,
                "chapter": chapter,
                "evidence_state": state,
                "evidence_state_label": evidence_state_label(state),  # type: ignore[arg-type]
                "band": band,
            }
        )
        by_chapter[chapter].append(kc_id)

    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_from, raw_to in edge_rows:
        from_id, to_id = str(raw_from), str(raw_to)
        if from_id not in names_by_id or to_id not in names_by_id or from_id == to_id:
            continue
        if (from_id, to_id) in seen:
            continue
        seen.add((from_id, to_id))
        edges.append({"from_kc_id": from_id, "to_kc_id": to_id})

    chapters = [
        {"chapter": chapter, "kc_ids": list(kc_ids), "kc_count": len(kc_ids)}
        for chapter, kc_ids in sorted(by_chapter.items(), key=lambda item: chapter_sort_key(item[0]))
    ]
    return {
        "kc_count": len(nodes),
        "chapter_count": len(chapters),
        "chapters": chapters,
        "nodes": nodes,
        "edges": edges,
    }
