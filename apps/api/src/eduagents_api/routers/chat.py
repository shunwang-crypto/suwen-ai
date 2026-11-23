from __future__ import annotations

import asyncio
import uuid

from eduagents_core.gateway import (
    ChatMessage as GwMessage,
)
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sse_starlette.sse import EventSourceResponse

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import ChatAttachment, ChatMessage, ChatSession, KnowledgeBase
from ..profile_summary import ProfileContext, build_profile_context
from ..schemas import ChatRequestIn
from .chat_stream import _ChatStreamContext, _event_stream
from .chat_support import (  # noqa: F401 — 部分名称供本模块端点与 main.py shutdown 注册复用
    _BLOCKED_FALLBACK,
    _SYSTEM_PROMPT,
    _active_runs,
    _ActiveChatRun,
    _attachment_summary,
    _bg_tasks,
    _cache_admissible,
    _direct_system_prompt,
    _document_excerpts,
    _gateway_user_content,
    _load_minor_flag,
    _maybe_enqueue_reflection,
    _new_mod_buffer,
    _query_terms,
    _safe_gateway_error,
    _session_runs,
    _spawn,
    _sse,
    _track_task,
    shutdown_chat_runs,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])
log = get_logger("chat")

# kb 路径的 RAG 防注入/引用/拒答文案由 qa 子图内联维护 (workflows/qa.py)
_HISTORY_LIMIT = 20  # 对话历史按轮次限定长度




