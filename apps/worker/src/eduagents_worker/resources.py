"""Background extraction for structured question resources."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import tempfile
import uuid
from pathlib import Path

from eduagents_api.db import tenant_session
from eduagents_api.models import (
    AssessmentAnswer,
    Attempt,
    ErrorItem,
    LearningResource,
    MasteryLog,
    Question,
    ResourceQuestion,
)
from eduagents_api.resource_utils import normalize_extracted_question, parse_json_payload
from eduagents_api.services import _resolve_kc_ids
from eduagents_core.config import Settings, get_settings
from eduagents_core.document_recognition import PdfRecognitionPolicy, inspect_pdf, recognize_pdf
from eduagents_core.gateway import ChatMessage, ChatRequest, Tier
from eduagents_core.logging import get_logger
from eduagents_core.parsing.registry import get_parser
from sqlalchemy import or_, select

from .enrichment import get_worker_gateway

log = get_logger("worker.resources")

_PROMPT = """你是试题资料识别助手。请忠实识别原文件中的所有题目。
每个元素必须包含：question_no, page_no, type, stem, options, detected_student_answer,
detected_reference_answer, explanation, points, kc_names, difficulty, confidence。
不清楚的字段必须是空字符串、空数组或 null，绝不能猜答案、补造题干或知识点。
type 只能是 single、multi、blank、short；difficulty 只能是 easy、mid、hard；
confidence 是 0 到 1。只输出一个 JSON 对象，格式严格为：
{"questions":[{...}]}。不要输出数组顶层、Markdown 或解释文字。"""

_IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _safe_error_code(exc: BaseException) -> str:
    """Persist only stable, user-safe identifiers, never provider details."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code and code.replace("_", "").replace("-", "").isalnum():
        return code[:64]
    return "RESOURCE_EXTRACTION_FAILED"


def _request_id(namespace: str, pages: list[int]) -> str:
    digest = hashlib.sha256(f"{namespace}\0{','.join(map(str, pages))}".encode()).hexdigest()
    return f"resx-{digest[:56]}"


def _question_response_diagnostics(response, *, page: int, json_parse_success: bool) -> dict:
    text = str(getattr(response, "text", "") or "")
    return {
        "page": page,
        "completion_tokens": int(getattr(response, "completion_tokens", 0) or 0),
        "response_length": len(text),
        "json_parse_success": json_parse_success,
        "finish_reason": getattr(response, "finish_reason", None),
        "response_hash": hashlib.sha256(text.encode()).hexdigest()[:16],
    }


def _parse_question_response(response) -> tuple[list[dict], bool]:
    raw_text = str(response.text or "").strip()
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
        )
    try:
        payload = json.loads(raw_text)
        json_parse_success = True
    except (TypeError, json.JSONDecodeError):
        payload = parse_json_payload(response.text)
        json_parse_success = isinstance(payload, (dict, list))
    if isinstance(payload, dict):
        payload = payload.get("questions") or []
    return (
        [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, list)
        else [],
        json_parse_success,
    )


def _response_is_truncated(response) -> bool:
    reason = str(getattr(response, "finish_reason", "") or "").lower()
    return reason in {"length", "max_tokens", "max_output_tokens"}


async def _set_resource_extraction_stage(
    tenant_id: str, resource_id: uuid.UUID | str, stage: str
) -> None:
    """Persist only the currently started extraction stage."""
    async with tenant_session(tenant_id) as session:
        resource = await session.get(LearningResource, uuid.UUID(str(resource_id)), with_for_update=True)
        if resource is not None:
            resource.metadata_json = {
                **(resource.metadata_json or {}),
                "extraction_stage": stage,
            }


