"""账号生命周期 — /api/account (P0-4 注销 + P1-8 数据导出)。

注销 (个保法第 47 条删除权, 物理可执行):
- 重输密码验证 -> 单事务: 删个人知识库行 (FK 级联 documents/chunks/KC/kc_edges)
  + 删 users 行 (0003 外键级联清 attempts/chat_sessions+messages/error_items/
  mastery_log/mastery_records/plans+adjustments/quizzes+questions/org_members/
  refresh_tokens; 0005 外键级联清 learner_insights/user_preferences;
  usage_log 置 NULL 留审计) + 写 pending_cleanup(kind=tenant)。
- Qdrant 两 collection / 对象存储前缀 / Redis 前缀 / checkpoint 三表由 worker
  outbox 消费 (purge_tenant), 与删除事务同提交, 不存在"删了一半"的窗口。
- 之后拉黑当前 jti + 写 deleted 状态标记: 其余在途 access token 被 deps 的
  账号状态检查立即拒绝; refresh 已随 users 行级联删除 (等价全部撤销)。

导出 (个保法第 45 条可携带权): POST 入队 export_user_data -> 业务表 JSON
(含 learner_insights/user_preferences/chat_attachments/error_imports) + chunk 文本 + 上传文件清单打 ZIP
落对象存储 exports/{uid}/{ts}.zip (worker/export.py);
GET 查状态; GET /download 经 API 流式转发 (LocalStorage 无预签名 URL 概念)。

接线 (主线): main.py app.include_router(account.router);
worker/main.py functions 注册 export_user_data。
"""

from __future__ import annotations

from collections.abc import Iterator

from eduagents_core.config import Settings
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, exists, select

from ..db import plain_session, tenant_session
from ..deps import (
    AuthContext,
    account_status_key,
    api_rate_limited,
    get_app_settings,
    invalidate_org_cache,
)
from ..models import (
    AssessmentAttempt,
    Attempt,
    ChatSession,
    ErrorItem,
    KnowledgeBase,
    LearningResource,
    PendingCleanup,
    Question,
    Quiz,
    ResourceQuestion,
    User,
)
from ..schemas import AccountDeleteIn
from ..security import verify_password

router = APIRouter(prefix="/api/account", tags=["account"])
log = get_logger("account")

_EXPORT_CHUNK = 256 * 1024
_EXPORT_PENDING_TTL = 600  # 导出任务排队标记 (幂等窗口)


def _export_key(user_id) -> str:
    return f"export:{user_id}"


def _export_pending_key(user_id) -> str:
    return f"export:pending:{user_id}"


