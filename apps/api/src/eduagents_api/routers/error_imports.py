"""学生错题导入：图片/文档识别草稿、人工校对、确认入错题本。"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Literal

import orjson
from eduagents_core.gateway import ChatMessage, ChatRequest, Tier
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..error_analysis import CAUSES
from ..models import Attempt, ErrorImport, ErrorItem, KnowledgeBase, Question, Quiz
from ..services import _resolve_kc_ids
from .chat_attachments import _extract_document, _read_capped, _safe_filename, _validate_file

router = APIRouter(prefix="/api/errors", tags=["errors"])

_QUESTION_TYPES = ("single", "multi", "blank", "short")
_DIFFICULTIES = ("easy", "mid", "hard")

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

选择题 answer 使用字母（多选用 A,C），不要把模型推测当成原文答案。"""


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
    return {
        "id": str(row.id),
        "source_type": row.source_type,
        "filename": row.filename,
        "status": row.status,
        "draft": row.draft_json or _blank_draft(),
        "recognition_warning": row.recognition_warning,
        "confirmed_error_item_id": (
            str(row.confirmed_error_item_id) if row.confirmed_error_item_id else None
        ),
        "created_at": row.created_at,
    }


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
                model_override=settings.vision_model if kind == "image" else None,
                allow_tier_fallback=kind != "image",
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
        "image_suffixes": [".png", ".jpg", ".jpeg", ".webp"] if settings.vision_model else [],
        "vision_enabled": bool(settings.vision_model),
    }


@router.post("/imports/manual", status_code=201)
async def create_manual_import(ctx: AuthContext = Depends(api_rate_limited)):
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
    filename = _safe_filename(file.filename)
    data = await _read_capped(file, settings.max_chat_attachment_mb * 1024 * 1024)
    kind, mime = _validate_file(filename, data, vision_enabled=bool(settings.vision_model))
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
    storage_prefix = f"{ctx.tenant_id}/error-imports/{import_id}"
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


async def _load_import(session, import_id: uuid.UUID, ctx: AuthContext) -> ErrorImport:
    row = await session.get(ErrorImport, import_id)
    if row is None or row.user_id != ctx.user_id:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return row


async def _validate_kb(session, kb_id: uuid.UUID | None) -> None:
    if kb_id is not None and await session.get(KnowledgeBase, kb_id) is None:
        raise HTTPException(status_code=404, detail="KB_NOT_FOUND")


@router.post("/imports/{import_id}/confirm")
async def confirm_error_import(
    import_id: uuid.UUID,
    body: ErrorDraftIn,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        imported = await _load_import(session, import_id, ctx)
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
        quiz = Quiz(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            kb_id=body.kb_id,
            title=body.stem[:120],
            blueprint_json={"kind": "error_import", "source_type": imported.source_type},
            status="ready",
        )
        session.add(quiz)
        await session.flush()
        question = Question(
            tenant_id=ctx.tenant_id,
            quiz_id=quiz.id,
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
            },
        )
        session.add(question)
        await session.flush()
        session.add(
            Attempt(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                question_id=question.id,
                response=body.my_answer.strip(),
                is_correct=False if body.answer_verified else None,
                scored_reason="用户手动收录错题",
            )
        )
        item = ErrorItem(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            question_id=question.id,
            kc_ids=kc_ids,
            cause=body.cause,
            state="new",
            verification_status="verified" if body.answer_verified else "pending",
            source_import_id=imported.id,
        )
        session.add(item)
        await session.flush()
        imported.status = "confirmed"
        imported.draft_json = json.loads(body.model_dump_json())
        imported.confirmed_error_item_id = item.id
        return {
            "error_item_id": str(item.id),
            "verification_status": item.verification_status,
        }


@router.patch("/{error_item_id}/content")
async def update_imported_error(
    error_item_id: uuid.UUID,
    body: ErrorDraftIn,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        item = await session.get(ErrorItem, error_item_id)
        if item is None or item.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
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
        quiz = await session.get(Quiz, question.quiz_id)
        if quiz is not None:
            quiz.kb_id = body.kb_id
        item.kc_ids = kc_ids
        item.cause = body.cause
        item.verification_status = "verified" if body.answer_verified else "pending"
        attempt = await session.scalar(
            select(Attempt)
            .where(Attempt.user_id == ctx.user_id, Attempt.question_id == question.id)
            .order_by(Attempt.at.desc())
        )
        if attempt is not None:
            attempt.response = body.my_answer.strip()
            attempt.is_correct = False if body.answer_verified else None
        return {
            "error_item_id": str(item.id),
            "verification_status": item.verification_status,
        }


@router.delete("/imports/{import_id}", status_code=204)
async def delete_error_import(
    import_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id) as session:
        row = await _load_import(session, import_id, ctx)
        if row.status == "confirmed":
            raise HTTPException(status_code=409, detail="IMPORT_ALREADY_CONFIRMED")
        if row.storage_key:
            await request.app.state.storage.delete_prefix(
                f"{ctx.tenant_id}/error-imports/{row.id}"
            )
        await session.delete(row)
