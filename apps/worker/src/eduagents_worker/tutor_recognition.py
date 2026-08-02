from __future__ import annotations

import asyncio
import base64
import tempfile
import uuid
import zipfile
from pathlib import Path

import pymupdf
from eduagents_api.db import tenant_session
from eduagents_api.models import LearningResource, TutorProblemDraft
from eduagents_api.resource_utils import normalize_extracted_question, parse_json_payload
from eduagents_core.config import Settings, get_settings
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
_LOW_TEXT_CHARS = 40


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
                model_override=settings.vision_model if vision else None,
                allow_tier_fallback=not vision,
                credential_policy="platform" if vision else "text",
            )
        ),
        timeout=(settings.tutor_ocr_timeout_seconds if vision else settings.tutor_recognition_timeout_seconds),
    )
    return _payload_questions(result.text)


def _pdf_selected_pages(
    data: bytes, selected_pages: list[int], settings: Settings
) -> tuple[dict[int, str], dict[int, bytes], int]:
    text_pages: dict[int, str] = {}
    image_pages: dict[int, bytes] = {}
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise ValueError("TUTOR_PDF_PASSWORD_PROTECTED")
            page_count = document.page_count
            if settings.max_tutor_attachment_pages > 0 and page_count > settings.max_tutor_attachment_pages:
                raise ValueError("TUTOR_PDF_PAGE_LIMIT_EXCEEDED")
            for page_no in selected_pages:
                if page_no < 1 or page_no > page_count:
                    raise ValueError("TUTOR_INVALID_PAGE_SELECTION")
                page = document.load_page(page_no - 1)
                text = page.get_text("text").strip()
                if len("".join(text.split())) >= _LOW_TEXT_CHARS:
                    text_pages[page_no] = text
                    continue
                dpi = max(72, min(settings.max_tutor_ocr_dpi, 200))
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                png = pixmap.tobytes("png")
                maximum = settings.max_tutor_ocr_image_mb * 1024 * 1024
                if maximum > 0 and len(png) > maximum and dpi > 96:
                    png = page.get_pixmap(dpi=96, alpha=False).tobytes("png")
                if maximum > 0 and len(png) > maximum:
                    raise ValueError("TUTOR_OCR_IMAGE_TOO_LARGE")
                image_pages[page_no] = png
            return text_pages, image_pages, page_count
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("TUTOR_ATTACHMENT_PARSE_FAILED") from exc


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
) -> tuple[list[dict], int, list[str]]:
    suffix = Path(filename).suffix.lower()
    if settings.gateway_mode == "stub":
        if suffix in _IMAGE_MIMES:
            return [], 1, ["VISION_MODEL_STUB_MANUAL_ENTRY_REQUIRED"]
        if suffix == ".pdf":
            text_pages, image_pages, page_count = await asyncio.to_thread(
                _pdf_selected_pages, data, selected_pages, settings
            )
            text = "\n\n".join(f"[第 {page} 页]\n{value}" for page, value in text_pages.items())
            warnings = ["SCANNED_PAGES_REQUIRE_VISION"] if image_pages else []
            return _numbered_blocks(text, 50) if text else [], page_count, warnings
        text, page_count, warnings = await _document_text(filename, data)
        return _numbered_blocks(text, 50) if text else [], page_count, warnings

    if suffix in _IMAGE_MIMES:
        if not settings.vision_model:
            raise ValueError("VISION_MODEL_UNAVAILABLE")
        content = [
            {"type": "text", "text": f"{_PROMPT}\n该图片页码为 1。"},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{_IMAGE_MIMES[suffix]};base64,{base64.b64encode(data).decode('ascii')}"
                },
            },
        ]
        raw = await _model_questions(
            settings, gateway, content, tenant_id=tenant_id, user_id=user_id, vision=True
        )
        for item in raw:
            item["page_no"] = 1
        return raw, 1, []

    if suffix == ".pdf":
        text_pages, image_pages, page_count = await asyncio.to_thread(
            _pdf_selected_pages, data, selected_pages, settings
        )
        questions: list[dict] = []
        if text_pages:
            text = "\n\n".join(
                f"[第 {page_no} 页]\n{value}" for page_no, value in text_pages.items()
            )
            questions.extend(
                await _model_questions(
                    settings,
                    gateway,
                    f"{_PROMPT}\n\n{text[:80000]}",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    vision=False,
                )
            )
        if image_pages:
            if not settings.vision_model:
                raise ValueError("VISION_MODEL_UNAVAILABLE")
            semaphore = asyncio.Semaphore(max(1, settings.max_tutor_ocr_concurrency))

            async def recognize_page(page_no: int, image: bytes) -> list[dict]:
                async with semaphore:
                    content = [
                        {"type": "text", "text": f"{_PROMPT}\n该扫描页真实页码为 {page_no}。"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64.b64encode(image).decode('ascii')}"
                            },
                        },
                    ]
                    values = await _model_questions(
                        settings,
                        gateway,
                        content,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        vision=True,
                    )
                    for value in values:
                        value["page_no"] = page_no
                    return values

            page_results = await asyncio.gather(
                *(recognize_page(page_no, image) for page_no, image in image_pages.items())
            )
            for values in page_results:
                questions.extend(values)
        return questions, page_count, []

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
        from eduagents_core.storage import create_storage

        data = await create_storage(settings).load(storage_key)
        gateway = gateway or get_worker_gateway(settings)
        raw, page_count, warnings = await asyncio.wait_for(
            _recognize_source(
                settings, gateway, filename, data, selected_pages, tenant_id, user_id
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