def _numbered_blocks(text: str, max_questions: int) -> list[dict]:
    """开发/无模型时的保守文本回退，便于人工校对多题草稿。"""
    import re

    if not text.strip():
        return [
            {
                "question_no": "1",
                "stem": "请根据原始文件补充题干（开发模式未启用视觉识别）",
                "type": "short",
                "options": [],
                "detected_student_answer": "",
                "detected_reference_answer": "",
                "explanation": "",
                "points": None,
                "kc_names": [],
                "difficulty": "mid",
                "confidence": 0.0,
            }
        ]
    matches = list(re.finditer(r"(?m)^\s*(\d{1,4})\s*[.、)）:]\s*", text))
    if not matches:
        return [{"question_no": "1", "stem": text.strip()[:12000], "confidence": 0.0}]
    result: list[dict] = []
    selected_matches = matches if max_questions <= 0 else matches[:max_questions]
    for index, match in enumerate(selected_matches):
        end = (
            selected_matches[index + 1].start()
            if index + 1 < len(selected_matches)
            else len(text)
        )
        block = text[match.end() : end].strip()
        result.append(
            {
                "question_no": match.group(1),
                "page_no": None,
                "type": "short",
                "stem": block[:12000],
                "options": [],
                "detected_student_answer": "",
                "detected_reference_answer": "",
                "explanation": "",
                "points": None,
                "kc_names": [],
                "difficulty": "mid",
                "confidence": 0.0,
            }
        )
    return result


async def _parse_source(filename: str, data: bytes) -> tuple[str, dict[int, str], int]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        # 图片没有文本 parser；直接交给视觉模型。保留一个页号，方便校对界面
        # 和后续来源定位使用统一的数据形状。
        return "", {1: ""}, 1
    if suffix in {".txt", ".md"}:
        return data.decode("utf-8"), {}, 1
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        parser = get_parser(suffix)
        parsed = await asyncio.wait_for(asyncio.to_thread(parser.parse, tmp_path), timeout=120)
    finally:
        tmp_path.unlink(missing_ok=True)
    pages: dict[int, list[str]] = {}
    parts: list[str] = []
    for element in parsed.elements:
        page = element.page or 1
        pages.setdefault(page, []).append(element.text)
        parts.append(f"[第 {page} 页]\n{element.text}")
    return "\n\n".join(parts), {page: "\n".join(items) for page, items in pages.items()}, parsed.page_count or len(pages) or 1


async def _recognize(
    settings: Settings,
    gateway,
    filename: str,
    data: bytes,
    text: str,
    pages: dict[int, str],
    max_questions: int,
    tenant_id: str,
    user_id: str,
) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if settings.gateway_mode == "stub":
        return _numbered_blocks(text, max_questions)
    if suffix in _IMAGE_MIMES:
        content = [
            {"type": "text", "text": _PROMPT},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{_IMAGE_MIMES[suffix]};base64,{base64.b64encode(data).decode()}"
                },
            },
        ]
    else:
        content = f"{_PROMPT}\n\n原文（保留页码）：\n{text[:80000]}"
    result = await gateway.chat(
        ChatRequest(
            messages=(ChatMessage(role="user", content=content),),
            tier=Tier.standard,
            temperature=0.0,
            max_tokens=8000,
            json_mode=True,
            feature="resource_extract",
            tenant_id=tenant_id,
            user_id=user_id,
            model_override=(
                settings.vision_model
                if suffix in _IMAGE_MIMES and not settings.model_routing_enabled
                else None
            ),
            allow_tier_fallback=suffix not in _IMAGE_MIMES,
            credential_policy="platform" if suffix in _IMAGE_MIMES else "text",
        )
    )
    if _response_is_truncated(result):
        log.warning(
            "resource_question_response_failed",
            failure_code="STRUCTURING_OUTPUT_TRUNCATED",
            **_question_response_diagnostics(result, page=1, json_parse_success=False),
        )
        return []
    questions, json_parse_success = _parse_question_response(result)
    if not json_parse_success:
        log.warning(
            "resource_question_response_failed",
            failure_code="JSON_PARSE_FAILED",
            **_question_response_diagnostics(result, page=1, json_parse_success=False),
        )
    return questions


