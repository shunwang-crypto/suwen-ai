"""Background extraction for structured question resources."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import uuid
from pathlib import Path

from eduagents_api.db import tenant_session
from eduagents_api.models import LearningResource, Question, ResourceQuestion
from eduagents_api.resource_utils import normalize_extracted_question, parse_json_payload
from eduagents_api.services import _resolve_kc_ids
from eduagents_core.config import Settings, get_settings
from eduagents_core.gateway import ChatMessage, ChatRequest, Tier
from eduagents_core.logging import get_logger
from eduagents_core.parsing.registry import get_parser
from sqlalchemy import func, select

from .enrichment import get_worker_gateway

log = get_logger("worker.resources")

_PROMPT = """你是试题资料识别助手。请忠实识别原文件中的所有题目，返回 JSON 数组，不要只返回一道题。
每个元素必须包含：question_no, page_no, type, stem, options, detected_student_answer,
detected_reference_answer, explanation, points, kc_names, difficulty, confidence。
不清楚的字段必须是空字符串、空数组或 null，绝不能猜答案、补造题干或知识点。
type 只能是 single、multi、blank、short；difficulty 只能是 easy、mid、hard；
confidence 是 0 到 1。只输出 JSON 数组。"""

_IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
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
    payload = parse_json_payload(result.text)
    if isinstance(payload, dict):
        payload = payload.get("questions") or []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


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
        existing = int(
            await session.scalar(
                select(func.count()).select_from(ResourceQuestion).where(ResourceQuestion.resource_id == rid)
            )
            or 0
        )
        if existing:
            resource.status = "reviewing"
            return "already-extracted"
    from eduagents_core.storage import create_storage

    try:
        data = await create_storage(settings).load(storage_key)
        text, pages, page_count = await _parse_source(filename, data)
        if (
            settings.max_learning_resource_pages > 0
            and page_count > settings.max_learning_resource_pages
        ):
            raise ValueError("RESOURCE_PAGE_LIMIT_EXCEEDED")
        gateway = gateway or get_worker_gateway(settings)
        raw_questions = await _recognize(
            settings,
            gateway,
            filename,
            data,
            text,
            pages,
            settings.max_learning_resource_questions,
            tenant_id,
            str(owner_id),
        )
        if not raw_questions:
            raise ValueError("NO_QUESTIONS_RECOGNIZED")
        truncated = (
            settings.max_learning_resource_questions > 0
            and len(raw_questions) > settings.max_learning_resource_questions
        )
        if settings.max_learning_resource_questions > 0:
            raw_questions = raw_questions[: settings.max_learning_resource_questions]
        questions = [normalize_extracted_question(raw, i + 1) for i, raw in enumerate(raw_questions)]
        questions = [question for question in questions if question["stem"]]
        if not questions:
            raise ValueError("NO_QUESTIONS_RECOGNIZED")
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
                **(
                    {"warnings": ["RESOURCE_QUESTION_LIMIT_EXCEEDED"]}
                    if truncated
                    else {}
                ),
            }
            for ordinal, question_data in enumerate(questions):
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
                resource.metadata_json = {**(resource.metadata_json or {}), "error": str(exc)[:500]}
        log.exception("resource_extraction_failed", resource_id=resource_id)
        return "failed"
