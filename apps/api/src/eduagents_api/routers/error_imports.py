"""学生错题导入：图片/文档识别草稿、人工校对、确认入错题本。"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import orjson
from eduagents_core.gateway import ChatMessage, ChatRequest, Tier
from eduagents_core.mastery.scheduler import new_card_json
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select

from .. import timectl
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..error_analysis import CAUSES
from ..error_import_cleanup import (
    cleanup_expired_error_imports,
    error_import_storage_prefix,
    pending_error_import_count,
)
from ..models import (
    ErrorImport,
    ErrorItem,
    KnowledgeBase,
    LearningReportSnapshot,
    LearningResource,
    MasteryLog,
    Question,
    ResourceQuestion,
)
from ..profile_summary import invalidate_profile_summary_cache
from ..services import _resolve_kc_ids
from ..uploads import read_chat_attachment
from .chat_attachments import _extract_document, _safe_filename, _validate_file

router = APIRouter(prefix="/api/errors", tags=["errors"])

_QUESTION_TYPES = ("single", "multi", "blank", "short")
_DIFFICULTIES = ("easy", "mid", "hard")
_SOURCE_MIMES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_INLINE_SOURCE_MIMES = {
    "application/pdf",
    "text/markdown",
    "text/plain",
    "image/png",
    "image/jpeg",
    "image/webp",
}

_EXTRACT_PROMPT = """你是试题录入助手。请从用户提供的内容中提取最主要的一道题，供用户校对。
只做忠实转录，不要补造看不清的条件、选项或答案。无法确认的字段用空字符串或空数组。

只输出一个 JSON 对象：
{
  "type": "single|multi|blank|short",
  "stem": "完整题干",
  "options": ["A. ...", "B. ..."],
  "my_answer": "图中能明确识别的学生答案，否则为空",
  "answer": "能从原文明确确认的参考答案，否则为空",
  "explanation": "原文已有解析才填写，否则为空",
  "kc_names": ["1 到 3 个简短知识点"],
  "difficulty": "easy|mid|hard"
}

