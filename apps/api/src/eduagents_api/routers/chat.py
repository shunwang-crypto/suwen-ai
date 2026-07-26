from __future__ import annotations

import uuid

import orjson
from eduagents_core.gateway import (
    ChatMessage as GwMessage,
)
from eduagents_core.gateway import (
    ChatRequest as GwRequest,
)
from eduagents_core.gateway import (
    ChatResult,
    StreamDelta,
    Tier,
)
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sse_starlette.sse import EventSourceResponse

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import ChatMessage, ChatSession, Document
from ..schemas import ChatRequestIn, MessageOut, SessionOut, SessionRename

router = APIRouter(prefix="/api/chat", tags=["chat"])
log = get_logger("chat")

_SYSTEM_PROMPT = (
    "你是 EduAgents 平台的 AI 助教，面向自学者与学生。"
    "回答准确、结构清晰、循序渐进；数学符号一律使用 LaTeX（行内 $..$，块级 $$..$$）。"
    "不确定的内容明确说明不确定，不要编造。"
)

# kb 路径的 RAG 防注入/引用/拒答文案由 qa 子图内联维护 (graphs/qa_graph.py)
_HISTORY_LIMIT = 20  # M3 接入 LangGraph checkpointer 后由滚动摘要接管


def _sse(data: dict) -> str:
    return orjson.dumps(data).decode()


