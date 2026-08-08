#!/usr/bin/env python3
"""向量库对账: 精确比对 PG child chunk 身份与 Qdrant point 引用。

只读。不修改 PG, 不修改 Qdrant。

    python scripts/check_vector_drift.py              # 全部知识库
    python scripts/check_vector_drift.py --all        # 连健康的一起列出
    python scripts/check_vector_drift.py --kb <uuid>  # 只看一个库
    python scripts/check_vector_drift.py --json       # 机器可读 (供 cron/告警接管)

退出码: 0 = 一致; 1 = 存在漂移; 2 = 对账过程本身出错。适合直接挂进 cron/CI。

**为什么需要它**: /api/health/ready 的 qdrant 探针只验连通性。若 Qdrant 丢过数据,
PG 仍标着 done, 检索会静默返回 0 命中并降级到 web 兜底 —— 表面正常, RAG 已失效。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from eduagents_api.vector_reconcile import reconcile_vectors
from eduagents_core.config import get_settings
from eduagents_core.vectorstore.qdrant_store import QdrantStore


def _status_exit_code(status: str) -> int:
    return {"ok": 0, "drift": 1, "error": 2}.get(status, 2)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    store = QdrantStore(settings)
    try:
        # 枚举 kb 需要一个 tenant GUC 落脚点 (随后立刻 set app.is_admin 放行);
        # 逐库计数仍回到各自租户的 session, 见 vector_reconcile 模块 docstring。
        report = await reconcile_vectors(
            store, args.admin_tenant or "00000000-0000-0000-0000-000000000000",
            kb_id=args.kb,
        )
    finally:
        store.close()

    if args.json:
        print(json.dumps(report.as_dict(include_all=args.all), ensure_ascii=False, indent=2))
    else:
        _print_human(report, show_all=args.all)

    return _status_exit_code(report.status)


def _print_human(report, *, show_all: bool) -> None:
    rows = report.rows if show_all else report.unhealthy_rows
    print(f"知识库总数: {len(report.rows)}   漂移: {len(report.unhealthy_rows)}")
    print(f"缺失向量 (PG 标 done 但检索不到): {report.missing_vectors}")
    print(f"孤儿点   (Qdrant 有但 PG 无):     {report.orphan_points}")
    if report.missing_points:
        print(f"缺失 point/chunk ID: {', '.join(sorted(report.missing_points))}")
    if report.extra_points:
        print(f"多余 point ID: {', '.join(sorted(report.extra_points))}")
    if report.wrong_tenant_points:
        print(f"租户不匹配 point ID: {', '.join(sorted(report.wrong_tenant_points))}")
    if report.malformed_points:
        print(f"格式错误 point: {len(report.malformed_points)}")
    if report.document_inconsistencies:
        print(f"文档不一致: {len(report.document_inconsistencies)}")
    if rows:
        print()
        print(f"{'kb_id':38} {'PG_done':>8} {'Qdrant':>8} {'drift':>7}  name")
        for r in sorted(rows, key=lambda x: -abs(x.drift)):
            print(
                f"{r.kb_id:38} {r.pg_done:>8} {r.qdrant_points:>8} "
                f"{r.drift:>+7}  {r.name[:30]}"
            )
    if report.orphan_kbs:
        print()
        print(f"已删知识库的残留向量 ({len(report.orphan_kbs)} 个 kb):")
        print(f"{'kb_id':38} {'points':>8}  tenant_id")
        for o in sorted(report.orphan_kbs, key=lambda x: -x.qdrant_points):
            print(f"{o.kb_id:38} {o.qdrant_points:>8}  {o.tenant_id}")
    for err in report.errors:
        print(f"  [错误] kb={err['kb_id']}: {err['error']}", file=sys.stderr)
    print()
    if report.errors:
        print("结论: 对账过程出错, 结果不完整。")
    elif report.status == "drift":
        parts = []
        if report.missing_vectors:
            parts.append("drift > 0 的库需要重新摄取才能恢复检索")
        if report.orphan_kbs:
            parts.append("已删库的残留向量应清理 (含原文, 属删除不完整)")
        if report.wrong_tenant_points:
            parts.append("租户不匹配的向量必须隔离")
        if report.malformed_points:
            parts.append("格式错误的向量需要人工核查")
        if report.document_inconsistencies:
            parts.append("文档状态与子块索引不一致")
        print("结论: 存在漂移。" + "; ".join(parts) + "。")
    else:
        print("结论: 两侧一致。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", help="只对账这一个 kb_id")
    parser.add_argument("--all", action="store_true", help="连一致的库一起列出")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--admin-tenant",
        help="枚举 kb 时落脚的 tenant GUC (任意合法 uuid 即可, 随后走 is_admin 放行)",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
