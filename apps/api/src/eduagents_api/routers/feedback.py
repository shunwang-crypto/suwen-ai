"""用户反馈通道 (P2-1): 报障/答错/内容投诉的最小工单闭环。

- 表结构见 alembic 0004 (手写迁移); models.py 归并行 P0 工作流所有, 本路由
  一律 sqlalchemy text SQL, 零 ORM 依赖 —— 这是当次并行开发的所有权隔离决定。
- POST /api/feedback: 登录用户提交, 自动携带最近一次请求的 X-Request-ID
  (前端从响应头取; 未传则回落当前请求的 request_id), 报障可与日志/用量串联。
- GET /api/feedback: 我的反馈列表 (RLS + user_id 双保险)。
- GET/PATCH /api/admin/feedback: admin 列表 (status 筛选) 与关闭工单,
  走事务级 app.is_admin GUC (惯例同 ops.py/D7); 不动 admin.py (所有权隔离)。

主线接线 (main.py, 各一行):
    from .routers import feedback
    app.include_router(feedback.router)
    app.include_router(feedback.admin_router)
"""

from __future__ import annotations

import uuid

from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, require_admin

log = get_logger("feedback")

router = APIRouter(prefix="/api/feedback", tags=["feedback"])
admin_router = APIRouter(prefix="/api/admin/feedback", tags=["admin"])

CATEGORIES = ("bug", "answer_wrong", "abuse", "other")
_MY_LIST_LIMIT = 100

_COLS = "id, request_id, category, content, page_url, status, created_at"


class FeedbackIn(BaseModel):
    category: str = Field(min_length=1, max_length=16)
    content: str = Field(min_length=1, max_length=4000)
    page_url: str = Field(default="", max_length=512)
    # 前端从最近一次 API 响应头 X-Request-ID 取值传入; 缺省回落当前请求 id
    request_id: str = Field(default="", max_length=64)


class FeedbackPatch(BaseModel):
    status: str = Field(min_length=1, max_length=8)


def _row_out(row) -> dict:
    return {
        "id": str(row.id),
        "request_id": row.request_id,
        "category": row.category,
        "content": row.content,
        "page_url": row.page_url,
        "status": row.status,
        "created_at": row.created_at,
    }


@router.post("", status_code=201)
async def create_feedback(
    body: FeedbackIn, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    if body.category not in CATEGORIES:
        raise HTTPException(status_code=422, detail="INVALID_CATEGORY")
    request_id = body.request_id or getattr(request.state, "request_id", "")
    feedback_id = uuid.uuid4()
    async with tenant_session(ctx.tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "INSERT INTO feedback "
                    "(id, tenant_id, user_id, request_id, category, content, page_url, status) "
                    "VALUES (:id, :tid, :uid, :rid, :category, :content, :page_url, 'open') "
                    f"RETURNING {_COLS}"
                ),
                {
                    "id": feedback_id,
                    "tid": ctx.tenant_id,
                    "uid": ctx.user_id,
                    "rid": request_id[:64],
                    "category": body.category,
                    "content": body.content,
                    "page_url": body.page_url,
                },
            )
        ).one()
        payload = _row_out(row)
    log.info("feedback_created", feedback_id=str(feedback_id), category=body.category)
    return payload


@router.get("")
async def list_my_feedback(ctx: AuthContext = Depends(api_rate_limited)):
    """我的反馈 (RLS 已按 tenant 隔离, user_id 条件双保险)。"""
    async with tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT {_COLS} FROM feedback WHERE user_id = :uid "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"uid": ctx.user_id, "limit": _MY_LIST_LIMIT},
            )
        ).all()
    return {"items": [_row_out(r) for r in rows], "total": len(rows)}


@admin_router.get("")
async def admin_list_feedback(
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx: AuthContext = Depends(require_admin),
):
    if status is not None and status not in ("open", "closed"):
        raise HTTPException(status_code=422, detail="INVALID_STATUS")
    where = "WHERE f.status = :status" if status is not None else ""
    params: dict = {"limit": page_size, "offset": (page - 1) * page_size}
    if status is not None:
        params["status"] = status
    async with tenant_session(ctx.tenant_id, is_admin=True) as session:
        total = int(
            await session.scalar(
                text(f"SELECT count(*) FROM feedback f {where}"), params
            )
            or 0
        )
        rows = (
            await session.execute(
                text(
                    "SELECT f.id, f.user_id, f.request_id, f.category, f.content, "
                    "f.page_url, f.status, f.created_at, u.email "
                    "FROM feedback f LEFT JOIN users u ON u.id = f.user_id "
                    f"{where} ORDER BY f.created_at DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        ).all()
    items = [
        {**_row_out(r), "user_id": str(r.user_id), "email": r.email} for r in rows
    ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@admin_router.patch("/{feedback_id}")
async def admin_patch_feedback(
    feedback_id: uuid.UUID,
    body: FeedbackPatch,
    ctx: AuthContext = Depends(require_admin),
):
    """关闭/重开工单 (admin)。RLS UPDATE 策略仅放行 is_admin GUC。"""
    if body.status not in ("open", "closed"):
        raise HTTPException(status_code=422, detail="INVALID_STATUS")
    async with tenant_session(ctx.tenant_id, is_admin=True) as session:
        row = (
            await session.execute(
                text(
                    "UPDATE feedback SET status = :status WHERE id = :id "
                    f"RETURNING {_COLS}"
                ),
                {"status": body.status, "id": feedback_id},
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        payload = _row_out(row)
    # admin 写操作留痕 (P2-10 审计口径)
    log.warning(
        "admin_feedback_status_changed",
        feedback_id=str(feedback_id),
        status=body.status,
        admin_user=str(ctx.user_id),
    )
    return payload