@router.post("")
async def chat(
    body: ChatRequestIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    gateway = request.app.state.gateway
    runtime = getattr(request.app.state, "graphs", None)
    request_id = getattr(request.state, "request_id", "")
    if body.kb_id is not None and runtime is None:
        raise HTTPException(status_code=503, detail="RETRIEVAL_UNAVAILABLE")

    # 会话与历史在流外准备好, 流内不持有长事务
    async with tenant_session(ctx.tenant_id) as session:
        if body.session_id is not None:
            chat_session = await session.get(ChatSession, body.session_id)
            if chat_session is None or chat_session.user_id != ctx.user_id:
                raise HTTPException(status_code=404, detail="NOT_FOUND")
        else:
            chat_session = ChatSession(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                title=body.message[:30],
            )
            session.add(chat_session)
            await session.flush()
        session_id = chat_session.id
        history_rows = (
            await session.scalars(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(_HISTORY_LIMIT)
            )
        ).all()
        session.add(
            ChatMessage(
                tenant_id=ctx.tenant_id,
                session_id=session_id,
                role="user",
                content=body.message,
            )
        )
    history: list[GwMessage] = [
        GwMessage(role=row.role, content=row.content)
        for row in reversed(history_rows)
        if row.role in ("user", "assistant")
    ]

    async def _persist_assistant(
        text: str,
        final: ChatResult | None,
        citations: list[dict],
        extra_meta: dict | None = None,
    ):
        async with tenant_session(ctx.tenant_id) as session:
            session.add(
                ChatMessage(
                    tenant_id=ctx.tenant_id,
                    session_id=session_id,
                    role="assistant",
                    content=text,
                    meta_json={
                        "mode": final.mode if final else "graph",
                        "model": final.model if final else "",
                        "degraded": final.degraded if final else False,
                        "citations": citations,
                        **(extra_meta or {}),
                    },
                )
            )

    async def event_stream():
        citations: list[dict] = []

        # ---- 知识库路径: qa 子图 (Agentic RAG 自省环 §5.3, M3 起唯一实现) ----
        if body.kb_id is not None:
            thread = runtime.thread_id("qa", str(ctx.user_id), uuid.uuid4().hex)
            payload = {
                "messages": [
                    {"role": m.role, "content": m.content} for m in history
                ],
                "query": body.message,
                "tenant_id": str(ctx.tenant_id),
                "user_id": str(ctx.user_id),
                "request_id": request_id,
                "kb_id": str(body.kb_id),
            }
            state: dict = {}
            try:
                async for ev in runtime.astream_updates("qa", thread, payload):
                    if ev["kind"] == "node":
                        delta = ev["delta"]
                        yield {
                            "event": "node_update",
                            "data": _sse({"node": ev["node"], "status": "done"}),
                        }
                        for tr in delta.get("trace") or []:
                            yield {"event": "trace_event", "data": _sse(tr)}
                        evidences = delta.get("evidences") or []
                        if evidences:
                            doc_ids = {
                                e["doc_id"] for e in evidences if e.get("doc_id")
                            }
                            async with tenant_session(ctx.tenant_id) as session:
                                rows = (
                                    await session.scalars(
                                        select(Document).where(
                                            Document.id.in_(
                                                [uuid.UUID(d) for d in doc_ids]
                                            )
                                        )
                                    )
                                ).all()
                                doc_names = {str(r.id): r.filename for r in rows}
                            citations = []
                            for i, e in enumerate(evidences, 1):
                                citation = {
                                    "idx": i,
                                    "chunk_id": e.get("chunk_id", ""),
                                    "doc_id": e.get("doc_id", ""),
                                    "doc_name": doc_names.get(
                                        e.get("doc_id", ""), "未知文档"
                                    ),
                                    "heading_path": e.get("heading_path", ""),
                                    "page": e.get("page_start"),
                                    "highlight": (e.get("text") or "")[:160],
                                    "sanitized": bool(e.get("sanitized")),
                                }
                                citations.append(citation)
                                yield {"event": "citation", "data": _sse(citation)}
                    else:  # end
                        state = ev["state"]
            except Exception:
                log.exception("qa_graph_failed")
                yield {
                    "event": "error",
                    "data": _sse(
                        {"code": "GENERATION_FAILED", "message": "回答生成失败，请重试"}
                    ),
                }
                return
            answer = state.get("answer", "")
            # 图内生成为整段输出, 此处按块下发 token 事件 (真流式优化: decisions D13)
            for i in range(0, len(answer), 24):
                yield {"event": "token", "data": _sse({"text": answer[i : i + 24]})}
            await _persist_assistant(
                answer,
                None,
                citations,
                extra_meta={
                    "route": state.get("route", ""),
                    "refused": state.get("refused", False),
                },
            )
            yield {
                "event": "done",
                "data": _sse(
                    {
                        "session_id": str(session_id),
                        "citations": len(citations),
                        "refused": state.get("refused", False),
                        "route": state.get("route", ""),
                        "usage": {},
                    }
                ),
            }
            return

        # ---- 通用路径: 直连网关, 真 token 流 ----
        messages = [GwMessage(role="system", content=_SYSTEM_PROMPT), *history,
                    GwMessage(role="user", content=body.message)]
        gw_req = GwRequest(
            messages=tuple(messages),
            tier=Tier.standard,
            feature="chat",
            tenant_id=str(ctx.tenant_id),
            user_id=str(ctx.user_id),
            request_id=request_id,
        )
        yield {
            "event": "node_update",
            "data": _sse({"node": "generate", "status": "running", "detail": "生成回答中"}),
        }
        parts: list[str] = []
        final: ChatResult | None = None
        try:
            async for item in gateway.stream_chat(gw_req):
                if isinstance(item, StreamDelta):
                    parts.append(item.text)
                    yield {"event": "token", "data": _sse({"text": item.text})}
                else:
                    final = item
        except Exception:
            log.exception("chat_stream_failed")
            yield {
                "event": "error",
                "data": _sse(
                    {"code": "GENERATION_FAILED", "message": "回答生成失败，请重试"}
                ),
            }
            return
        answer = "".join(parts)
        await _persist_assistant(answer, final, citations)
        yield {
            "event": "done",
            "data": _sse(
                {
                    "session_id": str(session_id),
                    "citations": len(citations),
                    "usage": {
                        "cost_cny": final.cost_cny if final else 0.0,
                        "mode": final.mode if final else "unknown",
                        "latency_ms": final.latency_ms if final else 0,
                    },
                }
            ),
        }

    return EventSourceResponse(event_stream())


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
            SessionOut(
                id=r.id, title=r.title, created_at=r.created_at, updated_at=r.updated_at
            )
            for r in rows
        ]


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def session_messages(
    session_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)
):
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
            MessageOut(id=r.id, role=r.role, content=r.content, created_at=r.created_at)
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
    session_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)
):
    async with tenant_session(ctx.tenant_id) as session:
        row = await session.get(ChatSession, session_id)
        if row is None or row.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        await session.execute(delete(ChatSession).where(ChatSession.id == session_id))
