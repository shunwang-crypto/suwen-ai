"""Shared validation and serialization helpers for structured learning resources."""

from __future__ import annotations

import hashlib
import io
import json
import math
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from .ocr_confidence import clean_field_confidence

QUESTION_TYPES = ("single", "multi", "blank", "short")
DIFFICULTIES = ("easy", "mid", "hard")
RESOURCE_TYPES = (
    "textbook",
    "lecture",
    "notes",
    "exam_paper",
    "mock_paper",
    "workbook",
    "worksheet",
    "homework",
    "single_question",
    "other",
)
QUESTION_SOURCE_SUFFIXES = {".pdf", ".docx", ".pptx", ".md", ".txt", ".png", ".jpg", ".jpeg", ".webp"}
SOURCE_MIMES = {
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
INLINE_MIMES = {
    "application/pdf",
    "text/markdown",
    "text/plain",
    "image/png",
    "image/jpeg",
    "image/webp",
}
MAGIC = {
    ".pdf": (b"%PDF",),
    ".docx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".webp": (b"RIFF",),
}

_OFFICE_MAX_MEMBERS = 5000
_OFFICE_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
_OFFICE_MAX_COMPRESSION_RATIO = 100


def _validate_office_archive(suffix: str, data: bytes) -> None:
    """Reject disguised, encrypted, or zip-bomb-like Office containers."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            names = {item.filename for item in members}
            if len(members) > _OFFICE_MAX_MEMBERS:
                raise ValueError("too many members")
            total = sum(item.file_size for item in members)
            compressed = sum(max(1, item.compress_size) for item in members)
            if total > _OFFICE_MAX_UNCOMPRESSED_BYTES:
                raise ValueError("expanded archive too large")
            if total > len(data) * _OFFICE_MAX_COMPRESSION_RATIO or total > compressed * _OFFICE_MAX_COMPRESSION_RATIO:
                raise ValueError("suspicious compression ratio")
            if any(item.flag_bits & 0x1 for item in members):
                raise ValueError("encrypted member")
            required = "word/document.xml" if suffix == ".docx" else "ppt/presentation.xml"
            if "[Content_Types].xml" not in names or required not in names:
                raise ValueError("wrong office container")
    except (ValueError, zipfile.BadZipFile, OSError):
        raise HTTPException(status_code=400, detail="UNSAFE_OFFICE_ARCHIVE") from None


def safe_filename(value: str | None) -> str:
    name = Path((value or "source").replace("\\", "/")).name
    name = name.replace("\x00", "").replace("\r", "").replace("\n", "").strip()
    return (name or "source")[:255]


def resource_storage_prefix(tenant_id: uuid.UUID | str, resource_id: uuid.UUID | str) -> str:
    return f"{tenant_id}/learning-resources/{resource_id}"


def resource_storage_key(
    tenant_id: uuid.UUID | str, resource_id: uuid.UUID | str, filename: str
) -> str:
    return f"{resource_storage_prefix(tenant_id, resource_id)}/{safe_filename(filename)}"


def validate_resource_file(filename: str, data: bytes) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    if suffix not in QUESTION_SOURCE_SUFFIXES:
        raise HTTPException(status_code=400, detail="UNSUPPORTED_RESOURCE_TYPE")
    if not data:
        raise HTTPException(status_code=400, detail="EMPTY_FILE")
    signatures = MAGIC.get(suffix)
    if signatures and not any(data.startswith(magic) for magic in signatures):
        raise HTTPException(status_code=400, detail="FILE_SIGNATURE_MISMATCH")
    if suffix == ".webp" and data[8:12] != b"WEBP":
        raise HTTPException(status_code=400, detail="FILE_SIGNATURE_MISMATCH")
    if suffix in {".docx", ".pptx"}:
        _validate_office_archive(suffix, data)
    if suffix in {".md", ".txt"}:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="FILE_SIGNATURE_MISMATCH") from None
    return "image" if suffix in {".png", ".jpg", ".jpeg", ".webp"} else "document", SOURCE_MIMES[suffix]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_source_headers(filename: str, mime_type: str) -> dict[str, str]:
    name = safe_filename(filename)
    ascii_name = "".join(
        char if 32 <= ord(char) < 127 and char not in {'"', "\\"} else "_"
        for char in name
    ).strip() or "source"
    disposition = "inline" if mime_type in INLINE_MIMES else "attachment"
    return {
        "Content-Disposition": (
            f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name, safe="")}'
        ),
        "X-Content-Type-Options": "nosniff",
    }


def parse_json_payload(text: str) -> object:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return []
        return []


def normalize_extracted_question(
    raw: object,
    ordinal: int,
    *,
    confidence_source: str = "model_reported",
    paginated: bool | None = None,
) -> dict:
    value = raw if isinstance(raw, dict) else {}
    qtype = str(value.get("type") or "short").strip()
    if qtype not in QUESTION_TYPES:
        qtype = "short"
    options = [str(x).strip()[:1000] for x in (value.get("options") or []) if str(x).strip()][:8]
    if qtype not in {"single", "multi"}:
        options = []
    confidence = value.get("confidence")
    try:
        confidence = (
            max(0.0, min(1.0, float(confidence)))
            if confidence is not None and math.isfinite(float(confidence))
            else None
        )
    except (TypeError, ValueError):
        confidence = None
    points = value.get("points")
    try:
        points = float(points) if points is not None and str(points).strip() else None
    except (TypeError, ValueError):
        points = None
    page = value.get("page_no")
    try:
        page = int(page) if page is not None and str(page).strip() else None
    except (TypeError, ValueError):
        page = None
    difficulty = str(value.get("difficulty") or "mid")
    if difficulty not in DIFFICULTIES:
        difficulty = "mid"
    kc_names = list(
        dict.fromkeys(str(x).strip()[:255] for x in (value.get("kc_names") or []) if str(x).strip())
    )[:3]
    answer = str(
        value.get("answer") or value.get("detected_reference_answer") or ""
    ).strip()[:8000]
    detected_student_answer = str(value.get("detected_student_answer") or "").strip()[:8000]
    if paginated is None:
        paginated = page is not None
    applicable_fields = ["question_no", "type", "stem", "answer"]
    if qtype in {"single", "multi"}:
        applicable_fields.append("options")
    if paginated:
        applicable_fields.append("page_no")
    if detected_student_answer or value.get("student_answer_expected") is True:
        applicable_fields.append("detected_student_answer")

    field_confidence = value.get("field_confidence") or value.get("field_confidence_json")
    if not field_confidence:
        if confidence_source == "model_reported" and confidence is not None:
            # Legacy model output has one uncalibrated score only. Preserve
            # that fact explicitly instead of promoting it to parser/OCR
            # evidence. New prompts use the per-field shape below.
            field_confidence = {
                field: {"confidence": confidence, "source": "model_reported"}
                for field in applicable_fields
            }
        elif confidence_source in {"parser", "rule", "ocr_provider"}:
            # These values are assigned by trusted server code from parsed
            # structure, never from a model-provided source claim.
            observed = {
                "question_no": True,
                "type": bool(value.get("type")),
                "stem": bool(str(value.get("stem") or "").strip()),
                "answer": bool(answer),
                "options": bool(options),
                "page_no": paginated and page is not None,
                "detected_student_answer": bool(detected_student_answer),
                "detected_reference_answer": bool(
                    str(value.get("detected_reference_answer") or "").strip()
                ),
                "explanation": bool(str(value.get("explanation") or "").strip()),
                "kc_names": bool(kc_names),
                "difficulty": value.get("difficulty") in DIFFICULTIES,
                "points": points is not None,
            }
            field_confidence = {
                field: {"confidence": 0.99, "source": confidence_source}
                for field, present in observed.items()
                if present
            }
        else:
            field_confidence = {}
    if isinstance(field_confidence, dict):
        field_confidence = {
            **field_confidence,
            "applicable_fields": applicable_fields,
        }
    return {
        "ordinal": ordinal,
        "question_no": str(value.get("question_no") or ordinal).strip()[:64],
        "page_no": page,
        "section_path": str(value.get("section_path") or "").strip()[:255],
        "source_region": value.get("source_region") if isinstance(value.get("source_region"), dict) else {},
        "type": qtype,
        "stem": str(value.get("stem") or "").strip()[:12000],
        "options": options,
        "detected_student_answer": detected_student_answer,
        "detected_reference_answer": answer,
        "explanation": str(value.get("explanation") or "").strip()[:12000],
        "points": points,
        "kc_names": kc_names,
        "difficulty": difficulty,
        "confidence": confidence,
        "field_confidence": clean_field_confidence(
            field_confidence, source_override=confidence_source
        ),
    }
