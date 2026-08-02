from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path

from eduagents_api.db import tenant_session
from eduagents_api.models import LearningResource, TutorProblemDraft
from eduagents_api.resource_utils import normalize_extracted_question, parse_json_payload
from eduagents_core.config import Settings, get_settings
from eduagents_core.document_recognition import (
    PdfRecognitionPolicy,
    inspect_pdf,
    recognize_page_image,
    recognize_pdf,
)
from eduagents_core.gateway import ChatMessage, ChatRequest, Tier
from eduagents_core.logging import get_logger
from eduagents_core.parsing.registry import get_parser

from .enrichment import get_worker_gateway
from .resources import _numbered_blocks

log = get_logger("worker.tutor_recognition")

_IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_PROMPT = """你是分级辅导的题目识别助手。忠实识别输入中出现的所有题目，返回 JSON 数组。
每题字段：question_no, page_no, type, stem, options, confidence。
type 只能是 single、multi、blank、short。不清楚的内容留空，绝不能补造题干、选项或答案。
保留给出的真实页码。只输出 JSON 数组。"""


def _tutor_page_request_id(
    request_namespace: str, *, tenant_id: str, user_id: str, page_number: int
) -> str:
    digest = hashlib.sha256(
        f"{tenant_id}\0{user_id}\0{request_namespace}\0{page_number}".encode()
    ).hexdigest()
    return f"tutorocr-{digest[:56]}"


def _payload_questions(text: str) -> list[dict]:
    payload = parse_json_payload(text)
    if isinstance(payload, dict):
        payload = payload.get("questions") or []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


async def _model_questions(
    settings: Settings,
    gateway,
    content,
    *,
    tenant_id: str,
    user_id: str,
    vision: bool,
) -> list[dict]:
    result = await asyncio.wait_for(
        gateway.chat(
            ChatRequest(
                messages=(ChatMessage(role="user", content=content),),
                tier=Tier.standard,
                temperature=0.0,
                max_tokens=5000,
                json_mode=True,
                feature="tutor_problem_recognition",
                tenant_id=tenant_id,
                user_id=user_id,
                model_override=(
                    settings.vision_model
                    if vision and not settings.model_routing_enabled
                    else None
                ),
                allow_tier_fallback=not vision,
                credential_policy="platform" if vision else "text",
            )
        ),
        timeout=(settings.tutor_ocr_timeout_seconds if vision else settings.tutor_recognition_timeout_seconds),
    )
    return _payload_questions(result.text)


def _tutor_pdf_policy(settings: Settings) -> PdfRecognitionPolicy:
    render_limits = [
        value
        for value in (settings.pdf_max_render_mb, settings.max_tutor_ocr_image_mb)
        if value > 0
    ]
    render_mb = min(render_limits) if render_limits else 0
    return replace(
        PdfRecognitionPolicy.from_settings(
            settings, max_pages=settings.max_tutor_attachment_pages
        ),
        render_dpi=min(settings.pdf_render_dpi, settings.max_tutor_ocr_dpi),
        max_render_bytes=render_mb * 1024 * 1024,
        max_vision_pages=settings.max_tutor_recognition_pages,
        vision_concurrency=settings.max_tutor_ocr_concurrency,
        page_timeout_seconds=settings.tutor_ocr_timeout_seconds,
        document_timeout_seconds=settings.tutor_recognition_timeout_seconds,
    )


async def _document_text(filename: str, data: bytes) -> tuple[str, int, list[str]]:
    suffix = Path(filename).suffix.lower()
    warnings: list[str] = []
    if suffix in {".docx", ".pptx"}:
        media_prefix = "word/media/" if suffix == ".docx" else "ppt/media/"
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
                if any(name.startswith(media_prefix) for name in archive.namelist()):
                    warnings.append("EMBEDDED_IMAGES_NOT_RECOGNIZED")
        except zipfile.BadZipFile as exc:
            raise ValueError("TUTOR_ATTACHMENT_PARSE_FAILED") from exc
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        path = Path(tmp.name)
    try:
        parsed = await asyncio.to_thread(get_parser(suffix).parse, path)
    finally:
        path.unlink(missing_ok=True)
    parts = [
        f"[第 {element.page} 页]\n{element.text}" if element.page else element.text
        for element in parsed.elements
        if element.text.strip()
    ]
    return "\n\n".join(parts), parsed.page_count or 0, warnings


