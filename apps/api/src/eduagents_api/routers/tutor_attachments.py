from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..models import (
    LearningResource,
    PendingCleanup,
    TutorProblemDraft,
)
from ..resource_utils import (
    safe_filename,
    safe_source_headers,
    sha256_hex,
    validate_resource_file,
)
router = APIRouter(prefix="/api/tutor/drafts", tags=["tutor"])


class TutorDraftPatch(BaseModel):
    selected_question_index: int | None = Field(default=None, ge=0)
    stem: str | None = Field(default=None, max_length=12000)
    options: list[str] | None = Field(default=None, max_length=8)
    known_conditions: str | None = Field(default=None, max_length=8000)
    current_thought: str | None = Field(default=None, max_length=8000)


class TutorDraftManualIn(BaseModel):
    stem: str = Field(min_length=1, max_length=12000)
    options: list[str] = Field(default_factory=list, max_length=8)
    known_conditions: str = Field(default="", max_length=8000)
    current_thought: str = Field(default="", max_length=8000)


class TutorRecognizeIn(BaseModel):
    pages: list[int] = Field(default_factory=list, max_length=20)


def _normalized_options(values: list[str] | None) -> list[str]:
    return [str(value).strip()[:1000] for value in (values or []) if str(value).strip()][:8]


async def _draft_out(session, draft: TutorProblemDraft) -> dict:
    await session.refresh(draft)
    resource = await session.get(LearningResource, draft.resource_id) if draft.resource_id else None
    questions = draft.recognized_questions_json or []
    return {
        "id": str(draft.id),
        "status": draft.status,
        "selected_pages": draft.selected_pages_json or [],
        "selected_question_index": draft.selected_question_index,
        "stem": draft.draft_stem,
        "options": draft.draft_options_json or [],
        "known_conditions": draft.known_conditions,
        "current_thought": draft.current_thought,
        "confirmed_problem_text": draft.confirmed_problem_text if draft.status == "confirmed" else "",
        "confirmed_page_no": draft.confirmed_page_no,
        "thread_local_id": draft.thread_local_id,
        "warnings": draft.warnings_json or [],
        "error_code": draft.error_code,
        "recognition_attempts": draft.recognition_attempts,
        "questions": questions,
        "attachment": (
            {
                "resource_id": str(resource.id),
                "filename": resource.source_filename,
                "mime_type": resource.mime_type,
                "size_bytes": resource.size_bytes,
                "page_count": resource.page_count,
                "preview_url": f"/api/tutor/drafts/{draft.id}/source",
            }
            if resource is not None
            else None
        ),
        "vision_uses_platform_quota": bool(
            resource
            and Path(resource.source_filename or "").suffix.lower()
            in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
        ),
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }


async def _owned_draft(session, draft_id: uuid.UUID, ctx: AuthContext, *, lock: bool = False):
    query = select(TutorProblemDraft).where(
        TutorProblemDraft.id == draft_id,
        TutorProblemDraft.tenant_id == ctx.tenant_id,
        TutorProblemDraft.user_id == ctx.user_id,
    )
    if lock:
        query = query.with_for_update()
    draft = await session.scalar(query)
    if draft is None:
        raise HTTPException(status_code=404, detail="TUTOR_DRAFT_NOT_FOUND")
    return draft


def _pdf_page_count(data: bytes, limit: int) -> int:
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise HTTPException(status_code=422, detail="TUTOR_PDF_PASSWORD_PROTECTED")
            count = document.page_count
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=422, detail="TUTOR_ATTACHMENT_PARSE_FAILED") from None
    if count <= 0:
        raise HTTPException(status_code=422, detail="TUTOR_ATTACHMENT_EMPTY")
    if limit > 0 and count > limit:
        raise HTTPException(status_code=413, detail="TUTOR_PDF_PAGE_LIMIT_EXCEEDED")
    return count


