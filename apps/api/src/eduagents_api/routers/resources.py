"""Structured learning resources: upload, extraction review and source access."""

from __future__ import annotations

import uuid
from pathlib import Path

from eduagents_core.mastery.scheduler import new_card_json
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select

from .. import timectl
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..models import (
    AssessmentAnswer,
    AssessmentAttempt,
    Attempt,
    ChatAttachment,
    Document,
    ErrorImport,
    ErrorItem,
    KnowledgeBase,
    LearningResource,
    MasteryLog,
    Question,
    ResourceQuestion,
    TutorProblemDraft,
)
from ..resource_utils import (
    DIFFICULTIES,
    QUESTION_TYPES,
    RESOURCE_TYPES,
    resource_storage_key,
    resource_storage_prefix,
    safe_filename,
    safe_source_headers,
    sha256_hex,
    validate_resource_file,
)
from ..services import _resolve_kc_ids

router = APIRouter(prefix="/api/resources", tags=["resources"])
questions_router = APIRouter(prefix="/api/resource-questions", tags=["resource-questions"])


class ResourcePatchIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    subject: str | None = Field(default=None, max_length=64)
    grade: str | None = Field(default=None, max_length=32)
    term: str | None = Field(default=None, max_length=32)
    resource_type: str | None = None
    linked_kb_id: uuid.UUID | None = None
    retrieval_policy: str | None = None


class ResourceQuestionIn(BaseModel):
    question_no: str | None = Field(default=None, max_length=64)
    section_path: str | None = Field(default=None, max_length=255)
    page_no: int | None = Field(default=None, ge=1)
    source_region: dict | None = None
    points: float | None = Field(default=None, ge=0)
    extraction_confidence: float | None = Field(default=None, ge=0, le=1)
    type: str | None = None
    stem: str | None = Field(default=None, max_length=12000)
    options: list[str] | None = Field(default=None, max_length=8)
    detected_student_answer: str | None = Field(default=None, max_length=8000)
    detected_reference_answer: str | None = Field(default=None, max_length=8000)
    answer: str | None = Field(default=None, max_length=8000)
    explanation: str | None = Field(default=None, max_length=12000)
    kc_names: list[str] | None = Field(default=None, max_length=3)
    difficulty: str | None = None
    verification_status: str | None = None

    def normalized(self) -> dict:
        values = self.model_dump(exclude_unset=True)
        if "type" in values and values["type"] not in QUESTION_TYPES:
            raise HTTPException(status_code=422, detail="INVALID_QUESTION_TYPE")
        if "difficulty" in values and values["difficulty"] not in DIFFICULTIES:
            raise HTTPException(status_code=422, detail="INVALID_DIFFICULTY")
        status = values.get("verification_status")
        if status is not None and status not in {"pending", "verified", "rejected"}:
            raise HTTPException(status_code=422, detail="INVALID_VERIFICATION_STATUS")
        if "options" in values:
            values["options"] = [str(x).strip()[:1000] for x in values["options"] if str(x).strip()]
        if values.get("type") not in {None, "single", "multi"}:
            values["options"] = []
        if "kc_names" in values:
            values["kc_names"] = list(
                dict.fromkeys(str(x).strip()[:255] for x in values["kc_names"] if str(x).strip())
            )[:3]
        return values


class ConfirmResourceIn(BaseModel):
    resource_question_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    question_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    error_question_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    add_to_error_book: bool = True


def _resource_dict(row: LearningResource, *, question_count: int = 0, pending_count: int = 0) -> dict:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "owner_type": row.owner_type,
        "owner_id": str(row.owner_id),
        "resource_type": row.resource_type,
        "title": row.title,
        "subject": row.subject,
        "grade": row.grade,
        "term": row.term,
        "source_filename": row.source_filename,
        "filename": row.source_filename,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
        "storage_key": row.storage_key,
        "source_url": f"/api/resources/{row.id}/source" if row.storage_key else None,
        "sha256": row.sha256,
        "page_count": row.page_count,
        "metadata": row.metadata_json or {},
        "status": row.status,
        "retrieval_policy": row.retrieval_policy,
        "linked_kb_id": str(row.linked_kb_id) if row.linked_kb_id else None,
        "created_by": str(row.created_by),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "question_count": question_count,
        "pending_question_count": pending_count,
    }


def _question_dict(
    question: Question,
    row: ResourceQuestion,
    *,
    reveal_answer: bool = True,
    content_editable: bool = True,
) -> dict:
    meta = question.source_meta_json or {}
    return {
        "id": str(row.id),
        "resource_question_id": str(row.id),
        "question_id": str(question.id),
        "ordinal": row.ordinal,
        "question_no": row.question_no,
        "section_path": row.section_path,
        "page_no": row.page_no,
        "source_region": row.source_region_json or {},
        "points": row.points,
        "extraction_confidence": row.extraction_confidence,
        "verification_status": row.verification_status,
        "type": question.type,
        "stem": question.stem,
        "options": question.options_json or [],
        **({"answer": question.answer} if reveal_answer else {}),
        "detected_student_answer": meta.get("detected_student_answer", ""),
        **(
            {
                "detected_reference_answer": meta.get("detected_reference_answer", question.answer),
                "explanation": question.explanation,
            }
            if reveal_answer
            else {}
        ),
        "kc_ids": [str(x) for x in (question.kc_ids or [])],
        "kc_names": meta.get("kc_names", []),
        "difficulty": question.difficulty,
        "is_error_selected": meta.get("error_selected", False),
        "content_editable": content_editable,
    }


