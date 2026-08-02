from __future__ import annotations

import asyncio
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eduagents_core.parsing.base import ParsedDocument, ParseError
from eduagents_core.parsing.registry import get_parser
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy import delete, func, select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..models import ChatAttachment
from ..schemas import ChatAttachmentOut

router = APIRouter(prefix="/api/chat/attachments", tags=["chat"])

_DOCUMENT_SUFFIXES = {".pdf", ".docx", ".pptx", ".md", ".txt"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_MIME_TYPES = {
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
_MAGIC = {
    ".pdf": (b"%PDF",),
    ".docx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".webp": (b"RIFF",),
}


def _safe_filename(value: str | None) -> str:
    name = Path((value or "attachment").replace("\\", "/")).name.strip().replace("\x00", "")
    name = name.replace("\r", "").replace("\n", "")
    return (name or "attachment")[:255]


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    data = bytearray()
    while True:
        part = await file.read(1024 * 1024)
        if not part:
            break
        data.extend(part)
        if len(data) > max_bytes:
            raise HTTPException(status_code=413, detail="CHAT_ATTACHMENT_TOO_LARGE")
    if not data:
        raise HTTPException(status_code=400, detail="EMPTY_FILE")
    return bytes(data)


def _validate_file(filename: str, data: bytes, *, vision_enabled: bool) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        if not vision_enabled:
            raise HTTPException(status_code=503, detail="VISION_MODEL_UNAVAILABLE")
        kind = "image"
    elif suffix in _DOCUMENT_SUFFIXES:
        kind = "document"
    else:
        raise HTTPException(status_code=400, detail="UNSUPPORTED_CHAT_ATTACHMENT_TYPE")
    signatures = _MAGIC.get(suffix)
    if signatures and not any(data.startswith(magic) for magic in signatures):
        raise HTTPException(status_code=400, detail="FILE_SIGNATURE_MISMATCH")
    if suffix == ".webp" and data[8:12] != b"WEBP":
        raise HTTPException(status_code=400, detail="FILE_SIGNATURE_MISMATCH")
    if suffix in {".md", ".txt"}:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="FILE_SIGNATURE_MISMATCH") from None
    return kind, _MIME_TYPES[suffix]


def _render_document(parsed: ParsedDocument) -> str:
    parts: list[str] = []
    current_page: int | None = None
    for element in parsed.elements:
        if element.page is not None and element.page != current_page:
            current_page = element.page
            parts.append(f"\n[第 {current_page} 页]\n")
        if element.type == "heading":
            parts.append(f"{'#' * max(1, min(element.level or 2, 6))} {element.text}")
        else:
            parts.append(element.text)
    return "\n\n".join(part for part in parts if part.strip()).strip()


async def _extract_document(filename: str, data: bytes, max_chars: int) -> str:
    suffix = Path(filename).suffix.lower()
    parser = get_parser(suffix)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        parsed = await asyncio.wait_for(asyncio.to_thread(parser.parse, tmp_path), timeout=120)
    except TimeoutError:
        raise HTTPException(status_code=408, detail="CHAT_ATTACHMENT_PARSE_TIMEOUT") from None
    except ParseError as exc:
        raise HTTPException(status_code=422, detail="CHAT_ATTACHMENT_PARSE_FAILED") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    text = _render_document(parsed)
    if not text:
        raise HTTPException(status_code=422, detail="CHAT_ATTACHMENT_EMPTY_TEXT")
    if len(text) > max_chars:
        raise HTTPException(status_code=413, detail="CHAT_ATTACHMENT_TEXT_TOO_LARGE")
    return text


def _out(row: ChatAttachment) -> ChatAttachmentOut:
    return ChatAttachmentOut(
        id=row.id,
        filename=row.filename,
        mime_type=row.mime_type,
        kind=row.kind,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
    )


@router.get("/capabilities")
async def attachment_capabilities(settings=Depends(get_app_settings)):
    return {
        "max_file_mb": settings.max_chat_attachment_mb,
        "max_files_per_message": settings.max_chat_attachments_per_message,
        "document_suffixes": sorted(_DOCUMENT_SUFFIXES),
        "image_suffixes": sorted(_IMAGE_SUFFIXES) if settings.routing_vision_enabled else [],
        "vision_enabled": settings.routing_vision_enabled,
        "thinking_enabled": bool(settings.strong_api_key and settings.strong_model),
        "web_search_enabled": bool(settings.tavily_api_key),
    }


@router.post("", response_model=ChatAttachmentOut, status_code=201)
async def upload_chat_attachment(
    file: UploadFile,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    filename = _safe_filename(file.filename)
    data = await _read_capped(file, settings.max_chat_attachment_mb * 1024 * 1024)
    kind, mime_type = _validate_file(
        filename, data, vision_enabled=settings.routing_vision_enabled
    )

    # Bound rows are lifecycle-owned by their message. Cap unbound uploads to
    # prevent abandoned composer selections from becoming unlimited storage.
    stale_ids: list[uuid.UUID] = []
    async with tenant_session(ctx.tenant_id) as session:
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        stale_ids = list(
            (
                await session.scalars(
                    select(ChatAttachment.id).where(
                        ChatAttachment.user_id == ctx.user_id,
                        ChatAttachment.message_id.is_(None),
                        ChatAttachment.created_at < cutoff,
                    )
                )
            ).all()
        )
        if stale_ids:
            await session.execute(
                delete(ChatAttachment).where(ChatAttachment.id.in_(stale_ids))
            )
        pending = await session.scalar(
            select(func.count())
            .select_from(ChatAttachment)
            .where(
                ChatAttachment.user_id == ctx.user_id,
                ChatAttachment.message_id.is_(None),
            )
        )
        if (pending or 0) >= 20:
            raise HTTPException(status_code=429, detail="TOO_MANY_PENDING_ATTACHMENTS")
    for stale_id in stale_ids:
        await request.app.state.storage.delete_prefix(
            f"{ctx.tenant_id}/chat-attachments/{stale_id}"
        )

    extracted_text = None
    if kind == "document":
        extracted_text = await _extract_document(
            filename, data, settings.max_chat_attachment_text_chars
        )

    attachment_id = uuid.uuid4()
    storage_prefix = f"{ctx.tenant_id}/chat-attachments/{attachment_id}"
    storage_key = f"{storage_prefix}/{filename}"
    await request.app.state.storage.save(storage_key, data)
    try:
        async with tenant_session(ctx.tenant_id) as session:
            row = ChatAttachment(
                id=attachment_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                filename=filename,
                mime_type=mime_type,
                kind=kind,
                size_bytes=len(data),
                storage_key=storage_key,
                extracted_text=extracted_text,
            )
            session.add(row)
            await session.flush()
            return _out(row)
    except BaseException:
        await request.app.state.storage.delete_prefix(storage_prefix)
        raise


@router.delete("/{attachment_id}", status_code=204)
async def delete_chat_attachment(
    attachment_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id) as session:
        row = await session.get(ChatAttachment, attachment_id)
        if row is None or row.user_id != ctx.user_id or row.message_id is not None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        storage_prefix = f"{ctx.tenant_id}/chat-attachments/{row.id}"
        await request.app.state.storage.delete_prefix(storage_prefix)
        await session.execute(delete(ChatAttachment).where(ChatAttachment.id == row.id))
