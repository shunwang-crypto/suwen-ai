"""向量库对账单测: 纯逻辑, 零 PG / 零 Qdrant (两侧均以假件注入)。

覆盖:
- 精确 chunk/point 身份差异（等量错配也能识别）与旧聚合字段兼容;
- orphan-only、wrong tenant、malformed、文档状态/point-doc 不一致;
- 单库出错不中断整体扫描 (对账是诊断工具, 一个坏库不该让其余库失去可见性);
- 超时、稳定输出、分页上限参数和 ``status`` 三态/CLI 契约。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from eduagents_api import vector_reconcile as vr
from eduagents_core.vectorstore.base import VectorPointRef


class _FakeStore:
    """按 (tenant, kb) 返回点数; scan 返回全库分布。"""

    def __init__(
        self,
        counts: dict[tuple[str, str], int] | None = None,
        scan: dict[tuple[str, str], int] | None = None,
        *,
        count_raises: str | None = None,
        scan_raises: bool = False,
    ) -> None:
        self._counts = counts or {}
        self._scan = scan if scan is not None else dict(counts or {})
        self._count_raises = count_raises
        self._scan_raises = scan_raises
        self.scan_calls = 0

    async def count_by_kb(self, tenant_id: str, kb_id: str) -> int:
        if self._count_raises and kb_id == self._count_raises:
            raise RuntimeError("qdrant unreachable")
        return self._counts.get((tenant_id, kb_id), 0)

    async def scan_kb_ids(self) -> dict[tuple[str, str], int]:
        self.scan_calls += 1
        if self._scan_raises:
            raise RuntimeError("scroll failed")
        return self._scan


class _ExactFakeStore:
    """Metadata-only point reference fake for the A0 identity path."""

    def __init__(
        self,
        refs: list[VectorPointRef],
        *,
        raises: Exception | None = None,
        delay: float = 0,
    ) -> None:
        self.refs = refs
        self.raises = raises
        self.delay = delay
        self.scan_calls = 0
        self.last_kwargs: dict = {}

    async def scan_point_refs(self, **kwargs):
        self.scan_calls += 1
        self.last_kwargs = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.refs


T1 = "11111111-1111-1111-1111-111111111111"
T2 = "22222222-2222-2222-2222-222222222222"
KB_A = "aaaaaaaa-0000-0000-0000-000000000001"
KB_B = "bbbbbbbb-0000-0000-0000-000000000002"
KB_GONE = "cccccccc-0000-0000-0000-000000000003"
P1 = "10000000-0000-0000-0000-000000000001"
P2 = "10000000-0000-0000-0000-000000000002"
P3 = "10000000-0000-0000-0000-000000000003"
P4 = "10000000-0000-0000-0000-000000000004"
D1 = "20000000-0000-0000-0000-000000000001"
D2 = "20000000-0000-0000-0000-000000000002"


@pytest.fixture
def patch_pg(monkeypatch):
    """替掉两个走 PG 的私有函数; 其余逻辑保持真实。"""

    def _apply(kbs: list[tuple[str, str, str]], done: dict[str, int]):
        async def _list(_admin_tenant):
            return kbs

        async def _count(_tenant, kb_id):
            return done.get(str(kb_id), 0)

        monkeypatch.setattr(vr, "_list_knowledge_bases", _list)
        monkeypatch.setattr(vr, "_pg_done_count", _count)

    return _apply


@pytest.fixture
def patch_exact_pg(monkeypatch):
    def _apply(
        kbs: list[tuple[str, str, str]],
        chunks: dict[str, list[vr.PgChunkRef]],
        docs: dict[str, list[dict]] | None = None,
        *,
        fail_kb: str | None = None,
    ):
        async def _list(_admin_tenant):
            return kbs

        async def _chunks(_tenant, kb_id):
            if str(kb_id) == fail_kb:
                raise TimeoutError("PG KB timeout")
            return chunks.get(str(kb_id), [])

        async def _docs(_tenant, kb_id):
            if str(kb_id) == fail_kb:
                raise TimeoutError("PG document timeout")
            return list((docs or {}).get(str(kb_id), []))

        monkeypatch.setattr(vr, "_list_knowledge_bases", _list)
        monkeypatch.setattr(vr, "_pg_done_chunks", _chunks)
        monkeypatch.setattr(vr, "_pg_document_inconsistencies", _docs)

    return _apply


def ref(point_id: str, tenant_id: str = T1, kb_id: str = KB_A, doc_id: str = D1):
    return VectorPointRef(
        point_id=point_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        doc_id=doc_id,
    )


async def test_consistent_is_ok(patch_pg):
    patch_pg([(KB_A, T1, "教材库")], {KB_A: 5})
    report = await vr.reconcile_vectors(_FakeStore({(T1, KB_A): 5}), T1)

    assert report.status == "ok"
    assert report.missing_vectors == 0
    assert report.orphan_points == 0
    assert report.rows[0].healthy is True
    assert report.as_dict()["items"] == []


async def test_missing_vectors_detected(patch_pg):
    """PG 标 done 但 Qdrant 没有 —— 检索会静默返回 0 命中。"""
    patch_pg([(KB_A, T1, "教材库")], {KB_A: 7})
    report = await vr.reconcile_vectors(_FakeStore({(T1, KB_A): 2}), T1)

    assert report.status == "drift"
    assert report.missing_vectors == 5
    row = report.rows[0]
    assert (row.drift, row.healthy) == (5, False)
    assert report.as_dict()["items"][0]["kb_id"] == KB_A


async def test_qdrant_surplus_on_live_kb_is_unhealthy(patch_pg):
    """现存 kb 的多余点必须报不健康: 它们能被检索命中, 会把已删内容当有效结果返回。

    正常摄取不会产生这种状态 (ingestion 重摄前先 delete_by_doc), 出现即异常路径。
    """
    patch_pg([(KB_A, T1, "教材库")], {KB_A: 3})
    report = await vr.reconcile_vectors(_FakeStore({(T1, KB_A): 9}), T1)

    assert report.rows[0].healthy is False
    assert report.status == "drift"
    assert report.missing_vectors == 0
    assert report.orphan_points == 6


async def test_orphan_kb_detected(patch_pg):
    """Qdrant 里有、PG 里已无对应 kb: 删除未清理, payload 含原文。"""
    patch_pg([(KB_A, T1, "教材库")], {KB_A: 4})
    store = _FakeStore(
        counts={(T1, KB_A): 4},
        scan={(T1, KB_A): 4, (T2, KB_GONE): 186},
    )
    report = await vr.reconcile_vectors(store, T1)

    assert report.status == "drift"
    assert report.missing_vectors == 0
    assert len(report.orphan_kbs) == 1
    orphan = report.orphan_kbs[0]
    assert (orphan.kb_id, orphan.tenant_id, orphan.qdrant_points) == (KB_GONE, T2, 186)
    assert report.orphan_points == 186
    payload = report.as_dict()
    assert payload["kb_orphaned"] == 1
    assert payload["orphans"][0]["kb_id"] == KB_GONE


async def test_single_kb_query_skips_reverse_scan(patch_pg):
    """指定 kb_id 时不做反向扫描 —— 单库查询没有"孤儿"这个概念。"""
    patch_pg([(KB_A, T1, "教材库"), (KB_B, T1, "笔记")], {KB_A: 2, KB_B: 9})
    store = _FakeStore({(T1, KB_A): 2, (T1, KB_B): 0})

    report = await vr.reconcile_vectors(store, T1, kb_id=KB_A)

    assert [r.kb_id for r in report.rows] == [KB_A]
    assert store.scan_calls == 0
    assert report.orphan_kbs == []
    assert report.status == "ok"


async def test_one_broken_kb_does_not_abort_scan(patch_pg):
    patch_pg([(KB_A, T1, "坏库"), (KB_B, T1, "好库")], {KB_A: 3, KB_B: 4})
    store = _FakeStore({(T1, KB_B): 4}, scan={(T1, KB_B): 4}, count_raises=KB_A)

    report = await vr.reconcile_vectors(store, T1)

    assert [r.kb_id for r in report.rows] == [KB_B]
    assert len(report.errors) == 1
    assert report.errors[0]["kb_id"] == KB_A
    assert "RuntimeError" in report.errors[0]["error"]
    assert report.status == "error"


async def test_scan_failure_surfaces_as_error(patch_pg):
    patch_pg([(KB_A, T1, "教材库")], {KB_A: 1})
    store = _FakeStore({(T1, KB_A): 1}, scan_raises=True)

    report = await vr.reconcile_vectors(store, T1)

    assert report.rows[0].healthy is True
    assert report.status == "error"
    assert report.errors[0]["kb_id"] == "*"
    assert "scan_kb_ids" in report.errors[0]["error"]


async def test_include_all_lists_healthy_rows(patch_pg):
    patch_pg([(KB_A, T1, "好库"), (KB_B, T1, "漂移库")], {KB_A: 2, KB_B: 5})
    store = _FakeStore(
        {(T1, KB_A): 2, (T1, KB_B): 1},
        scan={(T1, KB_A): 2, (T1, KB_B): 1},
    )

    report = await vr.reconcile_vectors(store, T1)

    assert len(report.as_dict()["items"]) == 1
    assert len(report.as_dict(include_all=True)["items"]) == 2
    assert report.as_dict()["kb_total"] == 2


async def test_equal_counts_with_different_ids_are_drift(patch_exact_pg):
    patch_exact_pg(
        [(KB_A, T1, "教材库")],
        {
            KB_A: [
                vr.PgChunkRef(T1, KB_A, P1, D1),
                vr.PgChunkRef(T1, KB_A, P2, D1),
                vr.PgChunkRef(T1, KB_A, P3, D2),
            ]
        },
    )
    report = await vr.reconcile_vectors(
        _ExactFakeStore([ref(P3, doc_id=D2), ref(P4), ref(P1)]), T1
    )

    assert report.status == "drift"
    assert report.rows[0].pg_done == report.rows[0].qdrant_points == 3
    assert report.missing_points == [P2]
    assert report.extra_points == [P4]
    assert report.missing_vectors == 1
    assert report.orphan_points == 1
    assert report.rows[0].healthy is False


async def test_orphan_only_is_drift(patch_exact_pg):
    patch_exact_pg([], {})
    report = await vr.reconcile_vectors(
        _ExactFakeStore([ref(P4, tenant_id=T2, kb_id=KB_GONE, doc_id=D2)]), T1
    )

    assert report.status == "drift"
    assert report.rows == []
    assert report.extra_points == [P4]
    assert report.orphan_points == 1
    assert report.orphan_kbs[0].as_dict()["point_ids"] == [P4]


async def test_wrong_tenant_is_reported_separately(patch_exact_pg):
    patch_exact_pg(
        [(KB_A, T1, "教材库")],
        {KB_A: [vr.PgChunkRef(T1, KB_A, P1, D1)]},
    )
    report = await vr.reconcile_vectors(
        _ExactFakeStore([ref(P1, tenant_id=T2)]), T1
    )

    assert report.status == "drift"
    assert report.missing_points == [P1]
    assert report.extra_points == [P1]
    assert report.wrong_tenant_points == [P1]
    assert report.orphan_kbs == []
    assert report.rows[0].wrong_tenant_points == [P1]
    assert report.as_dict()["wrong_tenant_details"][0]["expected_tenant_id"] == T1


async def test_malformed_point_is_not_silently_counted(patch_exact_pg):
    patch_exact_pg([(KB_A, T1, "教材库")], {})
    report = await vr.reconcile_vectors(
        _ExactFakeStore(
            [
                VectorPointRef(
                    point_id="not-a-uuid",
                    tenant_id=T1,
                    kb_id=KB_A,
                    doc_id=D1,
                ),
                VectorPointRef(point_id=P1, tenant_id=T1, kb_id=KB_A, doc_id=None),
            ]
        ),
        T1,
    )

    assert report.status == "drift"
    assert report.extra_points == []
    assert report.orphan_points == 2
    assert {item["reason"] for item in report.malformed_points} == {
        "invalid_point_id",
        "missing_doc_id",
    }


async def test_document_inconsistency_is_reported(patch_exact_pg):
    issue = {
        "tenant_id": T1,
        "kb_id": KB_A,
        "doc_id": D1,
        "status": "ready",
        "reason": "terminal_document_without_done_child_chunks",
        "done_child_chunks": 0,
    }
    patch_exact_pg([(KB_A, T1, "教材库")], {KB_A: []}, {KB_A: [issue]})
    report = await vr.reconcile_vectors(_ExactFakeStore([]), T1)

    assert report.status == "drift"
    assert report.document_inconsistencies == [issue]
    assert report.rows[0].healthy is False
    assert report.as_dict()["items"][0]["document_inconsistencies"] == [issue]


async def test_one_exact_kb_failure_does_not_abort_other_kbs(patch_exact_pg):
    patch_exact_pg(
        [(KB_A, T1, "坏库"), (KB_B, T1, "好库")],
        {KB_A: [vr.PgChunkRef(T1, KB_A, P1, D1)], KB_B: [vr.PgChunkRef(T1, KB_B, P2, D1)]},
        fail_kb=KB_A,
    )
    report = await vr.reconcile_vectors(
        _ExactFakeStore([ref(P2, kb_id=KB_B)]), T1
    )

    assert [row.kb_id for row in report.rows] == [KB_B]
    assert report.rows[0].healthy is True
    assert report.status == "error"
    assert report.errors[0]["kb_id"] == KB_A


async def test_scan_timeout_is_error_but_result_is_stable(patch_exact_pg):
    patch_exact_pg([(KB_A, T1, "教材库")], {KB_A: [vr.PgChunkRef(T1, KB_A, P1, D1)]})
    store = _ExactFakeStore([ref(P1)], delay=0.02)
    first = await vr.reconcile_vectors(store, T1, scan_timeout_seconds=0.001)
    second = await vr.reconcile_vectors(store, T1, scan_timeout_seconds=0.001)

    assert first.status == second.status == "error"
    assert first.as_dict() == second.as_dict()
    assert "scan_point_refs" in first.errors[0]["error"]


async def test_scan_arguments_include_bounded_pagination(patch_exact_pg):
    patch_exact_pg([(KB_A, T1, "教材库")], {})
    store = _ExactFakeStore([])
    report = await vr.reconcile_vectors(
        store, T1, scan_page_size=7, scan_max_pages=3, scan_max_points=20
    )

    assert report.status == "ok"
    assert store.last_kwargs == {
        "page_size": 7,
        "max_pages": 3,
        "timeout": 30,
        "max_points": 20,
    }


def test_cli_exit_code_uses_report_status():
    path = Path(__file__).parents[2] / "scripts" / "check_vector_drift.py"
    spec = importlib.util.spec_from_file_location("check_vector_drift", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._status_exit_code("ok") == 0
    assert module._status_exit_code("drift") == 1
    assert module._status_exit_code("error") == 2
