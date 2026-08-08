#!/usr/bin/env python3
"""清理孤儿向量: Qdrant 里有点、PG 里已无对应知识库。

**默认 dry-run**, 只打印将要删除什么。真正删除需要显式 ``--execute``。

    python scripts/purge_orphan_vectors.py               # 预演 (只读)
    python scripts/purge_orphan_vectors.py --execute     # 真删
    python scripts/purge_orphan_vectors.py --execute --kb <uuid>   # 只删一个

**为什么需要**: 知识库删除/账号注销后若向量未清干净, 点的 payload 仍含文档原文,
属于数据删除不完整 (不只是存储浪费)。检测口径与 ``check_vector_drift.py`` 完全一致
(共用 vector_reconcile), 这里只多一个受确认保护的删除动作。

**安全边界**:
- 只删 PG 中已无对应 knowledge_bases 行的 kb_id —— 现存库一律不碰, 哪怕它有漂移
  (那种情况要重新摄取, 不是删向量);
- 删除按 (tenant_id, kb_id) 过滤下发, 不做无租户删除;
- 删除不可逆且无回滚: 向量重建需要原始文件重新摄取。因此默认只预演。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from eduagents_api.vector_reconcile import reconcile_vectors
from eduagents_core.config import get_settings
from eduagents_core.vectorstore.qdrant_store import QdrantStore

_PLACEHOLDER_TENANT = "00000000-0000-0000-0000-000000000000"


async def _run(args: argparse.Namespace) -> int:
    store = QdrantStore(get_settings())
    try:
        report = await reconcile_vectors(store, args.admin_tenant or _PLACEHOLDER_TENANT)
        if report.errors:
            for err in report.errors:
                print(f"[错误] kb={err['kb_id']}: {err['error']}", file=sys.stderr)
            print("对账未完整, 拒绝执行删除。", file=sys.stderr)
            return 2

        targets = report.orphan_kbs
        if args.kb:
            targets = [o for o in targets if o.kb_id == args.kb]
            if not targets:
                print(f"{args.kb} 不在孤儿列表中 (可能是现存库或本就没有向量), 不做任何事。")
                return 0

        if not targets:
            print("没有孤儿向量, 无需清理。")
            return 0

        total = sum(o.qdrant_points for o in targets)
        print(f"孤儿知识库 {len(targets)} 个, 合计 {total} 个点:")
        print(f"{'kb_id':38} {'points':>8}  tenant_id")
        for o in sorted(targets, key=lambda x: -x.qdrant_points):
            print(f"{o.kb_id:38} {o.qdrant_points:>8}  {o.tenant_id}")
        print()

        if not args.execute:
            print("这是预演 (dry-run), 未删除任何数据。确认无误后加 --execute 执行。")
            return 0

        deleted_kbs = 0
        deleted_points = 0
        for o in targets:
            try:
                await store.delete_by_kb(o.tenant_id, o.kb_id)
            except Exception as exc:  # noqa: BLE001
                print(f"[失败] kb={o.kb_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            deleted_kbs += 1
            deleted_points += o.qdrant_points
            print(f"已删 kb={o.kb_id} ({o.qdrant_points} 点)")

        print()
        print(f"完成: {deleted_kbs}/{len(targets)} 个知识库, {deleted_points} 个点。")
        # 复核: 重新扫描确认目标 kb 确实归零
        remaining = await store.scan_kb_ids()
        still = {o.kb_id for o in targets} & {kb for _t, kb in remaining}
        if still:
            print(f"警告: 以下 kb 仍有残留点: {sorted(still)}", file=sys.stderr)
            return 1
        print("复核通过: 目标 kb 在 Qdrant 中已无残留。")
        return 0
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="真正删除 (默认只预演)")
    parser.add_argument("--kb", help="只清理这一个 kb_id (必须在孤儿列表中)")
    parser.add_argument("--admin-tenant", help="枚举 kb 时落脚的 tenant GUC")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
