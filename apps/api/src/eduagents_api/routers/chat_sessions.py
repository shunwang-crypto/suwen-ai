from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select, text

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import ChatAttachment, ChatMessage, ChatSession
from ..schemas import MessageOut, SessionOut, SessionRename
from .chat_support import _active_runs, _session_runs

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get("/sessions/{session_id}/run")
async def session_run_status(session_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    """查询会话是否仍有后台生成，用于离开页面后恢复等待状态。"""
    async with tenant_session(ctx.tenant_id) as session:
        owner = await session.get(ChatSession, session_id)
        if owner is None or owner.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
    run_id = _session_runs.get(session_id)
    run = _active_runs.get(run_id or "")
    if run is None or run.task.done():
        return {"status": "idle", "run_id": None}
    return {"status": "running", "run_id": run_id}


@router.post("/runs/{run_id}/stop", status_code=202)
async def stop_chat_run(run_id: str, ctx: AuthContext = Depends(api_rate_limited)):
    """只有显式点击“停止”才取消后台生成；页面/会话切换不会调用本接口。"""
    run = _active_runs.get(run_id)
    if run is None or run.task.done() or run.user_id != ctx.user_id:
        raise HTTPException(status_code=404, detail="CHAT_RUN_NOT_FOUND")
    run.task.cancel()
    return {"status": "stopping"}


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.scalars(
                select(ChatSession)
                .where(ChatSession.user_id == ctx.user_id)
                .order_by(ChatSession.updated_at.desc())
                .limit(100)
            )
        ).all()
        return [
            SessionOut(id=r.id, title=r.title, created_at=r.created_at, updated_at=r.updated_at)
            for r in rows
        ]


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def session_messages(session_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        owner = await session.get(ChatSession, session_id)
        if owner is None or owner.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        rows = (
            await session.scalars(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.asc())
            )
        ).all()
        return [
            MessageOut(
                id=r.id,
                role=r.role,
                content=r.content,
                created_at=r.created_at,
                meta=r.meta_json or {},
            )
            for r in rows
        ]


@router.patch("/sessions/{session_id}", response_model=SessionOut)
async def rename_session(
    session_id: uuid.UUID,
    body: SessionRename,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id) as session:
        row = await session.get(ChatSession, session_id)
        if row is None or row.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        row.title = body.title
        await session.flush()
        return SessionOut(
            id=row.id, title=row.title, created_at=row.created_at, updated_at=row.updated_at
        )


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id) as session:
        row = await session.get(ChatSession, session_id)
        if row is None or row.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        attachment_ids = (
            await session.scalars(
                select(ChatAttachment.id).where(ChatAttachment.session_id == session_id)
            )
        ).all()
        await session.execute(delete(ChatSession).where(ChatSession.id == session_id))
        # P0-4 (checkpoint 绑定): qa thread_id = "qa:{uid}:{sid}:{turn}", 删会话
        # 同事务按前缀清 LangGraph 三表, 明文问答不残留。表由 checkpointer.setup()
        # 建 (不在 Alembic), 环境未启用图运行时可能不存在 — to_regclass 先探。
        has_ckpt = await session.scalar(
            text("SELECT to_regclass('public.checkpoints') IS NOT NULL")
        )
        if has_ckpt:
            pattern = f"qa:{ctx.user_id}:{session_id}:%"
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id LIKE :pat"),  # noqa: S608 — 表名白名单常量
                    {"pat": pattern},
                )
    for attachment_id in attachment_ids:
        await request.app.state.storage.delete_prefix(
            f"{ctx.tenant_id}/chat-attachments/{attachment_id}"
        )
