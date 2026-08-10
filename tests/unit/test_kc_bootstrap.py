"""Wave 1B-B chapter KC bootstrap contract tests.

These tests deliberately exercise the worker's deterministic planning helpers
without a database.  The database-facing cases live in
``tests/integration/test_kc_bootstrap.py`` and use the real unique index and
RLS-scoped sessions.
"""

from __future__ import annotations

import uuid

from eduagents_worker.kc_bootstrap import (
    MAX_CHAPTER_KCS,
    chapter_key_for_heading,
    plan_chapter_assignments,
    stable_kc_id,
)


def test_deepest_heading_and_normalized_full_path_are_the_stable_key():
    first = chapter_key_for_heading(
        " 第一章  极限  >  1.1  极限定义 ",
        document_name="高等数学.md",
        document_id="00000000-0000-0000-0000-000000000001",
    )
    second = chapter_key_for_heading(
        "第一章 极限>1.1 极限定义",
        document_name="高等数学.md",
        document_id="00000000-0000-0000-0000-000000000001",
    )

    assert first.canonical_name == "1.1 极限定义"
    assert first.stable_key == "chapter:第一章 极限 > 1.1 极限定义"
    assert second == first


def test_same_leaf_title_under_different_paths_does_not_collide():
    first = chapter_key_for_heading(
        "第一章 极限 > 定义",
        document_name="math.md",
        document_id="00000000-0000-0000-0000-000000000001",
    )
    second = chapter_key_for_heading(
        "第二章 导数 > 定义",
        document_name="math.md",
        document_id="00000000-0000-0000-0000-000000000001",
    )

    assert first.canonical_name == second.canonical_name == "定义"
    assert first.stable_key != second.stable_key


def test_empty_heading_uses_document_name_and_document_scoped_key():
    first = chapter_key_for_heading(
        "",
        document_name="物理笔记.md",
        document_id="00000000-0000-0000-0000-000000000001",
    )
    second = chapter_key_for_heading(
        "   >   ",
        document_name="物理笔记.md",
        document_id="00000000-0000-0000-0000-000000000002",
    )

    assert first.canonical_name == "物理笔记.md · 未分章节"
    assert second.canonical_name == first.canonical_name
    assert "00000000-0000-0000-0000-000000000001" in first.stable_key
    assert first.stable_key != second.stable_key


def test_uuid5_is_stable_within_kb_but_isolated_across_kbs():
    stable_key = "chapter:第一章 极限 > 定义"
    kb_a = uuid.UUID("00000000-0000-0000-0000-000000000011")
    kb_b = uuid.UUID("00000000-0000-0000-0000-000000000012")

    assert stable_kc_id(kb_a, stable_key) == stable_kc_id(kb_a, stable_key)
    assert stable_kc_id(kb_a, stable_key) != stable_kc_id(kb_b, stable_key)
    assert stable_kc_id(kb_a, stable_key) == uuid.uuid5(kb_a, stable_key)


def test_chapter_cap_is_deterministic_and_covers_every_child():
    children = [
        {"id": f"child-{i}", "heading_path": path}
        for i, path in enumerate(
            (
                "第三章 > C",
                "第一章 > A",
                "第四章 > D",
                "第二章 > B",
                "第五章 > E",
                "第三章 > C",
            )
        )
    ]

    first = plan_chapter_assignments(
        children,
        document_name="math.md",
        document_id="00000000-0000-0000-0000-000000000021",
        max_chapters=3,
    )
    second = plan_chapter_assignments(
        list(reversed(children)),
        document_name="math.md",
        document_id="00000000-0000-0000-0000-000000000021",
        max_chapters=3,
    )

    assert set(first) == {f"child-{i}" for i in range(len(children))}
    assert first == second
    assert 1 <= len({item.stable_key for item in first.values()}) <= 3
    assert len({item.stable_key for item in first.values()}) <= MAX_CHAPTER_KCS
    assert all(item.canonical_name for item in first.values())

    overflow = [item for item in first.values() if "其他章节" in item.canonical_name]
    assert overflow
    assert len({item.stable_key for item in overflow}) == 1