def _validate_pages(pages: list[int], page_count: int | None, maximum: int) -> list[int]:
    normalized = sorted(set(pages))
    if any(page < 1 or (page_count and page > page_count) for page in normalized):
        raise HTTPException(status_code=422, detail="TUTOR_INVALID_PAGE_SELECTION")
    if maximum > 0 and len(normalized) > maximum:
        raise HTTPException(status_code=413, detail="TUTOR_OCR_PAGE_LIMIT_EXCEEDED")
    return normalized


async def _enqueue_recognition(request: Request, tenant_id: uuid.UUID, draft_id: uuid.UUID, attempt: int) -> None:
    try:
        await request.app.state.arq.enqueue_job(
            "recognize_tutor_problem",
            str(tenant_id),
            str(draft_id),
            _job_id=f"tutor-recognize:{draft_id}:{attempt}",
        )
    except Exception:
        async with tenant_session(tenant_id) as session:
            draft = await session.get(TutorProblemDraft, draft_id)
            if draft is not None and draft.status == "recognizing":
                draft.status = "failed"
                draft.error_code = "TUTOR_RECOGNITION_QUEUE_FAILED"
        raise HTTPException(status_code=503, detail="TUTOR_RECOGNITION_QUEUE_FAILED") from None


@router.get("/capabilities")
async def tutor_attachment_capabilities(settings=Depends(get_app_settings)):
    return {
        "max_file_mb": settings.max_chat_attachment_mb,
        "suffixes": [".png", ".jpg", ".jpeg", ".webp", ".pdf", ".docx", ".pptx"],
        "max_pdf_pages": settings.max_tutor_attachment_pages,
        "default_pdf_pages": settings.max_tutor_default_pages,
        "max_recognition_pages": settings.max_tutor_recognition_pages,
        "vision_enabled": bool(settings.vision_model),
        "vision_uses_platform_quota": True,
    }