async def _recognize_source(
    settings: Settings,
    gateway,
    filename: str,
    data: bytes,
    selected_pages: list[int],
    tenant_id: str,
    user_id: str,
    request_namespace: str = "",
) -> tuple[list[dict], int, list[str]]:
    suffix = Path(filename).suffix.lower()
    if settings.gateway_mode == "stub":
        if suffix in _IMAGE_MIMES:
            return [], 1, ["VISION_MODEL_STUB_MANUAL_ENTRY_REQUIRED"]
        if suffix == ".pdf":
            inspected = await asyncio.to_thread(
                inspect_pdf,
                data,
                _tutor_pdf_policy(settings),
                selected_pages=selected_pages,
            )
            text = "\n\n".join(
                f"[第 {page.page_number} 页]\n{page.native_text}"
                for page in inspected.pages
                if page.extraction_method == "native_text" and page.native_text
            )
            requires_vision = any(
                page.extraction_method in {"scanned", "mixed"} for page in inspected.pages
            )
            warnings = ["SCANNED_PAGES_REQUIRE_VISION"] if requires_vision else []
            return _numbered_blocks(text, 50) if text else [], inspected.page_count, warnings
        text, page_count, warnings = await _document_text(filename, data)
        return _numbered_blocks(text, 50) if text else [], page_count, warnings

    if suffix in _IMAGE_MIMES:
        markdown = await recognize_page_image(
            gateway,
            data,
            page_number=1,
            extraction_method="scanned",
                vision_model=(
                    settings.vision_model
                    if not settings.model_routing_enabled
                    else settings.opencode_vision_model
                ),
            tenant_id=tenant_id,
            user_id=user_id,
            timeout_seconds=settings.tutor_ocr_timeout_seconds,
            mime_type=_IMAGE_MIMES[suffix],
            request_id=(
                _tutor_page_request_id(
                    request_namespace, tenant_id=tenant_id, user_id=user_id, page_number=1
                )
                if request_namespace
                else ""
            ),
        )
        raw = await _model_questions(
            settings,
            gateway,
            f"{_PROMPT}\n\n识别稿：\n{markdown[:80000]}",
            tenant_id=tenant_id,
            user_id=user_id,
            vision=False,
        )
        for item in raw:
            item["page_no"] = 1
        return raw, 1, []

    if suffix == ".pdf":
        recognized = await recognize_pdf(
            data,
            policy=_tutor_pdf_policy(settings),
            gateway=gateway,
                vision_model=(
                    settings.vision_model
                    if not settings.model_routing_enabled
                    else settings.opencode_vision_model
                ),
            tenant_id=tenant_id,
            user_id=user_id,
            selected_pages=selected_pages,
            request_namespace=request_namespace,
        )
        if not recognized.successful_pages:
            raise ValueError("TUTOR_PDF_ALL_PAGES_FAILED")
        questions = await _model_questions(
            settings,
            gateway,
            f"{_PROMPT}\n\n识别稿：\n{recognized.markdown[:80000]}",
            tenant_id=tenant_id,
            user_id=user_id,
            vision=False,
        )
        warnings = list(recognized.warnings)
        return questions, recognized.page_count, warnings

    text, page_count, warnings = await _document_text(filename, data)
    if not text.strip():
        return [], page_count, warnings
    raw = await _model_questions(
        settings,
        gateway,
        f"{_PROMPT}\n\n原文：\n{text[:80000]}",
        tenant_id=tenant_id,
        user_id=user_id,
        vision=False,
    )
    return raw, page_count, warnings


async def _mark_failed(
    tenant_id: str, draft_id: uuid.UUID, code: str, warnings: list[str] | None = None
) -> None:
    async with tenant_session(tenant_id) as session:
        draft = await session.get(TutorProblemDraft, draft_id, with_for_update=True)
        if draft is not None and draft.status == "recognizing":
            draft.status = "failed"
            draft.error_code = code[:64]
            if warnings is not None:
                draft.warnings_json = warnings
            if draft.resource_id:
                resource = await session.get(LearningResource, draft.resource_id)
                if resource is not None:
                    resource.status = "failed"
                    resource.metadata_json = {**(resource.metadata_json or {}), "error": code[:64]}