async def _extract_pdf_questions(
    settings: Settings,
    gateway,
    data: bytes,
    *,
    tenant_id: str,
    user_id: str,
    resource_namespace: str,
    max_questions: int,
    selected_pages: list[int] | None = None,
    stage_callback=None,
) -> tuple[list[dict], int, list[int], dict]:
    """Run shared PDF OCR first, then bounded text extraction batches.

    The visual route only transcribes pages. Structured question extraction is
    always a text request, so a vision provider/model is never sent to the text
    fallback by accident.
    """
    policy = PdfRecognitionPolicy.from_settings(
        settings,
        max_pages=settings.max_learning_resource_pages if settings.max_learning_resource_pages > 0 else None,
    )
    if stage_callback is not None:
        await stage_callback("inspecting")
    inspection = await asyncio.to_thread(
        inspect_pdf, data, policy, selected_pages=selected_pages
    )
    if stage_callback is not None and any(
        page.extraction_method in {"scanned", "mixed"} for page in inspection.pages
    ):
        await stage_callback("transcribing")
    recognized = await recognize_pdf(
        data,
        policy=policy,
        gateway=gateway,
        vision_model=(
            settings.opencode_vision_model
            if settings.model_routing_enabled
            else settings.vision_model
        ),
        tenant_id=tenant_id,
        user_id=user_id,
        selected_pages=selected_pages,
        request_namespace=resource_namespace,
        inspection=inspection,
    )
    if stage_callback is not None:
        await stage_callback("structuring")
    successful = list(recognized.successful_pages)
    failed_pages = [page.page_number for page in recognized.failed_pages]
    if not successful:
        metadata = recognized.bounded_metadata()
        metadata["failed_pages"] = failed_pages
        return [], recognized.page_count, failed_pages, metadata

    batches = [[page] for page in successful]
    batch_failures: list[int] = []
    batch_results: list[list[dict]] = [[] for _ in batches]
    semaphore = asyncio.Semaphore(2)

    async def extract_batch(index: int, batch) -> None:
        page = batch[0]
        page_number = page.page_number
        body = page.markdown
        if not body.strip():
            batch_failures.append(page_number)
            return
        if settings.gateway_mode == "stub":
            raw = _numbered_blocks(body, 0)
        else:
            prompt = f"{_PROMPT}\n\n当前真实 PDF 页码：{page_number}\n原文：\n{body[:24000]}"
            try:
                async with semaphore:
                    response = await gateway.chat(
                        ChatRequest(
                            messages=(ChatMessage(role="user", content=prompt),),
                            tier=Tier.standard,
                            temperature=0.0,
                            max_tokens=settings.pdf_question_structuring_max_tokens,
                            json_mode=True,
                            feature="resource_extract",
                            request_id=_request_id(resource_namespace, [page_number]),
                            tenant_id=tenant_id,
                            user_id=user_id,
                            allow_tier_fallback=True,
                            credential_policy="text",
                        )
                    )
                raw, json_parse_success = _parse_question_response(response)
                if _response_is_truncated(response):
                    diagnostics = _question_response_diagnostics(
                        response, page=page_number, json_parse_success=json_parse_success
                    )
                    log.warning(
                        "resource_pdf_question_page_failed",
                        **diagnostics,
                        failure_code="STRUCTURING_OUTPUT_TRUNCATED",
                    )
                    batch_failures.append(page_number)
                    return
                valid_raw = [item for item in raw if str(item.get("stem") or "").strip()]
                if not valid_raw:
                    diagnostics = _question_response_diagnostics(
                        response, page=page_number, json_parse_success=json_parse_success
                    )
                    log.warning(
                        "resource_pdf_question_page_empty",
                        **diagnostics,
                        failure_code="JSON_PARSE_FAILED" if not json_parse_success else "NO_VALID_QUESTIONS",
                    )
                    batch_failures.append(page_number)
                    return
                raw = valid_raw
            except Exception as exc:  # noqa: BLE001 - preserve the other pages
                batch_failures.append(page_number)
                log.warning(
                    "resource_pdf_question_page_failed",
                    page=page_number,
                    failure_code=_safe_error_code(exc),
                )
                return
        page_has_error = False
        for item in raw:
            page_value = item.get("page_no")
            if page_value is None or not str(page_value).strip():
                item["page_no"] = page_number
                continue
            try:
                normalized_page = int(page_value)
            except (TypeError, ValueError):
                page_has_error = True
                continue
            if normalized_page != page_number:
                page_has_error = True
                continue
            item["page_no"] = normalized_page
        if page_has_error:
            batch_failures.append(page_number)
            log.warning(
                "resource_pdf_question_page_failed",
                page=page_number,
                failure_code="QUESTION_PAGE_MISMATCH",
            )
            return
        for item in raw:
            if item.get("page_no") != page_number:
                item["_wrong_page"] = True
        raw = [item for item in raw if not item.get("_wrong_page")]
        for item in raw:
            item.pop("_wrong_page", None)
        if not raw:
            batch_failures.append(page_number)
            log.warning(
                "resource_pdf_question_page_empty",
                page=page_number,
                failure_code="NO_CURRENT_PAGE_QUESTIONS",
            )
            return
        batch_results[index] = raw

    await asyncio.gather(*(extract_batch(index, batch) for index, batch in enumerate(batches)))
    raw_questions = [item for batch in batch_results for item in batch]
    unique_questions: list[dict] = []
    seen: set[tuple[str, int | None]] = set()
    for item in raw_questions:
        stem = str(item.get("stem") or "").strip()
        page = item.get("page_no")
        try:
            page = int(page) if page is not None else None
        except (TypeError, ValueError):
            page = None
        signature = (stem, page)
        if stem and signature in seen:
            continue
        if stem:
            seen.add(signature)
        unique_questions.append(item)
    raw_questions = unique_questions
    warnings = list(recognized.warnings)
    failed_pages = sorted(set(failed_pages + batch_failures))
    if failed_pages:
        warnings.append(f"PDF_PARTIAL_PAGE_FAILURE:{','.join(map(str, failed_pages))}")
    metadata = recognized.bounded_metadata()
    metadata["failed_pages"] = failed_pages
    metadata["warnings"] = warnings[:20]
    return raw_questions, recognized.page_count, failed_pages, metadata