@router.post("/manual", status_code=201)
async def create_manual_draft(body: TutorDraftManualIn, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        draft = TutorProblemDraft(
            id=uuid.uuid4(),
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            status="review",
            draft_stem=body.stem.strip(),
            draft_options_json=_normalized_options(body.options),
            known_conditions=body.known_conditions.strip(),
            current_thought=body.current_thought.strip(),
        )
        session.add(draft)
        await session.flush()
        return await _draft_out(session, draft)


@router.post("", status_code=201)
async def upload_tutor_attachment(
    file: UploadFile,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    filename = safe_filename(file.filename)
    data = bytearray()
    maximum = settings.max_chat_attachment_mb * 1024 * 1024
    while True:
        part = await file.read(1024 * 1024)
        if not part:
            break
        data.extend(part)
        if maximum > 0 and len(data) > maximum:
            raise HTTPException(status_code=413, detail="TUTOR_ATTACHMENT_TOO_LARGE")
    raw = bytes(data)
    kind, mime_type = validate_resource_file(filename, raw)
    suffix = Path(filename).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".docx", ".pptx"}:
        raise HTTPException(status_code=400, detail="UNSUPPORTED_TUTOR_ATTACHMENT_TYPE")
    page_count = _pdf_page_count(raw, settings.max_tutor_attachment_pages) if suffix == ".pdf" else (1 if kind == "image" else None)
    selected_pages = (
        list(range(1, min(page_count or 1, settings.max_tutor_default_pages) + 1))
        if suffix == ".pdf"
        else ([1] if kind == "image" else [])
    )
    resource_id = uuid.uuid4()
    draft_id = uuid.uuid4()
    storage_prefix = f"{ctx.tenant_id}/learning-resources/{resource_id}"
    storage_key = f"{storage_prefix}/{filename}"
    await request.app.state.storage.save(storage_key, raw)
    try:
        async with tenant_session(ctx.tenant_id) as session:
            resource = LearningResource(
                id=resource_id,
                tenant_id=ctx.tenant_id,
                owner_type="personal",
                owner_id=ctx.user_id,
                resource_type="single_question",
                title=filename,
                subject="general",
                source_filename=filename,
                mime_type=mime_type,
                size_bytes=len(raw),
                storage_key=storage_key,
                sha256=sha256_hex(raw),
                page_count=page_count,
                metadata_json={
                    "source_kind": kind,
                    "tutor_draft_id": str(draft_id),
                    "selected_pages": selected_pages,
                },
                status="uploaded" if suffix == ".pdf" else "extracting",
                retrieval_policy="questions_only",
                created_by=ctx.user_id,
            )
            draft = TutorProblemDraft(
                id=draft_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                resource_id=resource_id,
                status="uploaded" if suffix == ".pdf" else "recognizing",
                selected_pages_json=selected_pages,
                recognition_attempts=0 if suffix == ".pdf" else 1,
            )
            session.add_all([resource, draft])
            await session.flush()
            output = await _draft_out(session, draft)
    except BaseException:
        await request.app.state.storage.delete_prefix(storage_prefix)
        raise
    if suffix != ".pdf":
        await _enqueue_recognition(request, ctx.tenant_id, draft_id, 1)
    return output


@router.get("/{draft_id}")
async def get_tutor_draft(draft_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        return await _draft_out(session, await _owned_draft(session, draft_id, ctx))


@router.patch("/{draft_id}")
async def update_tutor_draft(
    draft_id: uuid.UUID,
    body: TutorDraftPatch,
    ctx: AuthContext = Depends(api_rate_limited),
):
    values = body.model_dump(exclude_unset=True)
    async with tenant_session(ctx.tenant_id) as session:
        draft = await _owned_draft(session, draft_id, ctx, lock=True)
        if draft.status == "confirmed":
            raise HTTPException(status_code=409, detail="TUTOR_DRAFT_LOCKED")
        selected = values.get("selected_question_index")
        if selected is not None:
            questions = draft.recognized_questions_json or []
            if selected >= len(questions):
                raise HTTPException(status_code=404, detail="TUTOR_QUESTION_NOT_FOUND")
            question = questions[selected]
            draft.selected_question_index = selected
            draft.draft_stem = str(question.get("stem") or "")
            draft.draft_options_json = question.get("options") or []
        if "stem" in values:
            draft.draft_stem = (values["stem"] or "").strip()
        if "options" in values:
            draft.draft_options_json = _normalized_options(values["options"])
        if "known_conditions" in values:
            draft.known_conditions = (values["known_conditions"] or "").strip()
        if "current_thought" in values:
            draft.current_thought = (values["current_thought"] or "").strip()
        if draft.status in {"uploaded", "failed"} and draft.draft_stem:
            draft.status = "review"
            draft.error_code = None
        await session.flush()
        return await _draft_out(session, draft)


@router.post("/{draft_id}/recognize", status_code=202)
async def retry_tutor_recognition(
    draft_id: uuid.UUID,
    body: TutorRecognizeIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    async with tenant_session(ctx.tenant_id) as session:
        draft = await _owned_draft(session, draft_id, ctx, lock=True)
        if draft.status == "confirmed":
            raise HTTPException(status_code=409, detail="TUTOR_DRAFT_LOCKED")
        if draft.status == "recognizing":
            return await _draft_out(session, draft)
        if not draft.resource_id:
            raise HTTPException(status_code=422, detail="TUTOR_ATTACHMENT_REQUIRED")
        resource = await session.get(LearningResource, draft.resource_id)
        if resource is None:
            raise HTTPException(status_code=404, detail="TUTOR_ATTACHMENT_NOT_FOUND")
        pages = _validate_pages(
            body.pages or draft.selected_pages_json or [],
            resource.page_count,
            settings.max_tutor_recognition_pages,
        )
        if Path(resource.source_filename or "").suffix.lower() == ".pdf" and not pages:
            raise HTTPException(status_code=422, detail="TUTOR_PAGE_SELECTION_REQUIRED")
        if draft.recognition_attempts >= settings.max_tutor_recognition_attempts:
            raise HTTPException(status_code=409, detail="TUTOR_RECOGNITION_LIMIT_REACHED")
        if draft.status == "review":
            draft.selected_question_index = None
            draft.recognized_questions_json = []
        draft.selected_pages_json = pages
        draft.status = "recognizing"
        draft.error_code = None
        draft.recognition_attempts += 1
        resource.status = "extracting"
        resource.metadata_json = {**(resource.metadata_json or {}), "selected_pages": pages}
        attempt = draft.recognition_attempts
        await session.flush()
        output = await _draft_out(session, draft)
    await _enqueue_recognition(request, ctx.tenant_id, draft_id, attempt)
    return output


@router.post("/{draft_id}/confirm")
async def confirm_tutor_draft(
    draft_id: uuid.UUID,
    body: TutorDraftPatch,
    ctx: AuthContext = Depends(api_rate_limited),
):
    values = body.model_dump(exclude_unset=True)
    async with tenant_session(ctx.tenant_id) as session:
        draft = await _owned_draft(session, draft_id, ctx, lock=True)
        if draft.status == "confirmed":
            return await _draft_out(session, draft)
        if draft.status == "recognizing":
            raise HTTPException(status_code=409, detail="TUTOR_DRAFT_NOT_READY")
        if "stem" in values:
            draft.draft_stem = (values["stem"] or "").strip()
        if "options" in values:
            draft.draft_options_json = _normalized_options(values["options"])
        if "known_conditions" in values:
            draft.known_conditions = (values["known_conditions"] or "").strip()
        if "current_thought" in values:
            draft.current_thought = (values["current_thought"] or "").strip()
        if draft.status in {"uploaded", "failed"} and draft.draft_stem:
            draft.status = "review"
            draft.error_code = None
        if draft.status != "review":
            raise HTTPException(status_code=409, detail="TUTOR_DRAFT_NOT_READY")
        if not draft.draft_stem:
            raise HTTPException(status_code=422, detail="TUTOR_STEM_REQUIRED")
        selected_page = None
        if draft.selected_question_index is not None:
            questions = draft.recognized_questions_json or []
            if draft.selected_question_index < len(questions):
                selected_page = questions[draft.selected_question_index].get("page_no")
        parts = [draft.draft_stem]
        if draft.draft_options_json:
            parts.append(
                "\n".join(
                    f"{chr(65 + index)}. {option}"
                    for index, option in enumerate(draft.draft_options_json)
                )
            )
        if draft.known_conditions:
            parts.append(f"已知条件：{draft.known_conditions}")
        if draft.current_thought:
            parts.append(f"当前思路或卡点：{draft.current_thought}")
        draft.confirmed_stem = draft.draft_stem
        draft.confirmed_options_json = list(draft.draft_options_json or [])
        draft.confirmed_problem_text = "\n\n".join(parts)
        draft.confirmed_page_no = selected_page
        draft.thread_local_id = str(uuid.uuid4())
        draft.status = "confirmed"
        draft.confirmed_at = datetime.now(UTC)
        await session.flush()
        return await _draft_out(session, draft)


@router.get("/{draft_id}/source")
async def preview_tutor_source(
    draft_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id) as session:
        draft = await _owned_draft(session, draft_id, ctx)
        resource = await session.get(LearningResource, draft.resource_id) if draft.resource_id else None
        if resource is None or not resource.storage_key or not resource.source_filename:
            raise HTTPException(status_code=404, detail="TUTOR_ATTACHMENT_NOT_FOUND")
        storage_key = resource.storage_key
        filename = resource.source_filename
        mime_type = resource.mime_type or "application/octet-stream"
    try:
        data = await request.app.state.storage.load(storage_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="TUTOR_ATTACHMENT_NOT_FOUND") from None
    return Response(content=data, media_type=mime_type, headers=safe_source_headers(filename, mime_type))


@router.delete("/{draft_id}", status_code=204)
async def delete_tutor_draft(draft_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        draft = await _owned_draft(session, draft_id, ctx, lock=True)
        if draft.status == "confirmed":
            raise HTTPException(status_code=409, detail="TUTOR_DRAFT_LOCKED")
        resource = await session.get(LearningResource, draft.resource_id) if draft.resource_id else None
        if resource is not None:
            session.add(
                PendingCleanup(
                    id=uuid.uuid4(),
                    kind="object_prefix",
                    payload={"prefix": f"{ctx.tenant_id}/learning-resources/{resource.id}"},
                )
            )
            await session.delete(resource)
        else:
            await session.delete(draft)
        await session.flush()