@router.post("")
async def chat(
    body: ChatRequestIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    gateway = request.app.state.gateway
    settings = request.app.state.settings
    runtime = getattr(request.app.state, "harness", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="GRAPHS_UNAVAILABLE")
    request_id = getattr(request.state, "request_id", "")
    # Wave3 联合检索: kb_ids 为准; 旧单值 kb_id 向后兼容 (等价 kb_ids=[kb_id])
    requested_kb_ids: list[uuid.UUID] = list(
        dict.fromkeys(body.kb_ids or ([body.kb_id] if body.kb_id is not None else []))
    )
    if body.kb_id is not None and not body.kb_ids:
        log.info(
            "legacy_kb_id_compat_used",
            surface="chat",
            replacement="kb_ids",
            metric="eduagents_legacy_kb_id_calls_total",
            increment=1,
        )
    # 未携带 mode 的旧客户端继续沿用“有 kb_id 即知识库问答”的历史契约；
    # 新客户端把 kb_ids 作为独立资料范围，可与 fast/think 组合。
    mode_explicit = "mode" in body.model_fields_set
    # kb 是旧客户端模式名；新契约把“资料范围”和“回答强度”拆开。
    legacy_kb_mode = body.mode == "kb" or (requested_kb_ids and not mode_explicit)
    chat_mode = "fast" if legacy_kb_mode else body.mode
    kb_ids = requested_kb_ids
    attachment_ids = list(dict.fromkeys(body.attachment_ids or []))
    if body.mode == "kb" and not kb_ids:
        raise HTTPException(status_code=400, detail="KB_MODE_REQUIRES_SCOPE")
    if chat_mode in {"think", "research"} and not (
        settings.strong_api_key and settings.strong_model
    ):
        raise HTTPException(status_code=503, detail="THINKING_MODEL_UNAVAILABLE")
    if chat_mode in {"web", "research"} and (
        runtime is None or not settings.tavily_api_key
    ):
        raise HTTPException(status_code=503, detail="WEB_SEARCH_UNAVAILABLE")
    if chat_mode in {"web", "research"} and len("".join(body.message.split())) < 2:
        raise HTTPException(status_code=400, detail="WEB_SEARCH_QUERY_TOO_SHORT")
    if chat_mode in {"web", "research"} and attachment_ids:
        raise HTTPException(status_code=400, detail="WEB_MODE_ATTACHMENTS_UNSUPPORTED")
    if chat_mode in {"web", "research"} and kb_ids:
        raise HTTPException(status_code=400, detail="WEB_MODE_KB_UNSUPPORTED")
    if (
        kb_ids
        and (runtime is None or getattr(request.app.state, "retrieval", None) is None)
        and not attachment_ids
    ):
        raise HTTPException(status_code=503, detail="RETRIEVAL_UNAVAILABLE")
    if len(attachment_ids) > settings.max_chat_attachments_per_message:
        raise HTTPException(status_code=400, detail="TOO_MANY_CHAT_ATTACHMENTS")

    # PG 侧可见性预校验 (授权唯一来源; Qdrant filter 只是纵深兜底):
    # 任何不可见 kb (不存在 / 非本人 / 非成员机构) 一律 403, 不泄漏存在性差异
    owner_ids: list[str] = []
    kb_names: dict[str, str] = {}
    kb_versions: dict[str, int] = {}
    if kb_ids:
        async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
            rows = (
                await session.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
            ).all()
            visible = {r.id for r in rows}
            if any(k not in visible for k in kb_ids):
                raise HTTPException(status_code=403, detail="KB_FORBIDDEN")
            owner_ids = sorted({str(r.tenant_id) for r in rows})
            kb_names = {str(r.id): r.name for r in rows}
            kb_versions = {str(r.id): r.kb_version for r in rows}

    # 会话与历史在流外准备好, 流内不持有长事务
    async with tenant_session(ctx.tenant_id) as session:
        current_attachments: list[ChatAttachment] = []
        if attachment_ids:
            attachment_rows = (
                await session.scalars(
                    select(ChatAttachment).where(ChatAttachment.id.in_(attachment_ids))
                )
            ).all()
            by_id = {row.id: row for row in attachment_rows}
            if len(by_id) != len(attachment_ids) or any(
                row.user_id != ctx.user_id or row.message_id is not None for row in attachment_rows
            ):
                raise HTTPException(status_code=404, detail="CHAT_ATTACHMENT_NOT_FOUND")
            current_attachments = [by_id[item_id] for item_id in attachment_ids]
            if sum(row.size_bytes for row in current_attachments) > (
                settings.max_chat_attachment_mb * 1024 * 1024
            ):
                raise HTTPException(status_code=413, detail="CHAT_ATTACHMENTS_TOTAL_TOO_LARGE")
            if any(row.kind == "image" for row in current_attachments) and not (
                settings.routing_vision_enabled
            ):
                raise HTTPException(status_code=503, detail="VISION_MODEL_UNAVAILABLE")
        if body.session_id is not None:
            chat_session = await session.get(ChatSession, body.session_id)
            if chat_session is None or chat_session.user_id != ctx.user_id:
                raise HTTPException(status_code=404, detail="NOT_FOUND")
            active_run_id = _session_runs.get(chat_session.id)
            active_run = _active_runs.get(active_run_id or "")
            if active_run is not None and not active_run.task.done():
                raise HTTPException(status_code=409, detail="CHAT_RUN_ALREADY_ACTIVE")
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
        user_message = ChatMessage(
            tenant_id=ctx.tenant_id,
            session_id=session_id,
            role="user",
            content=body.message,
            meta_json={
                "chat_mode": chat_mode,
                "kb_ids": [str(item) for item in kb_ids],
                **(
                    {"attachments": [_attachment_summary(row) for row in current_attachments]}
                    if current_attachments
                    else {}
                ),
            },
        )
        session.add(user_message)
        await session.flush()
        for row in current_attachments:
            row.session_id = session_id
            row.message_id = user_message.id

        history_message_ids = [row.id for row in history_rows]
        history_attachments = (
            (
                await session.scalars(
                    select(ChatAttachment).where(ChatAttachment.message_id.in_(history_message_ids))
                )
            ).all()
            if history_message_ids
            else []
        )
        # P0-4 (checkpoint 绑定): qa thread 用 "{session_id}:{turn_seq}" 作
        # local_id — 按 session 前缀可整批清理 (删会话联动删 checkpoint),
        # 按 turn 分隔避免 QaState 的 append reducer (evidences/trace) 跨问残留。
        turn_seq = (
            await session.scalar(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.session_id == session_id)
            )
        ) or 0
    attachments_by_message: dict[uuid.UUID, list[ChatAttachment]] = {}
    for attachment in history_attachments:
        if attachment.message_id is not None:
            attachments_by_message.setdefault(attachment.message_id, []).append(attachment)

    # Re-send at most the two most recent images so a follow-up such as “图中第二步”
    # still has visual context without repeatedly uploading the full image history.
    prior_images = sorted(
        (row for row in history_attachments if row.kind == "image"),
        key=lambda row: row.created_at,
        reverse=True,
    )[:2]
    image_ids = {row.id for row in prior_images}
    image_ids.update(row.id for row in current_attachments if row.kind == "image")

    history: list[GwMessage] = []
    for row in reversed(history_rows):
        if row.role not in ("user", "assistant"):
            continue
        content = row.content
        if row.role == "user":
            content = await _gateway_user_content(
                row.content,
                attachments_by_message.get(row.id, []),
                body.message,
                request.app.state.storage,
                image_ids,
            )
        history.append(GwMessage(role=row.role, content=content))
    current_user_content = await _gateway_user_content(
        body.message,
        current_attachments,
        body.message,
        request.app.state.storage,
        image_ids,
    )
    has_attachments = bool(current_attachments)
    has_images = any(row.kind == "image" for row in current_attachments) or bool(prior_images)
    if has_images and not settings.routing_vision_enabled:
        raise HTTPException(status_code=503, detail="VISION_MODEL_UNAVAILABLE")

    # 画像注入 (F8-A2 / P1-5 全路径): 绑定 kb 时按库取材; 未绑定传空列表走全量画像
    # (profile_summary 缓存 key 此时不带 kb 指纹)。空摘要 = 该用户尚无画像。
    # 读画像失败一律降级为"无画像"继续问答, 不让画像故障拖垮主链路。
    profile = ProfileContext()
    try:
        profile = await build_profile_context(
            ctx.tenant_id,
            ctx.user_id,
            [str(k) for k in kb_ids],
            redis=getattr(request.app.state, "redis", None),
            role="qa",
            org_ids=ctx.org_ids,
        )
    except Exception:  # noqa: BLE001
        log.exception("profile_summary_failed")

    # P0-5: 未成年判定 (birth_year, 未知按成年) — 决定审核 strict 档与 prompt 安全段
    minor = await _load_minor_flag(ctx.user_id)

    stream_ctx = _ChatStreamContext(
        request=request,
        ctx=ctx,
        body=body,
        gateway=gateway,
        settings=settings,
        runtime=runtime,
        request_id=request_id,
        chat_mode=chat_mode,
        kb_ids=kb_ids,
        owner_ids=owner_ids,
        kb_names=kb_names,
        kb_versions=kb_versions,
        session_id=session_id,
        turn_seq=turn_seq,
        history=history,
        current_user_content=current_user_content,
        has_attachments=has_attachments,
        has_images=has_images,
        profile=profile,
        minor=minor,
    )

    # 生产任务与客户端 SSE 订阅分离。EventSourceResponse 在浏览器切换页面时只会
    # 取消下面的 subscriber；producer 由模块级强引用持有，继续生成并把最终答案落 PG。
    run_id = request_id or uuid.uuid4().hex
    event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
    event_queue.put_nowait(
        {
            "event": "session",
            "data": _sse({"session_id": str(session_id), "run_id": run_id}),
        }
    )

    async def producer() -> None:
        try:
            async for event in _event_stream(stream_ctx):
                event_queue.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 后台异常须落日志并通知仍在线的订阅者
            log.exception("chat_background_run_failed", run_id=run_id)
            event_queue.put_nowait(
                {
                    "event": "error",
                    "data": _sse({"code": "GENERATION_FAILED", "message": "回答生成失败，请重试"}),
                }
            )
        finally:
            current = _active_runs.get(run_id)
            if current is not None and current.task is asyncio.current_task():
                _active_runs.pop(run_id, None)
            if _session_runs.get(session_id) == run_id:
                _session_runs.pop(session_id, None)
            event_queue.put_nowait(None)

    task = _track_task(asyncio.create_task(producer()))
    _active_runs[run_id] = _ActiveChatRun(task=task, user_id=ctx.user_id, session_id=session_id)
    _session_runs[session_id] = run_id

    async def subscriber():
        while True:
            event = await event_queue.get()
            if event is None:
                return
            yield event

    return EventSourceResponse(
        subscriber(),
        headers={
            # 代理层可能短暂缓冲首个很小的 SSE 帧；响应头让前端能立即把运行中
            # 的新会话放进左栏。SSE session 事件仍保留给非浏览器客户端。
            "X-Chat-Session-ID": str(session_id),
            "X-Chat-Run-ID": run_id,
        },
    )