async def _owned_resource(session, resource_id: uuid.UUID, ctx: AuthContext, *, lock: bool = False):
    query = select(LearningResource).where(
        LearningResource.id == resource_id,
        LearningResource.tenant_id == ctx.tenant_id,
        LearningResource.owner_id == ctx.user_id,
    )
    if lock:
        query = query.with_for_update()
    row = await session.scalar(query)
    if row is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return row


async def _owned_resource_question(session, resource_question_id: uuid.UUID, ctx: AuthContext, *, lock: bool = False):
    query = (
        select(ResourceQuestion, LearningResource, Question)
        .join(LearningResource, LearningResource.id == ResourceQuestion.resource_id)
        .join(Question, Question.id == ResourceQuestion.question_id)
        .where(
            ResourceQuestion.id == resource_question_id,
            ResourceQuestion.tenant_id == ctx.tenant_id,
            LearningResource.owner_id == ctx.user_id,
        )
    )
    if lock:
        query = query.with_for_update()
    row = (await session.execute(query)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return row


async def _delete_orphan_question(session, question_id: uuid.UUID) -> None:
    """Delete a resource-owned question only after every historical reference is gone."""
    question = await session.get(Question, question_id)
    if question is None or question.quiz_id is not None:
        return
    if await session.scalar(
        select(ResourceQuestion.id).where(ResourceQuestion.question_id == question_id).limit(1)
    ):
        return
    if await session.scalar(
        select(ErrorItem.id).where(ErrorItem.question_id == question_id).limit(1)
    ):
        return
    if await session.scalar(
        select(Attempt.id).where(Attempt.question_id == question_id).limit(1)
    ):
        return
    await session.delete(question)


async def _read_request_payload(request: Request) -> tuple[dict, object | None]:
    if request.headers.get("content-type", "").split(";", 1)[0] == "multipart/form-data":
        form = await request.form()
        file = form.get("file")
        values = {str(key): value for key, value in form.items() if key != "file"}
        return values, file
    try:
        value = await request.json()
    except ValueError:
        value = {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="INVALID_RESOURCE_BODY")
    return value, None


async def _resource_question_editability(session, resource_id: uuid.UUID) -> dict[uuid.UUID, bool]:
    """Return the semantic-edit lock for each question in a resource.

    The review UI uses this as a display hint; the PATCH endpoint still performs
    the authoritative check.  A mastery log for either an imported ErrorItem or
    a real AssessmentAnswer, and an FSRS review on an ErrorItem, both lock the
    fields that change grading semantics.
    """
    resource_rows = (
        await session.execute(
            select(ResourceQuestion.id, ResourceQuestion.question_id).where(
                ResourceQuestion.resource_id == resource_id
            )
        )
    ).all()
    resource_question_ids = [resource_question_id for resource_question_id, _ in resource_rows]
    question_ids = [question_id for _, question_id in resource_rows]
    if not resource_question_ids:
        return {}
    error_rows = (
        await session.execute(
            select(
                ErrorItem.resource_question_id,
                ErrorItem.question_id,
                ErrorItem.id,
                ErrorItem.fsrs_card_json,
            ).where(
                or_(
                    ErrorItem.resource_question_id.in_(resource_question_ids),
                    ErrorItem.question_id.in_(question_ids),
                )
            )
        )
    ).all()
    answer_rows = (
        await session.execute(
            select(AssessmentAnswer.resource_question_id, AssessmentAnswer.id).where(
                AssessmentAnswer.resource_question_id.in_(resource_question_ids)
            )
        )
    ).all()
    evidence_ids = [error_id for _rq_id, _question_id, error_id, _card in error_rows]
    evidence_ids.extend(answer_id for _rq_id, answer_id in answer_rows)
    evidence_ids.extend(resource_question_ids)
    evidence_ids.extend(question_ids)
    mastered_evidence = set()
    if evidence_ids:
        mastered_evidence = set(
            (
                await session.scalars(
                    select(MasteryLog.evidence_id).where(MasteryLog.evidence_id.in_(evidence_ids))
                )
            ).all()
        )
    locked: set[uuid.UUID] = {
        rq_id
        for rq_id, question_id, error_id, card in error_rows
        if (
            bool((card or {}).get("last_review"))
            or error_id in mastered_evidence
            or question_id in mastered_evidence
        )
    }
    locked.update(
        rq_id for rq_id, answer_id in answer_rows if answer_id in mastered_evidence
    )
    return {resource_question_id: resource_question_id not in locked for resource_question_id in resource_question_ids}


async def _resource_kb_has_evidence(session, resource_id: uuid.UUID) -> bool:
    """Return whether remapping this resource could rewrite learning history."""
    resource_questions = (
        await session.execute(
            select(ResourceQuestion.id, ResourceQuestion.question_id).where(
                ResourceQuestion.resource_id == resource_id
            )
        )
    ).all()
    if not resource_questions:
        return False
    resource_question_ids = [resource_question_id for resource_question_id, _ in resource_questions]
    question_ids = [question_id for _, question_id in resource_questions]
    error_rows = (
        await session.execute(
            select(ErrorItem.id, ErrorItem.fsrs_card_json).where(
                or_(
                    ErrorItem.resource_question_id.in_(resource_question_ids),
                    ErrorItem.question_id.in_(question_ids),
                )
            )
        )
    ).all()
    answer_ids = (
        await session.scalars(
            select(AssessmentAnswer.id).where(
                AssessmentAnswer.resource_question_id.in_(resource_question_ids)
            )
        )
    ).all()
    if error_rows or answer_ids:
        return True
    evidence_ids = resource_question_ids + question_ids
    attempt_ids = (
        await session.scalars(
            select(Attempt.id).where(Attempt.question_id.in_(question_ids))
        )
    ).all()
    if attempt_ids:
        return True
    if await session.scalar(
        select(MasteryLog.id)
        .where(MasteryLog.evidence_id.in_(evidence_ids))
        .limit(1)
    ):
        return True
    return False


async def _file_data(file, settings) -> tuple[str, str, bytes] | None:
    if file is None:
        return None
    filename = safe_filename(getattr(file, "filename", None))
    data = bytearray()
    max_bytes = settings.max_learning_resource_mb * 1024 * 1024
    while True:
        part = await file.read(1024 * 1024)
        if not part:
            break
        data.extend(part)
        if max_bytes > 0 and len(data) > max_bytes:
            raise HTTPException(status_code=413, detail="RESOURCE_FILE_TOO_LARGE")
    kind, mime = validate_resource_file(filename, bytes(data))
    return filename, mime, bytes(data)


async def _resource_quota_used(session, ctx: AuthContext) -> int:
    """按当前个人所有者累计对象，并按 storage_key 去重。

    已确认的 ErrorImport 与 LearningResource 会暂时共享同一个原始对象；
    不能因为两张元数据表同时存在而向同一用户重复计费。
    """
    objects: dict[str, int] = {}
    resource_rows = (
        await session.execute(
            select(LearningResource.storage_key, LearningResource.size_bytes).where(
                LearningResource.owner_id == ctx.user_id,
                LearningResource.storage_key.is_not(None),
            )
        )
    ).all()
    for key, size in resource_rows:
        if key:
            objects[str(key)] = max(objects.get(str(key), 0), int(size or 0))
    import_rows = (
        await session.execute(
            select(ErrorImport.storage_key, ErrorImport.size_bytes).where(
                ErrorImport.user_id == ctx.user_id,
                ErrorImport.storage_key.is_not(None),
            )
        )
    ).all()
    for key, size in import_rows:
        if key:
            objects[str(key)] = max(objects.get(str(key), 0), int(size or 0))
    document_rows = (
        await session.execute(
            select(Document.id, Document.kb_id, Document.filename, Document.size_bytes).where(
                Document.tenant_id == ctx.tenant_id
            )
        )
    ).all()
    for row_id, kb_id, filename, size in document_rows:
        key = f"{ctx.tenant_id}/{kb_id}/{row_id}/{filename}"
        objects[key] = max(objects.get(key, 0), int(size or 0))
    attachment_rows = (
        await session.execute(
            select(ChatAttachment.storage_key, ChatAttachment.size_bytes).where(
                ChatAttachment.tenant_id == ctx.tenant_id
            )
        )
    ).all()
    for key, size in attachment_rows:
        if key:
            objects[str(key)] = max(objects.get(str(key), 0), int(size or 0))
    return sum(objects.values())


@router.post("", status_code=201)
async def create_resource(
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    values, file = await _read_request_payload(request)
    uploaded = await _file_data(file, settings)
    resource_type = str(values.get("resource_type") or "other")
    if resource_type not in RESOURCE_TYPES:
        raise HTTPException(status_code=422, detail="INVALID_RESOURCE_TYPE")
    title = str(values.get("title") or (uploaded[0] if uploaded else "未命名题目资料"))[:255]
    subject = str(values.get("subject") or "general")[:64]
    linked_kb_id = values.get("linked_kb_id") or None
    if linked_kb_id:
        try:
            linked_kb_id = uuid.UUID(str(linked_kb_id))
        except ValueError:
            raise HTTPException(status_code=422, detail="INVALID_KB_ID") from None
    resource_id = uuid.uuid4()
    filename = mime = storage_key = None
    data = None
    if uploaded:
        filename, mime, data = uploaded
        storage_key = resource_storage_key(ctx.tenant_id, resource_id, filename)
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        if linked_kb_id is not None and await session.get(KnowledgeBase, linked_kb_id) is None:
            raise HTTPException(status_code=404, detail="KB_NOT_FOUND")
        count = int(
            await session.scalar(
                select(func.count()).select_from(LearningResource).where(
                    LearningResource.owner_id == ctx.user_id,
                    LearningResource.status != "archived",
                )
            )
            or 0
        )
        if settings.max_learning_resources_per_user > 0 and count >= settings.max_learning_resources_per_user:
            raise HTTPException(status_code=400, detail="RESOURCE_QUOTA_EXCEEDED")
        pending_count = int(
            await session.scalar(
                select(func.count()).select_from(LearningResource).where(
                    LearningResource.owner_id == ctx.user_id,
                    LearningResource.status.in_(("uploaded", "extracting", "reviewing")),
                )
            )
            or 0
        )
        if settings.max_pending_learning_resources > 0 and pending_count >= settings.max_pending_learning_resources:
            raise HTTPException(status_code=429, detail="TOO_MANY_PENDING_RESOURCES")
        if data is not None and settings.max_storage_mb_per_user > 0:
            used = await _resource_quota_used(session, ctx)
            if used + len(data) > settings.max_storage_mb_per_user * 1024 * 1024:
                raise HTTPException(status_code=400, detail="STORAGE_QUOTA_EXCEEDED")
        row = LearningResource(
            id=resource_id,
            tenant_id=ctx.tenant_id,
            owner_type="personal",
            owner_id=ctx.user_id,
            resource_type=resource_type,
            title=title,
            subject=subject,
            grade=str(values.get("grade") or "")[:32] or None,
            term=str(values.get("term") or "")[:32] or None,
            source_filename=filename,
            mime_type=mime,
            size_bytes=len(data or b""),
            storage_key=storage_key,
            sha256=sha256_hex(data) if data is not None else None,
            metadata_json={"source_kind": "image" if uploaded and Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else "document"},
            status="uploaded",
            retrieval_policy="reference" if resource_type in {"textbook", "lecture", "notes"} else "questions_only",
            linked_kb_id=linked_kb_id,
            created_by=ctx.user_id,
        )
        session.add(row)
        await session.flush()
        output = _resource_dict(row)
    if data is not None:
        try:
            await request.app.state.storage.save(storage_key, data)
        except Exception:  # noqa: BLE001 - failed source remains explicitly failed
            async with tenant_session(ctx.tenant_id) as session:
                failed = await session.get(LearningResource, resource_id)
                if failed is not None:
                    failed.status = "failed"
                    failed.metadata_json = {**(failed.metadata_json or {}), "error": "SOURCE_SAVE_FAILED"}
            raise HTTPException(status_code=503, detail="SOURCE_SAVE_FAILED") from None
    return output


@router.get("")
async def list_resources(ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        question_count = (
            select(func.count())
            .select_from(ResourceQuestion)
            .where(ResourceQuestion.resource_id == LearningResource.id)
            .correlate(LearningResource)
            .scalar_subquery()
        )
        pending_count = (
            select(func.count())
            .select_from(ResourceQuestion)
            .where(
                ResourceQuestion.resource_id == LearningResource.id,
                ResourceQuestion.verification_status == "pending",
            )
            .correlate(LearningResource)
            .scalar_subquery()
        )
        rows = (
            await session.execute(
                select(LearningResource)
                .add_columns(question_count.label("question_count"), pending_count.label("pending_count"))
                .where(
                    LearningResource.tenant_id == ctx.tenant_id,
                    LearningResource.owner_id == ctx.user_id,
                )
                .order_by(LearningResource.created_at.desc())
            )
        ).all()
        return [
            _resource_dict(row, question_count=int(question_total), pending_count=int(pending_total))
            for row, question_total, pending_total in rows
        ]


@router.get("/{resource_id}")
async def get_resource(resource_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await _owned_resource(session, resource_id, ctx)
        question_count = int(
            await session.scalar(
                select(func.count()).select_from(ResourceQuestion).where(ResourceQuestion.resource_id == row.id)
            )
            or 0
        )
        pending_count = int(
            await session.scalar(
                select(func.count()).select_from(ResourceQuestion).where(
                    ResourceQuestion.resource_id == row.id,
                    ResourceQuestion.verification_status == "pending",
                )
            )
            or 0
        )
        return _resource_dict(row, question_count=question_count, pending_count=pending_count)


@router.patch("/{resource_id}")
async def update_resource(
    resource_id: uuid.UUID,
    body: ResourcePatchIn,
    ctx: AuthContext = Depends(api_rate_limited),
):
    values = body.model_dump(exclude_unset=True)
    if "resource_type" in values and values["resource_type"] not in RESOURCE_TYPES:
        raise HTTPException(status_code=422, detail="INVALID_RESOURCE_TYPE")
    if values.get("retrieval_policy") not in {None, "none", "reference", "questions_only"}:
        raise HTTPException(status_code=422, detail="INVALID_RETRIEVAL_POLICY")
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await _owned_resource(session, resource_id, ctx, lock=True)
        if row.status == "archived":
            raise HTTPException(status_code=409, detail="RESOURCE_ARCHIVED")
        if "linked_kb_id" in values:
            linked_kb_id = values["linked_kb_id"]
            if linked_kb_id is not None and await session.get(KnowledgeBase, linked_kb_id) is None:
                # KnowledgeBase is read through the same scoped tenant session;
                # an inaccessible organization or another user's KB is therefore
                # intentionally indistinguishable from a missing one.
                raise HTTPException(status_code=404, detail="KB_NOT_FOUND")
            if linked_kb_id != row.linked_kb_id:
                if await _resource_kb_has_evidence(session, row.id):
                    raise HTTPException(
                        status_code=409,
                        detail="RESOURCE_KB_LOCKED_AFTER_EVIDENCE",
                    )
                question_rows = (
                    await session.execute(
                        select(ResourceQuestion, Question)
                        .join(Question, Question.id == ResourceQuestion.question_id)
                        .where(ResourceQuestion.resource_id == row.id)
                        .with_for_update()
                    )
                ).all()
                for _resource_question, question in question_rows:
                    meta = dict(question.source_meta_json or {})
                    kc_names = meta.get("kc_names") or []
                    question.kc_ids = (
                        await _resolve_kc_ids(
                            session,
                            str(linked_kb_id) if linked_kb_id else None,
                            kc_names,
                            user_id=ctx.user_id,
                        )
                        if linked_kb_id
                        else []
                    )
                    meta["kb_id"] = str(linked_kb_id) if linked_kb_id else None
                    question.source_meta_json = meta
            row.linked_kb_id = linked_kb_id
            values.pop("linked_kb_id")
        for key, value in values.items():
            setattr(row, key, value)
        return _resource_dict(row)


@router.post("/{resource_id}/archive")
async def archive_resource(resource_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await _owned_resource(session, resource_id, ctx, lock=True)
        if row.status != "archived":
            row.status = "archived"
        return _resource_dict(row)


@router.post("/{resource_id}/restore")
async def restore_resource(resource_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await _owned_resource(session, resource_id, ctx, lock=True)
        if row.status == "archived":
            pending = await session.scalar(
                select(ResourceQuestion.id)
                .where(
                    ResourceQuestion.resource_id == row.id,
                    ResourceQuestion.verification_status == "pending",
                )
                .limit(1)
            )
            row.status = "reviewing" if pending else "ready"
        return _resource_dict(row)


@router.get("/{resource_id}/source")
async def read_resource_source(
    resource_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    # Read metadata in a short transaction, then release the DB connection before
    # potentially slow object storage I/O.  Source reads never use FOR UPDATE.
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await _owned_resource(session, resource_id, ctx)
        storage_key, filename, mime = row.storage_key, row.source_filename, row.mime_type
    if not storage_key or not filename:
        raise HTTPException(status_code=404, detail="SOURCE_NOT_FOUND")
    try:
        data = await request.app.state.storage.load(storage_key)
    except Exception:  # noqa: BLE001 - do not leak object-store details
        raise HTTPException(status_code=404, detail="SOURCE_NOT_FOUND") from None
    mime = mime or "application/octet-stream"
    return Response(
        content=data,
        media_type=mime,
        headers={**safe_source_headers(filename, mime), "Content-Length": str(len(data))},
    )


@router.delete("/{resource_id}", status_code=204)
async def delete_resource(
    resource_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await _owned_resource(session, resource_id, ctx, lock=True)
        if await session.scalar(
            select(TutorProblemDraft.id)
            .where(TutorProblemDraft.resource_id == row.id)
            .limit(1)
        ):
            raise HTTPException(status_code=409, detail="TUTOR_RESOURCE_MANAGED")
        linked = await session.scalar(
            select(func.count()).select_from(ErrorItem).where(ErrorItem.resource_question_id.in_(
                select(ResourceQuestion.id).where(ResourceQuestion.resource_id == row.id)
            ))
        )
        if linked:
            raise HTTPException(status_code=409, detail="RESOURCE_HAS_ERROR_ITEMS")
        has_assessment = await session.scalar(
            select(func.count())
            .select_from(AssessmentAttempt)
            .where(AssessmentAttempt.resource_id == row.id)
        )
        if has_assessment:
            raise HTTPException(status_code=409, detail="RESOURCE_HAS_ASSESSMENTS")
        question_rows = (
            await session.execute(
                select(ResourceQuestion.question_id)
                .where(ResourceQuestion.resource_id == row.id)
            )
        ).all()
        question_ids = [question_id for (question_id,) in question_rows]
        if question_ids:
            if await session.scalar(
                select(func.count()).select_from(ErrorItem).where(
                    ErrorItem.question_id.in_(question_ids)
                )
            ):
                raise HTTPException(status_code=409, detail="RESOURCE_HAS_ERROR_ITEMS")
            if await session.scalar(
                select(func.count()).select_from(AssessmentAnswer).where(
                    AssessmentAnswer.resource_question_id.in_(
                        select(ResourceQuestion.id).where(ResourceQuestion.resource_id == row.id)
                    )
                )
            ):
                raise HTTPException(status_code=409, detail="RESOURCE_HAS_ASSESSMENTS")
            if await session.scalar(
                select(func.count()).select_from(Attempt).where(Attempt.question_id.in_(question_ids))
            ):
                raise HTTPException(status_code=409, detail="RESOURCE_HAS_ATTEMPTS")
        storage_key = row.storage_key
        row.status = "failed"
        row.metadata_json = {**(row.metadata_json or {}), "delete_pending": True}
    try:
        if storage_key:
            await request.app.state.storage.delete_prefix(resource_storage_prefix(ctx.tenant_id, resource_id))
    except Exception:  # noqa: BLE001 - leave a retryable non-deleted row
        raise HTTPException(status_code=503, detail="RESOURCE_DELETE_FAILED") from None
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await session.scalar(
            select(LearningResource)
            .where(
                LearningResource.id == resource_id,
                LearningResource.owner_id == ctx.user_id,
            )
            .with_for_update()
        )
        if row is not None and (row.metadata_json or {}).get("delete_pending"):
            question_ids = [
                question_id
                for (question_id,) in (
                    await session.execute(
                        select(ResourceQuestion.question_id).where(
                            ResourceQuestion.resource_id == row.id
                        )
                    )
                ).all()
            ]
            await session.execute(
                delete(ResourceQuestion).where(ResourceQuestion.resource_id == row.id)
            )
            await session.flush()
            await session.delete(row)
            await session.flush()
            for question_id in question_ids:
                await _delete_orphan_question(session, question_id)


@router.post("/{resource_id}/extract", status_code=202)
async def extract_resource(
    resource_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await _owned_resource(session, resource_id, ctx, lock=True)
        if row.status == "archived":
            raise HTTPException(status_code=409, detail="RESOURCE_ARCHIVED")
        if row.resource_type in {"textbook", "lecture", "notes"} or row.retrieval_policy == "reference":
            raise HTTPException(status_code=409, detail="REFERENCE_RESOURCE_NOT_EXTRACTABLE")
        if not row.storage_key:
            raise HTTPException(status_code=422, detail="RESOURCE_SOURCE_REQUIRED")
        retryable_partial = bool((row.metadata_json or {}).get("failed_pages"))
        if row.status in {"extracting", "ready"} or (row.status == "reviewing" and not retryable_partial):
            return {"resource_id": str(row.id), "status": row.status}
        row.status = "extracting"
        row.metadata_json = {k: v for k, v in (row.metadata_json or {}).items() if k not in {"error", "delete_pending"}}
    job_id = f"resource-extract:{resource_id}:{uuid.uuid4().hex}"
    try:
        await request.app.state.arq.enqueue_job(
            "extract_learning_resource", str(ctx.tenant_id), str(resource_id), _job_id=job_id
        )
    except Exception:  # noqa: BLE001 - make status retryable
        async with tenant_session(ctx.tenant_id) as session:
            row = await session.get(LearningResource, resource_id)
            if row is not None:
                row.status = "failed"
                row.metadata_json = {**(row.metadata_json or {}), "error": "EXTRACTION_QUEUE_FAILED"}
        raise HTTPException(status_code=503, detail="EXTRACTION_QUEUE_FAILED") from None
    return {"resource_id": str(resource_id), "status": "extracting", "job_id": job_id}


@router.get("/{resource_id}/questions")
async def list_resource_questions(resource_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        resource = await _owned_resource(session, resource_id, ctx)
        rows = (
            await session.execute(
                select(ResourceQuestion, Question)
                .join(Question, Question.id == ResourceQuestion.question_id)
                .where(ResourceQuestion.resource_id == resource.id)
                .order_by(ResourceQuestion.ordinal.asc(), ResourceQuestion.created_at.asc())
            )
        ).all()
        locked = await session.scalar(
            select(AssessmentAttempt.id).where(
                AssessmentAttempt.resource_id == resource.id,
                AssessmentAttempt.student_id == ctx.user_id,
                AssessmentAttempt.status == "draft",
            ).limit(1)
        )
        editability = await _resource_question_editability(session, resource.id)
        return [
            _question_dict(
                question,
                rq,
                reveal_answer=locked is None,
                content_editable=editability.get(rq.id, True),
            )
            for rq, question in rows
        ]


@router.post("/{resource_id}/questions/manual", status_code=201)
async def add_manual_question(
    resource_id: uuid.UUID,
    body: ResourceQuestionIn,
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    values = body.normalized()
    if not values.get("stem"):
        raise HTTPException(status_code=422, detail="QUESTION_STEM_REQUIRED")
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        resource = await _owned_resource(session, resource_id, ctx, lock=True)
        count = int(await session.scalar(select(func.count()).select_from(ResourceQuestion).where(ResourceQuestion.resource_id == resource.id)) or 0)
        if settings.max_learning_resource_questions > 0 and count >= settings.max_learning_resource_questions:
            raise HTTPException(status_code=400, detail="RESOURCE_QUESTION_QUOTA_EXCEEDED")
        kc_ids = await _resolve_kc_ids(
            session,
            str(resource.linked_kb_id) if resource.linked_kb_id else None,
            values.get("kc_names", []),
            user_id=ctx.user_id,
        )
        q = Question(
            tenant_id=ctx.tenant_id,
            quiz_id=None,
            type=values.get("type") or "short",
            stem=values["stem"],
            options_json=values.get("options", []),
            answer=(values.get("answer") or values.get("detected_reference_answer") or "").strip(),
            explanation=values.get("explanation", "") or "",
            kc_ids=kc_ids,
            source_type="manual",
            source_meta_json={
                "resource_id": str(resource.id),
                "detected_student_answer": values.get("detected_student_answer", ""),
                "detected_reference_answer": values.get("detected_reference_answer", ""),
                "kc_names": values.get("kc_names", []),
            },
            difficulty=values.get("difficulty") or "mid",
        )
        session.add(q)
        await session.flush()
        rq = ResourceQuestion(
            tenant_id=ctx.tenant_id,
            resource_id=resource.id,
            question_id=q.id,
            ordinal=count,
            question_no=values.get("question_no") or str(count + 1),
            section_path=values.get("section_path") or "",
            page_no=values.get("page_no"),
            source_region_json=values.get("source_region") or {},
            points=values.get("points"),
            extraction_confidence=values.get("extraction_confidence"),
            verification_status="pending",
        )
        session.add(rq)
        resource.status = "reviewing"
        await session.flush()
        return _question_dict(q, rq)


@router.post("/{resource_id}/confirm")
async def confirm_resource_questions(
    resource_id: uuid.UUID,
    body: ConfirmResourceIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    selected_ids = set(body.resource_question_ids or []) | set(body.error_question_ids or [])
    selected_question_ids = set(body.question_ids or [])
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        resource = await _owned_resource(session, resource_id, ctx, lock=True)
        rows = (
            await session.execute(
                select(ResourceQuestion, Question)
                .join(Question, Question.id == ResourceQuestion.question_id)
                .where(ResourceQuestion.resource_id == resource.id)
                .with_for_update()
            )
        ).all()
        selected = [
            (rq, question)
            for rq, question in rows
            if rq.id in selected_ids or question.id in selected_question_ids
        ]
        for rq, _question in selected:
            if rq.verification_status != "verified":
                raise HTTPException(status_code=409, detail="RESOURCE_QUESTION_NOT_VERIFIED")
        created: list[str] = []
        review_now = await timectl.now(request.app.state.redis)
        if body.add_to_error_book:
            for rq, question in selected:
                meta = dict(question.source_meta_json or {})
                existing = await session.scalar(
                    select(ErrorItem).where(
                        ErrorItem.user_id == ctx.user_id,
                        ErrorItem.resource_question_id == rq.id,
                    )
                )
                if existing is not None:
                    question.source_meta_json = {**meta, "error_selected": True}
                    created.append(str(existing.id))
                    continue
                session.add(
                    ErrorItem(
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        question_id=question.id,
                        kc_ids=list(question.kc_ids or []),
                        cause="knowledge_gap",
                        state="new",
                        verification_status="verified",
                        resource_question_id=rq.id,
                        original_response=str(meta.get("detected_student_answer") or ""),
                        fsrs_card_json=new_card_json(review_now),
                    )
                )
                await session.flush()
                latest = await session.scalar(
                    select(ErrorItem).where(
                        ErrorItem.user_id == ctx.user_id,
                        ErrorItem.resource_question_id == rq.id,
                    )
                )
                if latest is not None:
                    created.append(str(latest.id))
                question.source_meta_json = {**meta, "error_selected": True}
        pending = await session.scalar(
            select(func.count()).select_from(ResourceQuestion).where(
                ResourceQuestion.resource_id == resource.id,
                ResourceQuestion.verification_status == "pending",
            )
        )
        if not pending:
            resource.status = "ready"
        return {
            "resource_id": str(resource.id),
            "status": resource.status,
            "error_item_ids": created,
            "confirmed_question_count": len(selected),
        }


async def _edit_resource_question(
    resource_question_id: uuid.UUID,
    body: ResourceQuestionIn,
    ctx: AuthContext,
    *,
    verify_only: bool = False,
):
    values = body.normalized()
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        rq, resource, question = await _owned_resource_question(session, resource_question_id, ctx, lock=True)
        current = rq.verification_status
        requested = values.get("verification_status")
        if verify_only:
            requested = "verified"
        if current == "verified" and requested not in {None, "verified"}:
            raise HTTPException(status_code=409, detail="VERIFICATION_ROLLBACK_FORBIDDEN")
        if current == "rejected" and requested != "rejected":
            raise HTTPException(status_code=409, detail="VERIFICATION_ROLLBACK_FORBIDDEN")
        errors = (
            await session.scalars(
                select(ErrorItem).where(
                    or_(
                        ErrorItem.resource_question_id == rq.id,
                        ErrorItem.question_id == question.id,
                    )
                )
            )
        ).all()
        has_evidence = any(bool((item.fsrs_card_json or {}).get("last_review")) for item in errors)
        if errors:
            has_evidence = has_evidence or (
                await session.scalar(
                    select(MasteryLog.id).where(
                        MasteryLog.evidence_id.in_([item.id for item in errors])
                    ).limit(1)
                )
                is not None
            )
        assessment_evidence = (
            await session.scalar(
                select(MasteryLog.id)
                .where(
                    MasteryLog.evidence_id.in_(
                        select(AssessmentAnswer.id).where(
                            AssessmentAnswer.resource_question_id == rq.id
                        )
                    )
                )
                .limit(1)
            )
            is not None
        )
        has_evidence = has_evidence or assessment_evidence
        semantic_changed = any(
            key in values
            and values[key] != {
                "type": question.type,
                "options": question.options_json,
                "answer": question.answer,
                "kc_names": (question.source_meta_json or {}).get("kc_names", []),
            }.get(key)
            for key in ("type", "options", "answer", "kc_names")
        )
        if has_evidence and semantic_changed:
            raise HTTPException(status_code=409, detail="ERROR_ITEM_LOCKED_AFTER_REVIEW")
        if "type" in values:
            question.type = values["type"]
        if "stem" in values:
            question.stem = values["stem"] or ""
        if "options" in values:
            question.options_json = values["options"]
        if "answer" in values or "detected_reference_answer" in values:
            question.answer = (values.get("answer") or values.get("detected_reference_answer") or "").strip()
        if "explanation" in values:
            question.explanation = values["explanation"] or ""
        if "difficulty" in values:
            question.difficulty = values["difficulty"]
        if "kc_names" in values:
            kc_ids = await _resolve_kc_ids(
                session,
                str(resource.linked_kb_id) if resource.linked_kb_id else None,
                values["kc_names"],
                user_id=ctx.user_id,
            )
            question.kc_ids = kc_ids
            for item in errors:
                item.kc_ids = kc_ids
        meta = dict(question.source_meta_json or {})
        for key in ("detected_student_answer", "detected_reference_answer", "kc_names"):
            if key in values:
                meta[key] = values[key]
        question.source_meta_json = meta
        for key, attr in (
            ("question_no", "question_no"),
            ("section_path", "section_path"),
            ("page_no", "page_no"),
            ("source_region", "source_region_json"),
            ("points", "points"),
            ("extraction_confidence", "extraction_confidence"),
        ):
            if key in values:
                setattr(rq, attr, values[key] if key != "source_region" else values[key] or {})
        if requested is not None:
            rq.verification_status = requested
        if rq.verification_status == "verified" and resource.status in {"uploaded", "failed", "extracting"}:
            resource.status = "reviewing"
        if rq.verification_status == "rejected":
            resource.status = "reviewing"
        await session.flush()
        editability = await _resource_question_editability(session, resource.id)
        return _question_dict(question, rq, content_editable=editability.get(rq.id, True))


@questions_router.patch("/{resource_question_id}")
async def update_resource_question(
    resource_question_id: uuid.UUID,
    body: ResourceQuestionIn,
    ctx: AuthContext = Depends(api_rate_limited),
):
    return await _edit_resource_question(resource_question_id, body, ctx)


@questions_router.post("/{resource_question_id}/verify")
async def verify_resource_question(
    resource_question_id: uuid.UUID,
    body: ResourceQuestionIn | None = None,
    ctx: AuthContext = Depends(api_rate_limited),
):
    return await _edit_resource_question(
        resource_question_id, body or ResourceQuestionIn(verification_status="verified"), ctx, verify_only=True
    )


@questions_router.delete("/{resource_question_id}", status_code=204)
async def delete_resource_question(
    resource_question_id: uuid.UUID,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        rq, _resource, question = await _owned_resource_question(
            session, resource_question_id, ctx, lock=True
        )
        if await session.scalar(select(func.count()).select_from(ErrorItem).where(ErrorItem.resource_question_id == rq.id)):
            raise HTTPException(status_code=409, detail="RESOURCE_QUESTION_HAS_ERROR_ITEM")
        if await session.scalar(select(func.count()).select_from(AssessmentAnswer).where(AssessmentAnswer.resource_question_id == rq.id)):
            raise HTTPException(status_code=409, detail="RESOURCE_QUESTION_HAS_ASSESSMENT")
        if await session.scalar(select(func.count()).select_from(ErrorItem).where(ErrorItem.question_id == question.id)):
            raise HTTPException(status_code=409, detail="RESOURCE_QUESTION_HAS_ERROR_ITEM")
        if await session.scalar(select(func.count()).select_from(Attempt).where(Attempt.question_id == question.id)):
            raise HTTPException(status_code=409, detail="RESOURCE_QUESTION_HAS_ATTEMPT")
        if question.quiz_id is not None:
            raise HTTPException(status_code=409, detail="QUESTION_HAS_QUIZ_OWNER")
        await session.delete(rq)
        await session.flush()
        await _delete_orphan_question(session, question.id)
