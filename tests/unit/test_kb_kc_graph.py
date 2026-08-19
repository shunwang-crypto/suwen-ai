"""库级 KC 知识图（Learning Map full 模式）的纯聚合测试。

守两条纪律：掌握度数值不出境（只给 evidence_state 与定性档，且档位只在证据
充分时给）、按章聚合且章序确定。
"""

from __future__ import annotations

import json

from eduagents_api.kb_kc_graph import build_kb_kc_graph
from eduagents_api.kc_hierarchy import FALLBACK_CHAPTER

KC_A = "11111111-1111-4111-8111-111111111111"
KC_B = "22222222-2222-4222-8222-222222222222"
KC_C = "33333333-3333-4333-8333-333333333333"


def test_empty_kb_returns_empty_shape():
    graph = build_kb_kc_graph([], [], {}, [])
    assert graph == {
        "kc_count": 0,
        "chapter_count": 0,
        "chapters": [],
        "nodes": [],
        "edges": [],
    }


def test_nodes_keep_stable_db_order_and_group_by_chapter():
    graph = build_kb_kc_graph(
        [(KC_A, "极限"), (KC_B, "导数"), (KC_C, "积分")],
        [],
        {KC_A: "第一章 函数", KC_B: "第一章 函数", KC_C: "第三章 积分"},
        [],
    )
    assert [node["kc_name"] for node in graph["nodes"]] == ["极限", "导数", "积分"]
    assert [chapter["chapter"] for chapter in graph["chapters"]] == ["第一章 函数", "第三章 积分"]
    assert graph["chapters"][0]["kc_count"] == 2
    assert graph["kc_count"] == 3
    assert graph["chapter_count"] == 2


def test_chapters_sort_by_chapter_number_not_insertion_order():
    graph = build_kb_kc_graph(
        [(KC_A, "积分"), (KC_B, "极限")],
        [],
        {KC_A: "第十章 收尾", KC_B: "第二章 起步"},
        [],
    )
    assert [chapter["chapter"] for chapter in graph["chapters"]] == ["第二章 起步", "第十章 收尾"]


def test_missing_chapter_falls_back_to_the_explicit_bucket():
    graph = build_kb_kc_graph([(KC_A, "极限")], [], {}, [])
    assert graph["nodes"][0]["chapter"] == FALLBACK_CHAPTER


def test_edges_outside_the_kb_and_self_loops_are_dropped():
    graph = build_kb_kc_graph(
        [(KC_A, "极限"), (KC_B, "导数")],
        [(KC_A, KC_B), (KC_A, KC_B), (KC_A, KC_A), (KC_A, KC_C)],
        {},
        [],
    )
    assert graph["edges"] == [{"from_kc_id": KC_A, "to_kc_id": KC_B}]


def test_band_only_when_evidence_is_observed():
    """未观测 / 估计中的 KC 不许被叙述成薄弱或已掌握（沿用既有证据纪律）。"""
    graph = build_kb_kc_graph(
        [(KC_A, "极限"), (KC_B, "导数"), (KC_C, "积分")],
        [],
        {},
        [
            (KC_A, 0.9, 5),  # observed -> 给档
            (KC_B, 0.2, 2),  # estimated -> 不给档
            # KC_C 完全没有记录 -> unobserved
        ],
    )
    by_id = {node["kc_id"]: node for node in graph["nodes"]}
    assert by_id[KC_A]["evidence_state"] == "observed"
    assert by_id[KC_A]["band"] == "已掌握"
    assert by_id[KC_B]["evidence_state"] == "estimated"
    assert by_id[KC_B]["band"] is None
    assert by_id[KC_C]["evidence_state"] == "unobserved"
    assert by_id[KC_C]["band"] is None
    assert by_id[KC_C]["evidence_state_label"] == "未观测"


def test_no_mastery_number_leaves_the_module():
    """整个返回值里不得出现掌握度小数或百分比（P2-7 出境口径）。"""
    graph = build_kb_kc_graph(
        [(KC_A, "极限")],
        [],
        {KC_A: "第一章"},
        [(KC_A, 0.7345, 9)],
    )
    payload = json.dumps(graph, ensure_ascii=False)
    assert "0.7345" not in payload
    assert "0.73" not in payload
    assert "%" not in payload
    assert "p_mastery" not in payload
    assert graph["nodes"][0]["band"] == "接近掌握"


def test_mastery_rows_for_other_kbs_are_ignored():
    graph = build_kb_kc_graph([(KC_A, "极限")], [], {}, [(KC_C, 0.9, 9)])
    assert graph["nodes"][0]["evidence_state"] == "unobserved"


def test_duplicate_kc_rows_collapse_to_one_node():
    graph = build_kb_kc_graph([(KC_A, "极限"), (KC_A, "极限（重复）")], [], {}, [])
    assert graph["kc_count"] == 1
    assert graph["nodes"][0]["kc_name"] == "极限"


def test_blank_names_are_skipped_without_breaking_the_graph():
    graph = build_kb_kc_graph([(KC_A, "  "), (KC_B, "导数")], [(KC_A, KC_B)], {}, [])
    assert [node["kc_id"] for node in graph["nodes"]] == [KC_B]
    assert graph["edges"] == []