async def _has_question_evidence(session, question_id: uuid.UUID, resource_question_id: uuid.UUID) -> bool:
    return any(
        (
            await session.scalar(
                select(ErrorItem.id)
                .where(
                    or_(
                        ErrorItem.question_id == question_id,
                        ErrorItem.resource_question_id == resource_question_id,
                    )
                )
                .limit(1)
            ),
            await session.scalar(select(Attempt.id).where(Attempt.question_id == question_id).limit(1)),
            await session.scalar(
                select(AssessmentAnswer.id)
                .where(AssessmentAnswer.resource_question_id == resource_question_id)
                .limit(1)
            ),
            await session.scalar(
                select(MasteryLog.id)
                .where(MasteryLog.evidence_id.in_((question_id, resource_question_id)))
                .limit(1)
            ),
        )
    )


def _pending_question_is_replaceable(
    resource_question,
    *,
    has_evidence: bool,
    retry_pages: set[int],
) -> bool:
    """Limit destructive replacement to the current recognition scope.

    An empty ``retry_pages`` set means an intentional full recognition run.
    A non-empty set is a partial retry, so pending questions from every other
    page remain part of the review draft even when they have no evidence yet.
    """
    if resource_question.verification_status != "pending" or has_evidence:
        return False
    return not retry_pages or resource_question.page_no in retry_pages


def _filter_questions_to_retry_pages(questions: list[dict], retry_pages: list[int]) -> list[dict]:
    if not retry_pages:
        return questions
    retry_scope = set(retry_pages)
    return [question for question in questions if question.get("page_no") in retry_scope]


def _failed_pages_after_empty_retry(
    failed_pages: list[int], retry_pages: list[int], questions: list[dict]
) -> list[int]:
    if retry_pages and not questions:
        return sorted(set(failed_pages).union(retry_pages))
    return failed_pages