@router.delete("", status_code=204)
async def delete_account(
    body: AccountDeleteIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    """注销账号: 密码确认 -> 单事务删光 PG 业务行 + 写清理 outbox -> 吊销凭证。"""
    async with plain_session() as session:
        user = await session.get(User, ctx.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="INVALID_TOKEN")
        if not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=403, detail="PASSWORD_MISMATCH")

    async with tenant_session(ctx.user_id) as session:
        # reflect:{sid} 计数键在 Redis, 会话行马上级联删除 -> 先收集进 payload
        session_ids = [str(s) for s in (await session.scalars(select(ChatSession.id))).all()]
        # 个人知识库无 users 外键 (tenant_id 语义列), 显式删 -> 级联 documents/
        # chunks/knowledge_components/kc_edges (RLS 写策略限定只删自己的行)
        await session.execute(
            delete(KnowledgeBase).where(KnowledgeBase.owner_type == "user")
        )
        # 记录本账号产生的脱离 Quiz 的结构化题目；删除资源/RQ 后只回收
        # 仍无任何历史引用的题目，不能误删普通 Quiz 题或被其他资源共享的题。
        resource_question_question_ids = {
            question_id
            for (question_id,) in (
                await session.execute(
                    select(ResourceQuestion.question_id)
                    .join(LearningResource, LearningResource.id == ResourceQuestion.resource_id)
                    .where(LearningResource.owner_id == ctx.user_id)
                )
            ).all()
        }
        personal_error_question_ids = {
            question_id
            for (question_id,) in (
                await session.execute(
                    select(ErrorItem.question_id).where(ErrorItem.user_id == ctx.user_id)
                )
            ).all()
        }
        personal_attempt_question_ids = {
            question_id
            for (question_id,) in (
                await session.execute(
                    select(Attempt.question_id).where(Attempt.user_id == ctx.user_id)
                )
            ).all()
        }
        question_ids = (
            resource_question_question_ids
            | personal_error_question_ids
            | personal_attempt_question_ids
            | {
                question_id
                for (question_id,) in (
                    await session.execute(
                        select(Question.id).where(
                            Question.tenant_id == ctx.user_id,
                            Question.quiz_id.is_(None),
                        )
                    )
                ).all()
            }
        )

        # 新增的资源/真实测评表显式清理；对象存储前缀则由同一
        # pending_cleanup outbox 事务后置回收。
        await session.execute(
            delete(AssessmentAttempt).where(AssessmentAttempt.student_id == ctx.user_id)
        )
        await session.execute(delete(ErrorItem).where(ErrorItem.user_id == ctx.user_id))
        await session.execute(delete(Attempt).where(Attempt.user_id == ctx.user_id))
        await session.execute(
            delete(LearningResource).where(LearningResource.owner_id == ctx.user_id)
        )
        await session.flush()

        # PostgreSQL's historical quiz FK keeps a removed quiz's questions by
        # setting quiz_id to NULL. Remove questions belonging to this user's
        # quizzes first so account deletion cannot leave tenant-owned orphans.
        await session.execute(
            delete(Question).where(
                Question.quiz_id.in_(select(Quiz.id).where(Quiz.user_id == ctx.user_id))
            )
        )

        # A document/resource question may have no Attempt, ErrorItem, or
        # ResourceQuestion left by this point. Reclaim only same-tenant,
        # quiz-free questions with no remaining historical reference; regular
        # quiz questions and questions shared by another resource stay intact.
        await session.execute(
            delete(Question).where(
                Question.tenant_id == ctx.user_id,
                Question.quiz_id.is_(None),
                ~exists(
                    select(ResourceQuestion.id).where(
                        ResourceQuestion.question_id == Question.id
                    )
                ),
                ~exists(
                    select(ErrorItem.id).where(ErrorItem.question_id == Question.id)
                ),
                ~exists(select(Attempt.id).where(Attempt.question_id == Question.id)),
            )
        )
        for question_id in question_ids:
            question = await session.get(Question, question_id)
            if question is None or question.quiz_id is not None:
                continue
            if await session.scalar(
                select(ResourceQuestion.id)
                .where(ResourceQuestion.question_id == question_id)
                .limit(1)
            ):
                continue
            if await session.scalar(
                select(ErrorItem.id).where(ErrorItem.question_id == question_id).limit(1)
            ):
                continue
            if await session.scalar(
                select(Attempt.id).where(Attempt.question_id == question_id).limit(1)
            ):
                continue
            await session.delete(question)

        # users 行: 0003 外键级联清 8 张学情表 + refresh_tokens, 0005 级联清
        # learner_insights/user_preferences (users 无 RLS, 显式限定 id);
        # usage_log.user_id 置 NULL 留审计
        await session.execute(delete(User).where(User.id == ctx.user_id))
        session.add(
            PendingCleanup(
                kind="tenant",
                payload={"tenant_id": str(ctx.user_id), "session_ids": session_ids},
            )
        )

    redis = request.app.state.redis
    # 当前 jti 拉黑 + deleted 标记: 其余在途 access token 由 deps 状态检查拒绝
    await redis.setex(f"bl:{ctx.jti}", settings.access_token_ttl_seconds, "1")
    await redis.setex(
        account_status_key(ctx.user_id), settings.access_token_ttl_seconds, "deleted"
    )
    await invalidate_org_cache(redis, ctx.user_id)
    log.info("account_deleted", user_id=str(ctx.user_id), sessions=len(session_ids))
    response = Response(status_code=204)
    response.delete_cookie("edu_refresh", path="/api/auth")
    return response


# ---- 数据导出 (P1-8) ----


@router.post("/export", status_code=202)
async def request_export(request: Request, ctx: AuthContext = Depends(api_rate_limited)):
    redis = request.app.state.redis
    if await redis.exists(_export_pending_key(ctx.user_id)):
        return {"status": "pending"}  # 幂等: 已有任务在排队/执行
    await redis.setex(_export_pending_key(ctx.user_id), _EXPORT_PENDING_TTL, "1")
    await request.app.state.arq.enqueue_job("export_user_data", str(ctx.user_id))
    log.info("export_requested", user_id=str(ctx.user_id))
    return {"status": "queued"}


@router.get("/export")
async def export_status(request: Request, ctx: AuthContext = Depends(api_rate_limited)):
    redis = request.app.state.redis
    if await redis.get(_export_key(ctx.user_id)):
        return {"status": "ready", "download": "/api/account/export/download"}
    if await redis.exists(_export_pending_key(ctx.user_id)):
        return {"status": "pending"}
    return {"status": "none"}


@router.get("/export/download")
async def download_export(request: Request, ctx: AuthContext = Depends(api_rate_limited)):
    redis = request.app.state.redis
    path = await redis.get(_export_key(ctx.user_id))
    if not path:
        raise HTTPException(status_code=404, detail="EXPORT_NOT_READY")
    path = path if isinstance(path, str) else path.decode()
    if not path.startswith(f"exports/{ctx.user_id}/"):
        # 防御: 键值被污染也绝不转发他人对象
        raise HTTPException(status_code=404, detail="EXPORT_NOT_READY")
    try:
        data = await request.app.state.storage.load(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="EXPORT_NOT_READY") from None
    filename = path.rsplit("/", 1)[-1]

    def _chunks() -> Iterator[bytes]:
        for i in range(0, len(data), _EXPORT_CHUNK):
            yield data[i : i + _EXPORT_CHUNK]

    return StreamingResponse(
        _chunks(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(data)),
        },
    )
