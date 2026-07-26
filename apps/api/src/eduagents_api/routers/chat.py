from __future__ import annotations

import uuid

import orjson
from eduagents_core.gateway import (
    BudgetExceeded,
    ChatResult,
    StreamDelta,
    Tier,
    UsageEvent,
)
from eduagents_core.gateway import (
    ChatMessage as GwMessage,
)
from eduagents_core.gateway import (
    ChatRequest as GwRequest,
)
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sse_starlette.sse import EventSourceResponse

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import ChatMessage, ChatSession, Document, KnowledgeBase
from ..schemas import ChatRequestIn, MessageOut, SessionOut, SessionRename
from ..usage import persist_usage

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
    # Wave3 联合检索: kb_ids 为准; 旧单值 kb_id 向后兼容 (等价 kb_ids=[kb_id])
    kb_ids: list[uuid.UUID] = list(
        dict.fromkeys(body.kb_ids or ([body.kb_id] if body.kb_id is not None else []))
    )
    if kb_ids and runtime is None:
        raise HTTPException(status_code=503, detail="RETRIEVAL_UNAVAILABLE")

    # PG 侧可见性预校验 (授权唯一来源; Qdrant filter 只是纵深兜底):
    # 任何不可见 kb (不存在 / 非本人 / 非成员机构) 一律 403, 不泄漏存在性差异
    owner_ids: list[str] = []
    kb_names: dict[str, str] = {}
    kb_versions: dict[str, int] = {}
    if kb_ids:
        async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
            rows = (
                await session.scalars(
                    select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids))
                )
            ).all()
            visible = {r.id for r in rows}
            if any(k not in visible for k in kb_ids):
                raise HTTPException(status_code=403, detail="KB_FORBIDDEN")
            owner_ids = sorted({str(r.tenant_id) for r in rows})
            kb_names = {str(r.id): r.name for r in rows}
            kb_versions = {str(r.id): r.kb_version for r in rows}

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
        if kb_ids:
            # ---- 两级语义缓存 (包B, §7): 仅单库+首轮 (缓存键不含会话历史,
            # 多轮上下文相关答案禁止缓存) ----
            cache = getattr(request.app.state, "answer_cache", None)
            cache_embedder = getattr(request.app.state, "embedder", None)
            cacheable = (
                len(kb_ids) == 1 and not history
                and cache is not None and cache_embedder is not None
            )
            if cacheable:
                try:
                    hit = await cache.get(
                        str(ctx.tenant_id), str(kb_ids[0]),
                        kb_versions[str(kb_ids[0])], body.message, cache_embedder,
                    )
                except Exception:  # noqa: BLE001 — 缓存故障不阻塞主链路
                    log.exception("answer_cache_get_failed")
                    hit = None
                if hit is not None:
                    yield {
                        "event": "trace_event",
                        "data": _sse({
                            "type": "cache", "source": hit.source,
                            "matched_query": hit.matched_query, "score": hit.score,
                        }),
                    }
                    for i, c in enumerate(hit.citations, 1):
                        yield {"event": "citation", "data": _sse({**c, "idx": i})}
                    for i in range(0, len(hit.answer), 24):
                        yield {
                            "event": "token",
                            "data": _sse({"text": hit.answer[i : i + 24], "attempt": 0}),
                        }
                    await _persist_assistant(
                        hit.answer, None, hit.citations,
                        extra_meta={"route": "kb", "cache": hit.source,
                                    "followups": hit.followups},
                    )
                    # 命中计量 (F10 缓存节省口径, 见 ops.py): cached_tokens=原答案输出量
                    await persist_usage(UsageEvent(
                        request_id=request_id, tenant_id=str(ctx.tenant_id),
                        user_id=str(ctx.user_id), feature="qa_cache",
                        model=hit.model, tier=hit.tier, prompt_tokens=0,
                        completion_tokens=0, cached_tokens=hit.completion_tokens,
                        cost_cny=0.0, latency_ms=0, mode="cache",
                    ))
                    yield {
                        "event": "followups",
                        "data": _sse({"items": hit.followups}),
                    }
                    yield {
                        "event": "done",
                        "data": _sse({
                            "session_id": str(session_id),
                            "citations": len(hit.citations),
                            "refused": False, "route": "kb",
                            "usage": {"mode": "cache", "cost_cny": 0.0},
                        }),
                    }
                    return

            thread = runtime.thread_id("qa", str(ctx.user_id), uuid.uuid4().hex)
            payload = {
                "messages": [
                    {"role": m.role, "content": m.content} for m in history
                ],
                "query": body.message,
                "tenant_id": str(ctx.tenant_id),
                "user_id": str(ctx.user_id),
                "request_id": request_id,
                # kb_id 保留为首库 (路由判定/旧路径兼容); 检索层用 kb_ids+owner_ids
                "kb_id": str(kb_ids[0]),
                "kb_ids": [str(k) for k in kb_ids],
                "owner_ids": owner_ids,
            }
            state: dict = {}
            answer_parts: list[str] = []  # 当前 attempt 已下发的 token 累积
            attempt = 0
            try:
                async for ev in runtime.astream_updates("qa", thread, payload):
                    if ev["kind"] == "token":
                        # 图内真流式 token (generate 节点 get_stream_writer 写出);
                        # attempt 变化 = verify 触发重生成, 前端按此清空重排
                        ev_attempt = int(ev.get("attempt", 0))
                        if ev_attempt != attempt:
                            attempt = ev_attempt
                            answer_parts = []
                        answer_parts.append(ev["text"])
                        yield {
                            "event": "token",
                            "data": _sse({"text": ev["text"], "attempt": ev_attempt}),
                        }
                    elif ev["kind"] == "node":
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
                            # org_ids 注入: 机构库文档行在 org 只读策略下可见
                            async with tenant_session(
                                ctx.tenant_id, org_ids=ctx.org_ids
                            ) as session:
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
                                    # 联合检索: 标注引用来自哪个库
                                    "kb_id": e.get("kb_id", ""),
                                    "kb_name": kb_names.get(e.get("kb_id", ""), ""),
                                    "heading_path": e.get("heading_path", ""),
                                    "page": e.get("page_start"),
                                    "highlight": (e.get("text") or "")[:160],
                                    "sanitized": bool(e.get("sanitized")),
                                }
                                citations.append(citation)
                                yield {"event": "citation", "data": _sse(citation)}
                    elif ev["kind"] == "end":
                        state = ev["state"]
                    # 其余 kind (custom 等) 向前兼容: 忽略
            except BudgetExceeded:
                yield {
                    "event": "error",
                    "data": _sse({
                        "code": "BUDGET_EXCEEDED",
                        "message": "今日 AI 用量已达上限，明天再来或联系管理员",
                    }),
                }
                return
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
            # 图内已真流式; 这里只补差量: 拒答路径不经 generate (streamed 为空),
            # verify 二次仍不支持时免责声明追加在答案尾部
            streamed = "".join(answer_parts)
            if answer != streamed:
                if not answer.startswith(streamed):
                    attempt += 1  # 流内容与终态不一致 (罕见): 换 attempt 整段重发
                    streamed = ""
                tail = answer[len(streamed) :]
                for i in range(0, len(tail), 24):
                    yield {
                        "event": "token",
                        "data": _sse({"text": tail[i : i + 24], "attempt": attempt}),
                    }
            await _persist_assistant(
                answer,
                None,
                citations,
                extra_meta={
                    "route": state.get("route", ""),
                    "refused": state.get("refused", False),
                    "followups": state.get("followups") or [],
                },
            )
            # 缓存写入: 仅自省环通过的最终答案 (非拒答/无免责段/单库/首轮, 包B约定)
            if (
                cacheable and answer and not state.get("refused")
                and "⚠️" not in answer and state.get("route") == "kb"
            ):
                try:
                    await cache.put(
                        str(ctx.tenant_id), str(kb_ids[0]),
                        kb_versions[str(kb_ids[0])], body.message, cache_embedder,
                        answer=answer, citations=citations,
                        followups=state.get("followups") or [],
                        model=state.get("model", ""), tier="standard",
                        completion_tokens=len(answer),
                    )
                except Exception:  # noqa: BLE001
                    log.exception("answer_cache_put_failed")
            # 追问 chips (包A): done 前固定下发; 生成失败降级为空列表, 不阻塞
            yield {
                "event": "followups",
                "data": _sse({"items": state.get("followups") or []}),
            }
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
