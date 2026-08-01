"""F9 评估看板 API (仅管理员)。

eval_runs 表从 M0 起就存在但从未被写入 —— 评测结果由明确环境的临时产物导入。本路由
把它接通: 导入新产物 → 列表/详情/对比 → 页面上一键重跑。

RLS 说明: eval_runs **不在** TENANT_TABLES (平台级评测数据, 与租户无关), 因此全部
走 plain_session; 访问控制完全由 require_admin 承担。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import plain_session
from ..deps import AuthContext, require_admin
from ..evals_ingest import import_evidence
from ..models import EvalRun

router = APIRouter(prefix="/api/evals", tags=["evals"])


class RunEvalIn(BaseModel):
    kb_id: uuid.UUID
    tenant_id: uuid.UUID | None = None  # 省略 = 用调用者自己的租户
    golden: str = Field(default="eval/golden_set/v2.jsonl", max_length=255)
    no_sparse: bool = False
    no_rerank: bool = False
    label: str = Field(default="", max_length=200)


def _row_brief(row: EvalRun) -> dict[str, Any]:
    cfg = row.config_json or {}
    return {
        "id": str(row.id),
        "kind": cfg.get("kind", (row.metrics_json or {}).get("kind", "unknown")),
        "label": cfg.get("label", ""),
        "source": cfg.get("source", ""),
        "at": row.at.isoformat() if row.at else None,
        "generated_at": cfg.get("generated_at", ""),
        "corpus_version": row.corpus_version,
        "golden_set_version": row.golden_set_version,
        "data_basis": cfg.get("data_basis", ""),
        "summary": cfg.get("summary", []),
    }


@router.post("/import")
async def import_runs(ctx: AuthContext = Depends(require_admin)):
    """扫描临时评估目录中的新产物并入库 (按内容哈希幂等)。"""
    return await import_evidence()


@router.get("")
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    kind: str | None = Query(default=None, pattern="^(retrieval|simulation)$"),
    ctx: AuthContext = Depends(require_admin),
):
    async with plain_session() as session:
        stmt = select(EvalRun).order_by(EvalRun.at.desc()).limit(limit)
        rows = (await session.scalars(stmt)).all()
    briefs = [_row_brief(r) for r in rows]
    if kind:
        briefs = [b for b in briefs if b["kind"] == kind]
    return {"runs": briefs}


@router.get("/compare")
async def compare_runs(
    ids: str = Query(description="两个 eval_run id, 逗号分隔"),
    ctx: AuthContext = Depends(require_admin),
):
    """同名指标 delta。两次跑分的 metrics 先摊平成点号键, 再取交集比较。

    左 = ids 里的第一个 (基线), 右 = 第二个 (新)。delta = 右 - 左, 因此 delta>0
    读作"新的更高" —— 是不是"更好"由指标语义决定, 看板不替你判断方向。
    """
    parts = [p.strip() for p in ids.split(",") if p.strip()]
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="COMPARE_NEEDS_TWO_IDS")
    try:
        left_id, right_id = (uuid.UUID(p) for p in parts)
    except ValueError:
        raise HTTPException(status_code=400, detail="INVALID_ID") from None

    async with plain_session() as session:
        left = await session.get(EvalRun, left_id)
        right = await session.get(EvalRun, right_id)
    if left is None or right is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    flat_l = _flatten(left.metrics_json or {})
    flat_r = _flatten(right.metrics_json or {})
    shared = sorted(set(flat_l) & set(flat_r))
    deltas = []
    for metric in shared:
        lv, rv = flat_l[metric], flat_r[metric]
        delta = round(rv - lv, 6)
        pct = round(delta / abs(lv) * 100, 2) if lv else None
        deltas.append(
            {"metric": metric, "left": lv, "right": rv, "delta": delta, "pct": pct}
        )
    brief_l, brief_r = _row_brief(left), _row_brief(right)
    return {
        "left": brief_l,
        "right": brief_r,
        "same_kind": brief_l["kind"] == brief_r["kind"],
        "deltas": deltas,
        "left_only": sorted(set(flat_l) - set(flat_r)),
        "right_only": sorted(set(flat_r) - set(flat_l)),
    }


@router.post("/run")
async def run_eval(
    body: RunEvalIn, request: Request, ctx: AuthContext = Depends(require_admin)
):
    """把一次检索评估投进 ARQ 队列 (F9-A1 的"一键触发")。

    评估要加载 BGE-M3 + reranker 并逐问打 60 次检索, 秒级到分钟级 —— 绝不能占着
    HTTP 请求, 也不该跑在 API 进程里污染在线服务的内存。
    """
    arq = getattr(request.app.state, "arq", None)
    if arq is None:
        raise HTTPException(status_code=503, detail="QUEUE_UNAVAILABLE")
    job = await arq.enqueue_job(
        "run_retrieval_eval_task",
        str(body.kb_id),
        str(body.tenant_id or ctx.tenant_id),
        body.golden,
        body.no_sparse,
        body.no_rerank,
        body.label,
    )
    if job is None:  # 同 job_id 已在队列 (arq 去重)
        raise HTTPException(status_code=409, detail="JOB_ALREADY_QUEUED")
    return {"job_id": job.job_id, "status": "queued"}


@router.get("/jobs/{job_id}")
async def job_status(
    job_id: str, request: Request, ctx: AuthContext = Depends(require_admin)
):
    """前端轮询用。arq 结果过期后会变成 not_found —— 那时列表里已有新行可看。"""
    from arq.jobs import Job

    arq = getattr(request.app.state, "arq", None)
    if arq is None:
        raise HTTPException(status_code=503, detail="QUEUE_UNAVAILABLE")
    job = Job(job_id, redis=arq)
    status = (await job.status()).value
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "success": None,
        "eval_run_id": None,
        "error": None,
    }
    info = await job.result_info()
    if info is not None:
        payload["success"] = bool(info.success)
        result = info.result
        if info.success and isinstance(result, dict):
            payload["eval_run_id"] = result.get("eval_run_id")
            payload["error"] = result.get("error")
        elif not info.success:
            payload["error"] = str(result)
    return payload


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, ctx: AuthContext = Depends(require_admin)):
    async with plain_session() as session:
        row = await session.get(EvalRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return {**_row_brief(row), "config": row.config_json, "metrics": row.metrics_json}


def _flatten(node: Any, prefix: str = "") -> dict[str, float]:
    """把 metrics_json 里的数值叶子摊平成 {"a.b.c": 1.0}。

    - 只保留 int/float (bool 排除: True/False 做 delta 没有意义)
    - 列表里的 dict 用其 category/variant/arm 名做键, 而非下标 —— 两次跑分若
      category 顺序不同, 用下标会把"表格数据查询"和"多跳推理"对着比。
    - mae_curve_mean 这类纯数值序列直接跳过 (逐点比较没有可读性)。
    """
    out: dict[str, float] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(_flatten(value, f"{prefix}{key}."))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if not isinstance(item, dict):
                return out  # 纯标量序列 (曲线/seed 明细): 不参与对比
            name = item.get("category") or item.get("variant") or item.get("qid") or str(i)
            out.update(_flatten(item, f"{prefix}{name}."))
    elif isinstance(node, bool):
        return out
    elif isinstance(node, int | float):
        out[prefix.rstrip(".")] = float(node)
    return out