选择题 answer 使用字母（多选用 A,C），不要把模型推测当成原文答案。
数学表达式必须使用 LaTeX：行内公式严格写成 $...$；块公式的开始和结束 $$ 各自独占一行，并在 JSON 字符串中把换行编码为 \\n。不要省略闭合定界符，不要把中文正文放进公式定界符，不要裸输出完整公式。"""


class ErrorDraftIn(BaseModel):
    type: Literal["single", "multi", "blank", "short"] = "short"
    stem: str = Field(min_length=1, max_length=12000)
    options: list[str] = Field(default_factory=list, max_length=8)
    my_answer: str = Field(default="", max_length=8000)
    answer: str = Field(default="", max_length=8000)
    explanation: str = Field(default="", max_length=12000)
    kc_names: list[str] = Field(default_factory=list, max_length=3)
    difficulty: Literal["easy", "mid", "hard"] = "mid"
    kb_id: uuid.UUID | None = None
    cause: str = Field(default="knowledge_gap", max_length=32)
    answer_verified: bool = False

    @model_validator(mode="after")
    def validate_question(self):
        self.stem = self.stem.strip()
        self.options = [str(item).strip()[:1000] for item in self.options if str(item).strip()]
        self.kc_names = list(
            dict.fromkeys(str(item).strip()[:255] for item in self.kc_names if str(item).strip())
        )[:3]
        if self.type in {"single", "multi"} and len(self.options) < 2:
            raise ValueError("选择题至少需要两个选项")
        if self.type not in {"single", "multi"}:
            self.options = []
        if self.answer_verified and not self.answer.strip():
            raise ValueError("确认答案可靠前，需要填写正确答案")
        if self.cause not in CAUSES:
            raise ValueError("无效的错因分类")
        return self


def _blank_draft() -> dict:
    return {
        "type": "short",
        "stem": "",
        "options": [],
        "my_answer": "",
        "answer": "",
        "explanation": "",
        "kc_names": [],
        "difficulty": "mid",
    }


def _parse_draft(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        value = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return _blank_draft()
    if not isinstance(value, dict) or value.get("stub") is True:
        return _blank_draft()
    qtype = str(value.get("type") or "short")
    difficulty = str(value.get("difficulty") or "mid")
    return {
        "type": qtype if qtype in _QUESTION_TYPES else "short",
        "stem": str(value.get("stem") or "").strip()[:12000],
        "options": [
            str(item).strip()[:1000]
            for item in (value.get("options") or [])
            if str(item).strip()
        ][:8],
        "my_answer": str(value.get("my_answer") or "").strip()[:8000],
        "answer": str(value.get("answer") or "").strip()[:8000],
        "explanation": str(value.get("explanation") or "").strip()[:12000],
        "kc_names": [
            str(item).strip()[:255]
            for item in (value.get("kc_names") or [])
            if str(item).strip()
        ][:3],
        "difficulty": difficulty if difficulty in _DIFFICULTIES else "mid",
    }


def _out(row: ErrorImport) -> dict:
    source_url = (
        f"/api/errors/imports/{row.id}/source" if row.storage_key and row.filename else None
    )
    return {
        "id": str(row.id),
        "source_type": row.source_type,
        "filename": row.filename,
        "mime_type": row.mime_type,
        "source_url": source_url,
        "status": row.status,
        "draft": row.draft_json or _blank_draft(),
        "recognition_warning": row.recognition_warning,
        "confirmed_error_item_id": (
            str(row.confirmed_error_item_id) if row.confirmed_error_item_id else None
        ),
        "created_at": row.created_at,
    }


async def _prepare_new_import(ctx: AuthContext, request: Request, settings) -> None:
    await cleanup_expired_error_imports(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        storage=request.app.state.storage,
        ttl_hours=settings.error_import_draft_ttl_hours,
    )
    limit = settings.max_error_import_drafts
    if limit > 0 and await pending_error_import_count(ctx.tenant_id, ctx.user_id) >= limit:
        raise HTTPException(status_code=429, detail="TOO_MANY_PENDING_ERROR_IMPORTS")


def _source_headers(filename: str, mime_type: str) -> dict[str, str]:
    safe_name = _safe_filename(filename)
    ascii_name = "".join(
        char if 32 <= ord(char) < 127 and char not in {'"', "\\"} else "_"
        for char in safe_name
    ).strip()
    ascii_name = ascii_name or "source"
    disposition = "inline" if mime_type in _INLINE_SOURCE_MIMES else "attachment"
    encoded_name = quote(safe_name, safe="")
    return {
        "Content-Disposition": (
            f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
        ),
        "X-Content-Type-Options": "nosniff",
    }


async def _source_response(row: ErrorImport, request: Request) -> Response:
    if not row.storage_key or not row.filename:
        raise HTTPException(status_code=404, detail="SOURCE_NOT_FOUND")
    try:
        data = await request.app.state.storage.load(row.storage_key)
    except Exception:  # noqa: BLE001 - 不泄露对象存储细节
        raise HTTPException(status_code=404, detail="SOURCE_NOT_FOUND") from None
    mime_type = _SOURCE_MIMES.get(Path(row.filename).suffix.lower(), "application/octet-stream")
    return Response(
        content=data,
        media_type=mime_type,
        headers={
            **_source_headers(row.filename, mime_type),
            "Content-Length": str(len(data)),
        },
    )


async def _recognize(request: Request, ctx: AuthContext, *, kind: str, mime: str, data: bytes,
                     extracted_text: str | None, settings) -> tuple[dict, str | None]:
    gateway = request.app.state.gateway
    gateway_settings = getattr(gateway, "_s", None)
    if getattr(gateway_settings, "gateway_mode", None) == "stub":
        return _blank_draft(), "当前为开发模式，请在校对表单中手动录入题目。"
    content: str | list[dict]
    if kind == "image":
        content = [
            {"type": "text", "text": _EXTRACT_PROMPT},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"},
            },
        ]
    else:
        content = f"{_EXTRACT_PROMPT}\n\n以下是文档解析文本：\n{(extracted_text or '')[:24000]}"
    try:
        result = await gateway.chat(
            ChatRequest(
                messages=(ChatMessage(role="user", content=content),),
                tier=Tier.standard,
                temperature=0.0,
                max_tokens=1800,
                json_mode=True,
                feature="error_import",
                tenant_id=str(ctx.tenant_id),
                user_id=str(ctx.user_id),
                request_id=getattr(request.state, "request_id", ""),
                model_override=(
                    settings.vision_model
                    if kind == "image" and not settings.model_routing_enabled
                    else None
                ),
                allow_tier_fallback=kind != "image",
                credential_policy="platform" if kind == "image" else "text",
            )
        )
        draft = _parse_draft(result.text)
        if not draft["stem"]:
            return draft, "没有可靠识别出题干，请根据原文件手动补充。"
        return draft, None
    except Exception:  # noqa: BLE001 - 自动识别失败仍允许人工录入，不丢上传文件
        return _blank_draft(), "自动识别暂不可用，请根据原文件手动录入。"


@router.get("/imports/capabilities")
async def import_capabilities(settings=Depends(get_app_settings)):
    return {
        "max_file_mb": settings.max_chat_attachment_mb,
        "document_suffixes": [".pdf", ".docx", ".pptx", ".md", ".txt"],
        "image_suffixes": [".png", ".jpg", ".jpeg", ".webp"]
        if settings.routing_vision_enabled
        else [],
        "vision_enabled": settings.routing_vision_enabled,
    }


@router.post("/imports/manual", status_code=201)
async def create_manual_import(
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    await _prepare_new_import(ctx, request, settings)
    async with tenant_session(ctx.tenant_id) as session:
        row = ErrorImport(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            source_type="manual",
            draft_json=_blank_draft(),
        )
        session.add(row)
        await session.flush()
        return _out(row)


@router.post("/imports", status_code=201)
async def upload_error_import(
    file: UploadFile,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    await _prepare_new_import(ctx, request, settings)
    filename = _safe_filename(file.filename)
    data = await read_chat_attachment(file, settings)
    kind, mime = _validate_file(
        filename, data, vision_enabled=settings.routing_vision_enabled
    )
    from .resources import _resource_quota_used

    async with tenant_session(ctx.tenant_id) as session:
        used = await _resource_quota_used(session, ctx)
        if (
            settings.max_storage_mb_per_user > 0
            and used + len(data) > settings.max_storage_mb_per_user * 1024 * 1024
        ):
            raise HTTPException(status_code=400, detail="STORAGE_QUOTA_EXCEEDED")
    extracted_text = (
        await _extract_document(filename, data, settings.max_chat_attachment_text_chars)
        if kind == "document"
        else None
    )
    draft, warning = await _recognize(
        request,
        ctx,
        kind=kind,
        mime=mime,
        data=data,
        extracted_text=extracted_text,
        settings=settings,
    )
    import_id = uuid.uuid4()
    storage_prefix = error_import_storage_prefix(ctx.tenant_id, import_id)
    storage_key = f"{storage_prefix}/{filename}"
    await request.app.state.storage.save(storage_key, data)
    try:
        async with tenant_session(ctx.tenant_id) as session:
            row = ErrorImport(
                id=import_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                source_type=kind,
                filename=filename,
                mime_type=mime,
                size_bytes=len(data),
                storage_key=storage_key,
                draft_json=draft,
                recognition_warning=warning,
            )
            session.add(row)
            await session.flush()
            return _out(row)
    except BaseException:
        await request.app.state.storage.delete_prefix(storage_prefix)
        raise


async def _load_import(
    session,
    import_id: uuid.UUID,
    ctx: AuthContext,
    *,
    allow_deleting: bool = False,
    for_update: bool = False,
) -> ErrorImport:
    query = select(ErrorImport).where(ErrorImport.id == import_id)
    if for_update:
        query = query.with_for_update()
    row = await session.scalar(query)
    if row is None or row.user_id != ctx.user_id:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if row.status == "deleting" and not allow_deleting:
        raise HTTPException(status_code=409, detail="IMPORT_DELETE_PENDING")
    return row


async def _validate_kb(session, kb_id: uuid.UUID | None) -> None:
    if kb_id is not None and await session.get(KnowledgeBase, kb_id) is None:
        raise HTTPException(status_code=404, detail="KB_NOT_FOUND")


@router.post("/imports/{import_id}/confirm")
async def confirm_error_import(
    import_id: uuid.UUID,
    body: ErrorDraftIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    verified = False
    now_dt = await timectl.now(request.app.state.redis)
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        imported = await _load_import(session, import_id, ctx, for_update=True)
        if imported.status == "confirmed" and imported.confirmed_error_item_id:
            existing = await session.get(ErrorItem, imported.confirmed_error_item_id)
            return {
                "error_item_id": str(imported.confirmed_error_item_id),
                "verification_status": (
                    existing.verification_status if existing is not None else "pending"
                ),
            }
        await _validate_kb(session, body.kb_id)
        kc_ids = await _resolve_kc_ids(
            session,
            str(body.kb_id) if body.kb_id else None,
            body.kc_names,
            user_id=ctx.user_id,
        )
        resource = None
        if imported.resource_id is not None:
            resource = await session.get(LearningResource, imported.resource_id)
        if resource is None:
            resource = LearningResource(
                id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                owner_type="personal",
                owner_id=ctx.user_id,
                resource_type="single_question",
                title=(imported.filename or body.stem[:120] or "错题导入")[:255],
                subject="general",
                source_filename=imported.filename,
                mime_type=imported.mime_type,
                size_bytes=imported.size_bytes,
                storage_key=imported.storage_key,
                metadata_json={"legacy_error_import_id": str(imported.id)},
            status="reviewing",
                retrieval_policy="questions_only",
                linked_kb_id=body.kb_id,
                created_by=ctx.user_id,
            )
            session.add(resource)
            await session.flush()
            imported.resource_id = resource.id
        else:
            resource.linked_kb_id = body.kb_id
            resource.status = "reviewing"
        question = Question(
            tenant_id=ctx.tenant_id,
            quiz_id=None,
            type=body.type,
            stem=body.stem,
            options_json=body.options,
            answer=body.answer.strip(),
            explanation=body.explanation.strip(),
            kc_ids=kc_ids,
            difficulty=body.difficulty,
            source_type=imported.source_type,
            source_meta_json={
                "import_id": str(imported.id),
                "filename": imported.filename,
                "kc_names": body.kc_names,
                "kb_id": str(body.kb_id) if body.kb_id else None,
                "detected_student_answer": body.my_answer.strip(),
                "detected_reference_answer": body.answer.strip(),
                "error_selected": True,
            },
        )
        session.add(question)
        await session.flush()
        resource_question = ResourceQuestion(
            tenant_id=ctx.tenant_id,
            resource_id=resource.id,
            question_id=question.id,
            ordinal=0,
            question_no="1",
            verification_status="verified" if body.answer_verified else "pending",
        )
        session.add(resource_question)
        await session.flush()
        item_id = uuid.uuid4()
        item = ErrorItem(
            id=item_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            question_id=question.id,
            kc_ids=kc_ids,
            cause=body.cause,
            state="new",
            verification_status="verified" if body.answer_verified else "pending",
            source_import_id=imported.id,
            resource_question_id=resource_question.id,
            original_response=body.my_answer.strip(),
            fsrs_card_json=(
                new_card_json(now_dt, business_card_id=item_id)
                if body.answer_verified
                else {}
            ),
        )
        session.add(item)
        await session.flush()
        imported.status = "confirmed"
        imported.draft_json = json.loads(body.model_dump_json())
        imported.confirmed_error_item_id = item.id
        verified = item.verification_status == "verified"
        resource.status = "ready" if verified else "reviewing"
        result = {
            "error_item_id": str(item.id),
            "verification_status": item.verification_status,
        }
        # 仅派生画像/报告需要失效；pending 导入不会改变它们。
        if verified:
            await session.execute(
                delete(LearningReportSnapshot).where(
                    LearningReportSnapshot.user_id == ctx.user_id
                )
            )
    if verified:
        await invalidate_profile_summary_cache(request.app.state.redis, ctx.user_id)
    return result


@router.patch("/{error_item_id}/content")
async def update_imported_error(
    error_item_id: uuid.UUID,
    body: ErrorDraftIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    verified = False
    now_dt = await timectl.now(request.app.state.redis)
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        item = await session.scalar(
            select(ErrorItem).where(ErrorItem.id == error_item_id).with_for_update()
        )
        if item is None or item.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        if item.verification_status == "verified" and not body.answer_verified:
            raise HTTPException(status_code=409, detail="VERIFICATION_ROLLBACK_FORBIDDEN")
        question = await session.get(Question, item.question_id)
        if question is None or question.source_type not in {"manual", "image", "document"}:
            raise HTTPException(status_code=409, detail="ERROR_ITEM_NOT_EDITABLE")
        await _validate_kb(session, body.kb_id)
        kc_ids = await _resolve_kc_ids(
            session,
            str(body.kb_id) if body.kb_id else None,
            body.kc_names,
            user_id=ctx.user_id,
        )
        has_evidence = bool((item.fsrs_card_json or {}).get("last_review"))
        if not has_evidence:
            has_evidence = (
                await session.scalar(
                    select(MasteryLog.id)
                    .where(
                        MasteryLog.user_id == ctx.user_id,
                        MasteryLog.evidence_id == item.id,
                    )
                    .limit(1)
                )
                is not None
            )
        semantic_changed = (
            question.type != body.type
            or list(question.options_json or []) != list(body.options)
            or question.answer.strip() != body.answer.strip()
            or [str(k) for k in (question.kc_ids or [])] != [str(k) for k in kc_ids]
        )
        if has_evidence and semantic_changed:
            raise HTTPException(status_code=409, detail="ERROR_ITEM_LOCKED_AFTER_REVIEW")
        question.type = body.type
        question.stem = body.stem
        question.options_json = body.options
        question.answer = body.answer.strip()
        question.explanation = body.explanation.strip()
        question.kc_ids = kc_ids
        question.difficulty = body.difficulty
        question.source_meta_json = {
            **(question.source_meta_json or {}),
            "kc_names": body.kc_names,
            "kb_id": str(body.kb_id) if body.kb_id else None,
        }
        if item.resource_question_id is not None:
            resource_question = await session.get(ResourceQuestion, item.resource_question_id)
            if resource_question is not None:
                resource = await session.get(LearningResource, resource_question.resource_id)
                if resource is not None:
                    resource.linked_kb_id = body.kb_id
                    resource.status = "ready" if item.verification_status == "verified" else "reviewing"
                if item.verification_status == "verified":
                    resource_question.verification_status = "verified"
        item.kc_ids = kc_ids
        item.cause = body.cause
        item.original_response = body.my_answer.strip()
        if item.verification_status == "pending" and body.answer_verified:
            item.verification_status = "verified"
            item.fsrs_card_json = new_card_json(now_dt, business_card_id=item.id)
        verified = item.verification_status == "verified"
        if item.source_import_id is not None:
            imported = await session.get(ErrorImport, item.source_import_id)
            if imported is not None and imported.user_id == ctx.user_id:
                imported.draft_json = json.loads(body.model_dump_json())
        if verified:
            await session.execute(
                delete(LearningReportSnapshot).where(
                    LearningReportSnapshot.user_id == ctx.user_id
                )
            )
        result = {
            "error_item_id": str(item.id),
            "verification_status": item.verification_status,
        }
    if verified:
        await invalidate_profile_summary_cache(request.app.state.redis, ctx.user_id)
    return result


@router.get("/imports/{import_id}/source")
async def read_error_import_source(
    import_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """读取本人错题导入的原始文件；文件字节不经前端可猜的对象 key 直取。"""
    async with tenant_session(ctx.tenant_id) as session:
        row = await _load_import(session, import_id, ctx)
        source = (row.storage_key, row.filename, row.mime_type)
    # 不在数据库事务/连接上进行对象存储读取。
    detached = ErrorImport(
        id=import_id,
        storage_key=source[0],
        filename=source[1],
        mime_type=source[2],
    )
    return await _source_response(detached, request)


@router.get("/{error_item_id}/source")
async def read_error_item_source(
    error_item_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """编辑已保存错题时的来源别名；仍同时校验租户和错题所有权。"""
    async with tenant_session(ctx.tenant_id) as session:
        item = await session.get(ErrorItem, error_item_id)
        if item is None or item.user_id != ctx.user_id or item.source_import_id is None:
            raise HTTPException(status_code=404, detail="SOURCE_NOT_FOUND")
        row = await _load_import(session, item.source_import_id, ctx)
        source = (row.id, row.storage_key, row.filename, row.mime_type)
    detached = ErrorImport(
        id=source[0],
        storage_key=source[1],
        filename=source[2],
        mime_type=source[3],
    )
    return await _source_response(detached, request)


@router.delete("/imports/{import_id}", status_code=204)
async def delete_error_import(
    import_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id) as session:
        row = await _load_import(
            session, import_id, ctx, allow_deleting=True, for_update=True
        )
        if row.status == "confirmed":
            raise HTTPException(status_code=409, detail="IMPORT_ALREADY_CONFIRMED")
        row.status = "deleting"
        storage_prefix = error_import_storage_prefix(ctx.tenant_id, row.id)

    try:
        if row.storage_key:
            await request.app.state.storage.delete_prefix(storage_prefix)
    except Exception:  # noqa: BLE001 - deleting 状态保留，后续可重试
        raise HTTPException(status_code=503, detail="ERROR_IMPORT_DELETE_FAILED") from None

    # 只有对象删除成功后才删除数据库行。若此事务失败，row 仍是 deleting，
    # 不会向客户端返回 204，也不会留下错误的“已删除”状态。
    async with tenant_session(ctx.tenant_id) as session:
        row = await session.get(ErrorImport, import_id)
        if row is not None and row.user_id == ctx.user_id and row.status == "deleting":
            await session.delete(row)
