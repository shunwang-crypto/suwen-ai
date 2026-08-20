"""按知识点结构化讲解的契约测试（纯逻辑）。

守三条硬约束：出处必须落在本人资料的真实 chunk、diagram 结构必须可用、讲解不出
练习题；外加缓存键的确定性与 stale 判定。
"""

from __future__ import annotations

import pytest
from eduagents_api.kc_explanation import (
    BLOCK_TYPES,
    EXPLANATION_GENERATOR_VERSION,
    BlockIssue,
    band_for,
    context_hash,
    is_stale,
    validate_block,
    validate_blocks,
)

SOURCES = {"chunk-1", "chunk-2"}


def _concept(**extra) -> dict:
    return {
        "type": "concept",
        "title": "什么是导数",
        "content": "导数刻画瞬时变化率。",
        "source_refs": ["chunk-1"],
        **extra,
    }


# ---- 出处约束 ----


def test_block_without_source_refs_is_rejected():
    with pytest.raises(BlockIssue, match="出处"):
        validate_block(_concept(source_refs=[]), SOURCES)


def test_block_with_fabricated_source_id_is_rejected():
    """模型编造 chunk id 等于脱离资料生成 —— 直接拒。"""
    with pytest.raises(BlockIssue, match="出处"):
        validate_block(_concept(source_refs=["chunk-999"]), SOURCES)


def test_only_known_source_ids_survive():
    block = validate_block(_concept(source_refs=["chunk-1", "chunk-999", "chunk-2"]), SOURCES)
    assert block["source_refs"] == ["chunk-1", "chunk-2"]


# ---- 类型与内容 ----


def test_unknown_block_type_is_rejected():
    with pytest.raises(BlockIssue, match="未知 block 类型"):
        validate_block(_concept(type="handoff"), SOURCES)


def test_text_block_needs_title_and_content():
    with pytest.raises(BlockIssue, match="标题"):
        validate_block(_concept(title="  "), SOURCES)
    with pytest.raises(BlockIssue, match="内容为空"):
        validate_block(_concept(content=""), SOURCES)


def test_v1_supports_exactly_four_block_types():
    assert BLOCK_TYPES == ("concept", "worked_example", "misconception", "diagram")


def test_practice_semantics_are_rejected_from_explanations():
    """讲解模块不出题：练习与判分有独立模块与证据口径。"""
    with pytest.raises(BlockIssue, match="练习/判分"):
        validate_block(
            _concept(content='下面选择正确答案：{"options": ["A", "B"]}'),
            SOURCES,
        )


# ---- diagram 最小结构校验 ----


def _diagram(nodes, edges) -> dict:
    return {
        "type": "diagram",
        "title": "求导流程",
        "source_refs": ["chunk-1"],
        "data": {"nodes": nodes, "edges": edges},
    }


def test_diagram_requires_usable_structure():
    with pytest.raises(BlockIssue, match="缺少 nodes"):
        validate_block(_diagram([], []), SOURCES)


def test_diagram_edge_to_unknown_node_is_rejected():
    with pytest.raises(BlockIssue, match="不存在的节点"):
        validate_block(
            _diagram([{"id": "a", "label": "输入"}], [{"source": "a", "target": "ghost"}]),
            SOURCES,
        )


def test_diagram_self_loop_is_rejected():
    with pytest.raises(BlockIssue, match="自环"):
        validate_block(
            _diagram([{"id": "a", "label": "输入"}], [{"source": "a", "target": "a"}]),
            SOURCES,
        )


def test_diagram_cycle_is_rejected():
    """一个'看起来对其实错'的图比没有图更糟。"""
    with pytest.raises(BlockIssue, match="成环"):
        validate_block(
            _diagram(
                [{"id": "a", "label": "甲"}, {"id": "b", "label": "乙"}],
                [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
            ),
            SOURCES,
        )


def test_valid_diagram_is_normalized():
    block = validate_block(
        _diagram(
            [{"id": "a", "label": "甲"}, {"id": "b", "name": "乙"}, {"id": "a", "label": "重复"}],
            [{"from": "a", "to": "b"}],
        ),
        SOURCES,
    )
    assert [node["id"] for node in block["data"]["nodes"]] == ["a", "b"]
    assert block["data"]["edges"] == [{"source": "a", "target": "b"}]


# ---- 整份讲解 ----


def test_bad_blocks_are_dropped_not_fatal_and_reasons_are_reported():
    blocks, issues = validate_blocks(
        [
            _concept(),
            _concept(title="没有出处", source_refs=[]),
            {"type": "worked_example", "title": "例题", "content": "步骤…", "source_refs": ["chunk-2"]},
        ],
        SOURCES,
    )
    assert [block["type"] for block in blocks] == ["concept", "worked_example"]
    assert len(issues) == 1


def test_all_blocks_invalid_yields_empty_so_caller_must_fail():
    blocks, issues = validate_blocks([_concept(source_refs=[])], SOURCES)
    assert blocks == []
    assert issues


def test_duplicate_blocks_are_dropped():
    blocks, issues = validate_blocks([_concept(), _concept()], SOURCES)
    assert len(blocks) == 1
    assert any("重复" in issue for issue in issues)


def test_blocks_are_ordered_by_teaching_sequence():
    blocks, _issues = validate_blocks(
        [
            {"type": "misconception", "title": "误解", "content": "…", "source_refs": ["chunk-1"]},
            _concept(),
        ],
        SOURCES,
    )
    assert [block["type"] for block in blocks] == ["concept", "misconception"]


def test_block_count_is_capped_and_truncation_is_reported():
    many = [_concept(title=f"概念 {index}") for index in range(12)]
    blocks, issues = validate_blocks(many, SOURCES)
    assert len(blocks) == 8
    assert any("上限" in issue for issue in issues)


# ---- 掌握档与缓存键 ----


def test_band_falls_back_to_unobserved_without_enough_evidence():
    assert band_for("estimated", "near") == "unobserved"
    assert band_for("unobserved", None) == "unobserved"
    assert band_for("observed", "near") == "near"
    assert band_for("observed", "nonsense") == "unobserved"


def test_context_hash_is_deterministic_and_sensitive():
    base = context_hash("导数", "near", ["原文 A", "原文 B"])
    assert base == context_hash("导数", "near", ["原文 A", "原文 B"])
    assert base != context_hash("导数", "weak", ["原文 A", "原文 B"])
    assert base != context_hash("导数", "near", ["原文 A", "原文 B 改了"])
    assert base != context_hash("积分", "near", ["原文 A", "原文 B"])
    assert base != context_hash("导数", "near", ["原文 A"])


def test_stale_when_teaching_contract_or_material_changes():
    current = context_hash("导数", "near", ["原文"])
    assert is_stale(EXPLANATION_GENERATOR_VERSION, current, current) is False
    assert is_stale("kc-explanation-v0", current, current) is True
    assert is_stale(EXPLANATION_GENERATOR_VERSION, "old-hash", current) is True


def test_diagram_with_no_edges_is_allowed_by_design():
    """edges 允许为空：只有节点的概念图不算结构错误。

    这条是**刻意钉住的产品决定**（评审定稿），不是漏校验 —— 若将来改成强制非空，
    必须同时改 kc_explanation 模块 docstring 与本测试。
    """
    block = validate_block(_diagram([{"id": "a", "label": "甲"}], []), SOURCES)
    assert block["data"]["nodes"] == [{"id": "a", "label": "甲"}]
    assert block["data"]["edges"] == []
