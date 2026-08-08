"""向量库对账 (PG chunks ↔ Qdrant points)。

**要守的不变量**: 每个 ``embedding_status='done'`` 的子块行, 在 Qdrant 里都应有一个
同 id 的点。摄取流程先 upsert 向量再把行标 done (ingestion.py), 所以正常情况下
PG 侧的 done 数 == Qdrant 侧该 kb 的点数。

**为什么需要这个**: /api/health/ready 的 qdrant 探针只测连通性 (HTTP 200), 不看数据。
若 Qdrant 掉过数据或被重建, PG 仍标着 done, 检索会静默返回 0 命中 —— 链路"看起来
正常"(降级到 web 兜底), 但 RAG 实际已失效, 没有任何告警。本模块把这个差异显式化。

**租户边界**: chunks/documents 是严格 RLS 隔离的 (只有 tenant_read/tenant_write,
无 admin 放行策略, 与 knowledge_bases 不同)。因此对账不走跨租户大查询, 而是
"管理员枚举 kb -> 逐 kb 开该租户的 session 计数"。这样零迁移, 也不新增跨租户读面。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from eduagents_core.logging import get_logger
from sqlalchemy import func, select, text

from .db import tenant_session
from .models import Chunk, Document, KnowledgeBase

log = get_logger("vector_reconcile")

_ADMIN_GUC = text("SELECT set_config('app.is_admin', 'true', true)")

# 每个 kb 的差异绝对值 ≤ 该阈值时视为噪声 (摄取进行中的短暂窗口), 不计入 drift。
# 摄取过程本身是 upsert 后才标 done, 中间态表现为 PG < Qdrant, 不是数据丢失。
DRIFT_TOLERANCE = 0


@dataclass
class KbReconcileRow:
    """单个知识库的两侧计数。"""

    kb_id: str
    tenant_id: str
    name: str
    pg_done: int
    qdrant_points: int

    @property
    def drift(self) -> int:
        """正数 = PG 多于 Qdrant (向量丢失, 检索会静默失效); 负数 = Qdrant 有孤儿点。"""
        return self.pg_done - self.qdrant_points

    @property
    def healthy(self) -> bool:
        return abs(self.drift) <= DRIFT_TOLERANCE

    def as_dict(self) -> dict:
        return {
            "kb_id": self.kb_id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "pg_done": self.pg_done,
            "qdrant_points": self.qdrant_points,
            "drift": self.drift,
            "healthy": self.healthy,
        }


@dataclass
class OrphanKbRow:
    """Qdrant 里有向量、PG 里已无对应知识库 —— 删除未清理干净的残留。

    不只是存储浪费: 点的 payload 含文档原文, 知识库删除/账号注销后仍留存, 属于
    数据删除不完整。
    """

    kb_id: str
    tenant_id: str
    qdrant_points: int

    def as_dict(self) -> dict:
        return {
            "kb_id": self.kb_id,
            "tenant_id": self.tenant_id,
            "qdrant_points": self.qdrant_points,
        }


@dataclass
class ReconcileReport:
    rows: list[KbReconcileRow] = field(default_factory=list)
    orphan_kbs: list[OrphanKbRow] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def missing_vectors(self) -> int:
        """PG 标 done 但 Qdrant 查不到的块总数 —— 这是会导致检索静默失效的部分。"""
        return sum(r.drift for r in self.rows if r.drift > 0)

    @property
    def orphan_points(self) -> int:
        """Qdrant 多出来的点: 既包含现存 kb 的多余点, 也包含已删 kb 的全部残留。"""
        within_known = sum(-r.drift for r in self.rows if r.drift < 0)
        return within_known + sum(o.qdrant_points for o in self.orphan_kbs)

    @property
    def status(self) -> str:
        if self.errors:
            return "error"
        if self.unhealthy_rows or self.orphan_kbs:
            return "drift"
        return "ok"

    @property
    def unhealthy_rows(self) -> list[KbReconcileRow]:
        return [r for r in self.rows if not r.healthy]

    def as_dict(self, *, include_all: bool = False) -> dict:
        shown = self.rows if include_all else self.unhealthy_rows
        return {
            "status": self.status,
            "kb_total": len(self.rows),
            "kb_drifted": len(self.unhealthy_rows),
            "kb_orphaned": len(self.orphan_kbs),
            "missing_vectors": self.missing_vectors,
            "orphan_points": self.orphan_points,
            "items": [r.as_dict() for r in shown],
            "orphans": [o.as_dict() for o in self.orphan_kbs],
            "errors": self.errors,
        }


async def _list_knowledge_bases(admin_tenant_id: uuid.UUID | str) -> list[tuple]:
    """枚举全部 kb (id, tenant_id, name)。

    knowledge_bases 有 admin_count_read 策略放行 app.is_admin, 所以这一步可以跨租户;
    chunks 没有该策略, 计数必须回到各自租户的 session (见模块 docstring)。
    """
    async with tenant_session(admin_tenant_id) as session:
        await session.execute(_ADMIN_GUC)
        rows = await session.execute(
            select(KnowledgeBase.id, KnowledgeBase.tenant_id, KnowledgeBase.name)
        )
        return list(rows.all())


async def _pg_done_count(tenant_id: uuid.UUID, kb_id: uuid.UUID) -> int:
    """该 kb 下 embedding_status='done' 的子块数 (chunks 无 kb_id, 经 documents 关联)。"""
    async with tenant_session(tenant_id) as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(Chunk)
                .join(Document, Document.id == Chunk.doc_id)
                .where(
                    Document.kb_id == kb_id,
                    Chunk.embedding_status == "done",
                )
            )
            or 0
        )


async def reconcile_vectors(
    vector_store,
    admin_tenant_id: uuid.UUID | str,
    kb_id: uuid.UUID | str | None = None,
) -> ReconcileReport:
    """比对 PG 的 done 块数与 Qdrant 的点数。只读, 不修改任何一侧。

    两个方向都查:
    - PG 有 / Qdrant 无 -> ``missing_vectors``, 检索会静默失效
    - Qdrant 有 / PG 无 -> ``orphan_kbs``, 删除未清理干净 (含原文残留)

    单个 kb 出错不中断整体扫描 —— 对账本身是诊断工具, 不该因为一个坏库就失去
    对其余库的可见性。
    """
    report = ReconcileReport()
    wanted = str(kb_id) if kb_id else None
    known_kbs: set[str] = set()

    for row_id, row_tenant, row_name in await _list_knowledge_bases(admin_tenant_id):
        known_kbs.add(str(row_id))
        if wanted and str(row_id) != wanted:
            continue
        try:
            pg_done = await _pg_done_count(row_tenant, row_id)
            points = await vector_store.count_by_kb(str(row_tenant), str(row_id))
        except Exception as exc:  # noqa: BLE001
            report.errors.append(
                {"kb_id": str(row_id), "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        report.rows.append(
            KbReconcileRow(
                kb_id=str(row_id),
                tenant_id=str(row_tenant),
                name=row_name,
                pg_done=pg_done,
                qdrant_points=points,
            )
        )

    # 反向扫描: 只在全量对账时做 (单 kb 查询没有"孤儿"这个概念)。
    if wanted is None:
        try:
            for (t_id, k_id), count in (await vector_store.scan_kb_ids()).items():
                if k_id not in known_kbs:
                    report.orphan_kbs.append(
                        OrphanKbRow(kb_id=k_id, tenant_id=t_id, qdrant_points=count)
                    )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(
                {"kb_id": "*", "error": f"scan_kb_ids: {type(exc).__name__}: {exc}"}
            )

    if report.unhealthy_rows or report.orphan_kbs or report.errors:
        log.warning(
            "vector_reconcile_drift",
            kb_total=len(report.rows),
            kb_drifted=len(report.unhealthy_rows),
            kb_orphaned=len(report.orphan_kbs),
            missing_vectors=report.missing_vectors,
            orphan_points=report.orphan_points,
            errors=len(report.errors),
        )
    return report