async def extract_learning_resource(
    ctx: dict,
    tenant_id: str,
    resource_id: str,
    gateway=None,
    settings: Settings | None = None,
) -> str:
    settings = settings or ctx.get("settings") or get_settings()
    rid = uuid.UUID(resource_id)
    async with tenant_session(tenant_id) as session:
        resource = await session.get(LearningResource, rid)
        if resource is None or not resource.storage_key or not resource.source_filename:
            return "missing"
        filename, storage_key, owner_id = (
            resource.source_filename,
            resource.storage_key,
            resource.owner_id,
        )
        retry_pages = [int(page) for page in (resource.metadata_json or {}).get("failed_pages", [])]
    from eduagents_core.storage import create_storage

    recognition_meta: dict = {}
    failed_pages: list[int] = []
    try:
        data = await create_storage(settings).load(storage_key)
        suffix = Path(filename).suffix.lower()
        if (
            settings.max_learning_resource_pages > 0
            and suffix != ".pdf"
        ):
            _text, _pages, page_count = await _parse_source(filename, data)
            if page_count > settings.max_learning_resource_pages:
                raise ValueError("RESOURCE_PAGE_LIMIT_EXCEEDED")
        elif suffix == ".pdf":
            page_count = 0
        else:
            page_count = 1
        if suffix == ".pdf":
            raw_questions, page_count, failed_pages, recognition_meta = await _extract_pdf_questions(
                settings,
                gateway or get_worker_gateway(settings),
                data,
                tenant_id=tenant_id,
                user_id=str(owner_id),
                resource_namespace=f"resource:{rid}:{resource.sha256 or hashlib.sha256(data).hexdigest()}",
                max_questions=settings.max_learning_resource_questions,
                selected_pages=retry_pages or None,
                stage_callback=lambda stage: _set_resource_extraction_stage(tenant_id, rid, stage),
            )
            text = ""
            pages = {}
        else:
            await _set_resource_extraction_stage(tenant_id, rid, "inspecting")
            text, pages, page_count = await _parse_source(filename, data)
            await _set_resource_extraction_stage(tenant_id, rid, "structuring")
            raw_questions = await _recognize(
                settings,
                gateway or get_worker_gateway(settings),
                filename,
                data,
                text,
                pages,
                settings.max_learning_resource_questions,
                tenant_id,
                str(owner_id),
            )
            failed_pages = []
            recognition_meta = {"page_count": page_count, "failed_pages": [], "warnings": []}
        if (
            settings.max_learning_resource_pages > 0
            and page_count > settings.max_learning_resource_pages
        ):
            raise ValueError("RESOURCE_PAGE_LIMIT_EXCEEDED")
        if not raw_questions:
            failed_pages = _failed_pages_after_empty_retry(failed_pages, retry_pages, [])
            recognition_meta["failed_pages"] = failed_pages
            async with tenant_session(tenant_id) as session:
                resource = await session.get(LearningResource, rid, with_for_update=True)
                existing = await session.scalar(
                    select(ResourceQuestion.id).where(ResourceQuestion.resource_id == rid).limit(1)
                )
                if resource is not None and existing is not None:
                    resource.metadata_json = {
                        **(resource.metadata_json or {}),
                        "extraction_stage": "reviewing",
                    }
                    resource.status = "reviewing"
                    resource.page_count = page_count
                    resource.metadata_json = {
                        **(resource.metadata_json or {}),
                        "recognition": recognition_meta,
                        "failed_pages": failed_pages,
                        "error": "NO_QUESTIONS_RECOGNIZED",
                    }
                    return "reviewing"
            raise ValueError("NO_QUESTIONS_RECOGNIZED")
        truncated = (
            settings.max_learning_resource_questions > 0
            and len(raw_questions) > settings.max_learning_resource_questions
        )
        if settings.max_learning_resource_questions > 0:
            raw_questions = raw_questions[: settings.max_learning_resource_questions]
        questions = [normalize_extracted_question(raw, i + 1) for i, raw in enumerate(raw_questions)]
        questions = [question for question in questions if question["stem"]]
        questions = _filter_questions_to_retry_pages(questions, retry_pages)
        if not questions:
            failed_pages = _failed_pages_after_empty_retry(failed_pages, retry_pages, questions)
            recognition_meta["failed_pages"] = failed_pages
            async with tenant_session(tenant_id) as session:
                resource = await session.get(LearningResource, rid, with_for_update=True)
                existing = await session.scalar(
                    select(ResourceQuestion.id).where(ResourceQuestion.resource_id == rid).limit(1)
                )
                if resource is not None and existing is not None:
                    resource.status = "reviewing"
                    resource.page_count = page_count
                    resource.metadata_json = {
                        **(resource.metadata_json or {}),
                        "recognition": recognition_meta,
                        "failed_pages": failed_pages,
                        "error": "NO_QUESTIONS_RECOGNIZED",
                    }
                    return "reviewing"
            raise ValueError("NO_QUESTIONS_RECOGNIZED")
        await _set_resource_extraction_stage(tenant_id, rid, "reviewing")
        async with tenant_session(tenant_id) as session:
            resource = await session.get(LearningResource, rid, with_for_update=True)
            if resource is None:
                return "missing"
            resource.page_count = page_count
            resource.status = "reviewing"
            resource.metadata_json = {
                **(resource.metadata_json or {}),
                "extraction_text_pages": sorted(pages),
                "question_count": len(questions),
                "recognition": recognition_meta,
                "failed_pages": failed_pages,
                "warnings": [
                    *(recognition_meta.get("warnings") or []),
                    *( ["RESOURCE_QUESTION_LIMIT_EXCEEDED"] if truncated else []),
                ][:20],
            }
            existing_rows = (
                await session.execute(
                    select(ResourceQuestion, Question)
                    .join(Question, Question.id == ResourceQuestion.question_id)
                    .where(ResourceQuestion.resource_id == rid)
                    .with_for_update()
                )
            ).all()
            protected = []
            replaceable = []
            retry_scope = set(retry_pages)
            for resource_question, question in existing_rows:
                has_evidence = await _has_question_evidence(
                    session, question.id, resource_question.id
                )
                if _pending_question_is_replaceable(
                    resource_question,
                    has_evidence=has_evidence,
                    retry_pages=retry_scope,
                ):
                    replaceable.append((resource_question, question))
                else:
                    protected.append((resource_question, question))
            for resource_question, question in replaceable:
                await session.delete(resource_question)
                await session.flush()
                await session.delete(question)
            existing_signatures = {
                (question.stem.strip(), resource_question.page_no)
                for resource_question, question in protected
            }
            questions = [
                question
                for question in questions
                if (question["stem"].strip(), question["page_no"]) not in existing_signatures
            ]
            ordinal_start = max((row.ordinal for row, _question in protected), default=-1) + 1
            for ordinal, question_data in enumerate(questions, start=ordinal_start):
                kc_ids = await _resolve_kc_ids(
                    session,
                    str(resource.linked_kb_id) if resource.linked_kb_id else None,
                    question_data["kc_names"],
                    user_id=owner_id,
                )
                question = Question(
                    tenant_id=resource.tenant_id,
                    quiz_id=None,
                    type=question_data["type"],
                    stem=question_data["stem"],
                    options_json=question_data["options"],
                    answer=question_data["detected_reference_answer"],
                    explanation=question_data["explanation"],
                    kc_ids=kc_ids,
                    source_type="image" if Path(filename).suffix.lower() in _IMAGE_MIMES else "document",
                    source_meta_json={
                        "resource_id": str(rid),
                        "question_no": question_data["question_no"],
                        "page_no": question_data["page_no"],
                        "detected_student_answer": question_data["detected_student_answer"],
                        "detected_reference_answer": question_data["detected_reference_answer"],
                        "kc_names": question_data["kc_names"],
                    },
                    difficulty=question_data["difficulty"],
                )
                session.add(question)
                await session.flush()
                session.add(
                    ResourceQuestion(
                        tenant_id=resource.tenant_id,
                        resource_id=rid,
                        question_id=question.id,
                        ordinal=ordinal,
                        question_no=question_data["question_no"],
                        section_path=question_data["section_path"],
                        page_no=question_data["page_no"],
                        source_region_json=question_data["source_region"],
                        points=question_data["points"],
                        extraction_confidence=question_data["confidence"],
                        verification_status="pending",
                    )
                )
        log.info("resource_extraction_done", resource_id=resource_id, questions=len(questions))
        return "reviewing"
    except Exception as exc:  # noqa: BLE001 - preserve source and expose retryable failure
        async with tenant_session(tenant_id) as session:
            resource = await session.get(LearningResource, rid)
            if resource is not None:
                resource.status = "failed"
                if recognition_meta.get("page_count"):
                    resource.page_count = int(recognition_meta["page_count"])
                resource.metadata_json = {
                    **(resource.metadata_json or {}),
                    "error": _safe_error_code(exc),
                    **({"recognition": recognition_meta} if recognition_meta else {}),
                    **({"failed_pages": failed_pages} if failed_pages else {}),
                }
        log.error(
            "resource_extraction_failed",
            resource_id=resource_id,
            error_code=_safe_error_code(exc),
        )
        return "failed"