async def recognize_tutor_problem(
    ctx: dict,
    tenant_id: str,
    draft_id: str,
    *,
    gateway=None,
    settings: Settings | None = None,
) -> str:
    settings = settings or ctx.get("settings") or get_settings()
    did = uuid.UUID(draft_id)
    try:
        async with tenant_session(tenant_id) as session:
            draft = await session.get(TutorProblemDraft, did)
            if draft is None:
                return "missing"
            if str(draft.tenant_id) != tenant_id or str(draft.user_id) != tenant_id:
                return "owner-mismatch"
            if draft.status == "review":
                return "already-recognized"
            if draft.status != "recognizing" or not draft.resource_id:
                return "not-runnable"
            if draft.recognition_attempts > settings.max_tutor_recognition_attempts:
                raise ValueError("TUTOR_RECOGNITION_LIMIT_REACHED")
            resource = await session.get(LearningResource, draft.resource_id)
            if (
                resource is None
                or resource.owner_id != draft.user_id
                or resource.tenant_id != draft.tenant_id
                or not resource.storage_key
                or not resource.source_filename
            ):
                raise ValueError("TUTOR_ATTACHMENT_NOT_FOUND")
            filename = resource.source_filename
            storage_key = resource.storage_key
            selected_pages = [int(page) for page in (draft.selected_pages_json or [])]
            user_id = str(draft.user_id)
            recognition_attempt = draft.recognition_attempts
        from eduagents_core.storage import create_storage

        data = await create_storage(settings).load(storage_key)
        gateway = gateway or get_worker_gateway(settings)
        raw, page_count, warnings = await asyncio.wait_for(
            _recognize_source(
                settings,
                gateway,
                filename,
                data,
                selected_pages,
                tenant_id,
                user_id,
                request_namespace=f"{draft_id}:attempt:{recognition_attempt}",
            ),
            timeout=settings.tutor_recognition_timeout_seconds,
        )
        questions = [normalize_extracted_question(item, index + 1) for index, item in enumerate(raw)]
        questions = [item for item in questions if item["stem"]]
        if not questions:
            await _mark_failed(
                tenant_id, did, "TUTOR_NO_QUESTIONS_RECOGNIZED", warnings
            )
            return "failed"
        async with tenant_session(tenant_id) as session:
            draft = await session.get(TutorProblemDraft, did, with_for_update=True)
            if draft is None or draft.status != "recognizing" or not draft.resource_id:
                return "stale"
            resource = await session.get(LearningResource, draft.resource_id, with_for_update=True)
            if resource is None or resource.owner_id != draft.user_id:
                raise ValueError("TUTOR_ATTACHMENT_NOT_FOUND")
            if draft.recognized_questions_json:
                return "already-recognized"
            candidates = [
                {
                    "index": index,
                    "question_no": item["question_no"],
                    "page_no": item["page_no"],
                    "type": item["type"],
                    "stem": item["stem"],
                    "options": item["options"],
                    "confidence": item["confidence"],
                }
                for index, item in enumerate(questions)
            ]
            first = candidates[0]
            draft.recognized_questions_json = candidates
            draft.selected_question_index = 0
            draft.draft_stem = first["stem"]
            draft.draft_options_json = first["options"]
            draft.status = "review"
            draft.warnings_json = warnings
            draft.error_code = None
            resource.status = "reviewing"
            resource.page_count = page_count or resource.page_count
            resource.metadata_json = {
                **(resource.metadata_json or {}),
                "question_count": len(questions),
                "warnings": warnings,
            }
        return "review"
    except TimeoutError:
        await _mark_failed(tenant_id, did, "TUTOR_RECOGNITION_TIMEOUT")
        return "failed"
    except FileNotFoundError:
        await _mark_failed(tenant_id, did, "TUTOR_ATTACHMENT_NOT_FOUND")
        return "failed"
    except ValueError as exc:
        code = str(exc) if str(exc).startswith(("TUTOR_", "VISION_")) else "TUTOR_RECOGNITION_FAILED"
        await _mark_failed(tenant_id, did, code)
        return "failed"
    except Exception:
        log.exception("tutor_recognition_failed", tenant_id=tenant_id, draft_id=draft_id)
        await _mark_failed(tenant_id, did, "TUTOR_RECOGNITION_FAILED")
        return "failed"
