from __future__ import annotations

import asyncio
import hashlib
import math
import unicodedata
from collections import Counter
from collections.abc import Iterable

import pymupdf

from .markdown import normalize_page_markdown
from .types import (
    PdfExtractionMethod,
    PdfInspection,
    PdfPageInspection,
    PdfPageResult,
    PdfRecognitionPolicy,
    PdfRecognitionResult,
)
from .vision import recognize_page_image


def _safe_error_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "PDF_PAGE_RECOGNITION_TIMEOUT"
    if isinstance(exc, ValueError) and str(exc).startswith("PDF_"):
        return str(exc)[:64]
    return "PDF_PAGE_RECOGNITION_FAILED"


def _visible_chars(text: str) -> list[str]:
    return [char for char in text if not char.isspace()]


def _printable_ratio(chars: list[str]) -> float:
    if not chars:
        return 0.0
    printable = sum(
        1
        for char in chars
        if char.isprintable() and not unicodedata.category(char).startswith("C")
    )
    ratio = printable / len(chars)
    # A nominally printable text layer made almost entirely of one replacement
    # glyph (commonly '?' from a missing embedded font) is still unusable.
    if len(chars) >= 30:
        dominant_ratio = Counter(chars).most_common(1)[0][1] / len(chars)
        if dominant_ratio > 0.35:
            ratio *= max(0.0, 1.0 - (dominant_ratio - 0.35) / 0.65)
    return ratio


def _classify_page(
    *,
    char_count: int,
    printable_ratio: float,
    text_coverage: float,
    body_char_ratio: float,
    image_count: int,
    image_area_ratio: float,
    drawing_count: int,
    policy: PdfRecognitionPolicy,
) -> tuple[PdfExtractionMethod, str | None]:
    has_visual_body = (
        image_area_ratio >= policy.mixed_image_area_ratio
        or image_count >= policy.mixed_image_count
        or drawing_count >= 20
    )
    if char_count == 0:
        if image_count or drawing_count:
            return "scanned", None
        return "failed", "PDF_BLANK_PAGE"
    text_quality_is_trustworthy = (
        printable_ratio >= policy.native_min_printable_ratio
        and text_coverage >= policy.native_min_text_coverage
        and body_char_ratio >= policy.body_min_char_ratio
    )
    text_is_complete = char_count >= policy.native_min_chars and text_quality_is_trustworthy
    if text_is_complete and not has_visual_body:
        return "native_text", None
    # Short exercise pages can be entirely text while legitimately containing
    # fewer characters than a prose page.  Only accept the lower threshold
    # when there are no image/drawing body signals and layout quality is strong.
    if (
        not image_count
        and not drawing_count
        and char_count >= policy.native_no_visual_min_chars
        and text_quality_is_trustworthy
    ):
        return "native_text", None
    # A small title/footer text layer is not evidence that the visual body was
    # extracted.  Uncertain pages deliberately take the conservative path.
    return "mixed", "PDF_NATIVE_TEXT_INCOMPLETE"


def _selected_page_numbers(page_count: int, selected_pages: Iterable[int] | None) -> list[int]:
    values = list(range(1, page_count + 1)) if selected_pages is None else list(selected_pages)
    if not values or len(values) != len(set(values)):
        raise ValueError("PDF_INVALID_PAGE_SELECTION")
    if any(page < 1 or page > page_count for page in values):
        raise ValueError("PDF_INVALID_PAGE_SELECTION")
    return sorted(values)


def inspect_pdf(
    data: bytes,
    policy: PdfRecognitionPolicy,
    *,
    selected_pages: Iterable[int] | None = None,
) -> PdfInspection:
    """Inspect selected PDF pages using text, layout, image and drawing signals."""
    if not data.startswith(b"%PDF-"):
        raise ValueError("PDF_FILE_TYPE_MISMATCH")
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("PDF_DAMAGED") from exc
    try:
        if document.needs_pass:
            raise ValueError("PDF_PASSWORD_PROTECTED")
        page_count = document.page_count
        if page_count < 1:
            raise ValueError("PDF_EMPTY")
        if policy.max_pages > 0 and page_count > policy.max_pages:
            raise ValueError("PDF_PAGE_LIMIT_EXCEEDED")
        wanted = _selected_page_numbers(page_count, selected_pages)
        pages: list[PdfPageInspection] = []
        for page_number in wanted:
            page = document.load_page(page_number - 1)
            page_area = max(float(page.rect.width * page.rect.height), 1.0)
            blocks = page.get_text("blocks") or []
            text = page.get_text("text").strip()
            chars = _visible_chars(text)
            body_chars = 0
            covered_area = 0.0
            for block in blocks:
                if len(block) < 5 or not str(block[4]).strip():
                    continue
                rect = pymupdf.Rect(block[:4]) & page.rect
                covered_area += max(0.0, float(rect.get_area()))
                center_y = (rect.y0 + rect.y1) / 2
                if page.rect.y0 + page.rect.height * 0.12 <= center_y <= page.rect.y1 - page.rect.height * 0.12:
                    body_chars += len(_visible_chars(str(block[4])))
            image_count = 0
            image_area = 0.0
            seen_xrefs: set[int] = set()
            for image in page.get_images(full=True):
                xref = int(image[0])
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                rects = page.get_image_rects(xref)
                if rects:
                    image_count += len(rects)
                    image_area += sum(max(0.0, float((rect & page.rect).get_area())) for rect in rects)
            try:
                drawing_count = len(page.get_drawings())
            except Exception:
                drawing_count = 0
            printable = _printable_ratio(chars)
            text_coverage = min(1.0, covered_area / page_area)
            body_ratio = body_chars / len(chars) if chars else 0.0
            image_ratio = min(1.0, image_area / page_area)
            method, warning = _classify_page(
                char_count=len(chars),
                printable_ratio=printable,
                text_coverage=text_coverage,
                body_char_ratio=body_ratio,
                image_count=image_count,
                image_area_ratio=image_ratio,
                drawing_count=drawing_count,
                policy=policy,
            )
            pages.append(
                PdfPageInspection(
                    page_number=page_number,
                    extraction_method=method,
                    native_text=text,
                    native_text_chars=len(chars),
                    printable_ratio=printable,
                    text_coverage=text_coverage,
                    body_char_ratio=body_ratio,
                    image_count=image_count,
                    image_area_ratio=image_ratio,
                    drawing_count=drawing_count,
                    warning_code=warning,
                )
            )
        return PdfInspection(page_count=page_count, pages=tuple(pages))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF_INSPECTION_FAILED") from exc
    finally:
        document.close()


def render_pdf_page(data: bytes, page_number: int, policy: PdfRecognitionPolicy) -> bytes:
    """Render one page while bounding pixels and encoded image bytes."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise ValueError("PDF_PASSWORD_PROTECTED")
            if page_number < 1 or page_number > document.page_count:
                raise ValueError("PDF_INVALID_PAGE_SELECTION")
            page = document.load_page(page_number - 1)
            dpi = max(72, policy.render_dpi)
            target_pixels = page.rect.width * dpi / 72 * page.rect.height * dpi / 72
            if policy.max_render_pixels > 0 and target_pixels > policy.max_render_pixels:
                dpi = max(72, int(dpi * math.sqrt(policy.max_render_pixels / target_pixels)))
            pixmap = page.get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csRGB)
            if policy.max_render_pixels > 0 and pixmap.width * pixmap.height > policy.max_render_pixels:
                raise ValueError("PDF_RENDER_PIXEL_LIMIT_EXCEEDED")
            encoded = pixmap.tobytes("png")
            if policy.max_render_bytes > 0 and len(encoded) > policy.max_render_bytes and dpi > 96:
                pixmap = page.get_pixmap(dpi=96, alpha=False, colorspace=pymupdf.csRGB)
                encoded = pixmap.tobytes("png")
            if policy.max_render_bytes > 0 and len(encoded) > policy.max_render_bytes:
                raise ValueError("PDF_RENDER_SIZE_LIMIT_EXCEEDED")
            return encoded
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF_RENDER_FAILED") from exc


async def recognize_pdf(
    data: bytes,
    *,
    policy: PdfRecognitionPolicy,
    gateway,
    vision_model: str,
    tenant_id: str,
    user_id: str,
    selected_pages: Iterable[int] | None = None,
    inspection: PdfInspection | None = None,
    request_namespace: str = "",
) -> PdfRecognitionResult:
    """Recognize selected pages with bounded, partial-failure semantics."""
    if inspection is None:
        inspection = await asyncio.to_thread(
            inspect_pdf, data, policy, selected_pages=selected_pages
        )
    visual_pages = [
        page for page in inspection.pages if page.extraction_method in {"scanned", "mixed"}
    ]
    if policy.max_vision_pages > 0 and len(visual_pages) > policy.max_vision_pages:
        raise ValueError("PDF_VISION_PAGE_LIMIT_EXCEEDED")
    semaphore = asyncio.Semaphore(max(1, policy.vision_concurrency))
    namespace = request_namespace or hashlib.sha256(data).hexdigest()

    def page_request_id(page_number: int) -> str:
        digest = hashlib.sha256(
            f"{tenant_id}\0{user_id}\0{namespace}\0{page_number}".encode()
        ).hexdigest()
        return f"pdfocr-{digest[:56]}"

    async def one(page: PdfPageInspection) -> PdfPageResult:
        if page.extraction_method == "native_text":
            markdown = normalize_page_markdown(
                page.native_text, page.page_number, page.extraction_method
            )
            return PdfPageResult(
                page_number=page.page_number,
                extraction_method=page.extraction_method,
                recognition_status="ready" if markdown else "failed",
                markdown=markdown,
                native_text_chars=page.native_text_chars,
                image_count=page.image_count,
                warning_code=page.warning_code,
                error_code=None if markdown else "PDF_PAGE_EMPTY",
            )
        if page.extraction_method == "failed":
            return PdfPageResult(
                page_number=page.page_number,
                extraction_method="failed",
                recognition_status="failed",
                native_text_chars=page.native_text_chars,
                image_count=page.image_count,
                warning_code=page.warning_code,
                error_code=page.warning_code or "PDF_PAGE_EMPTY",
            )
        try:
            async with semaphore:
                image = await asyncio.to_thread(render_pdf_page, data, page.page_number, policy)
                markdown = await recognize_page_image(
                    gateway,
                    image,
                    page_number=page.page_number,
                    extraction_method=page.extraction_method,
                    vision_model=vision_model,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    timeout_seconds=policy.page_timeout_seconds,
                    request_id=page_request_id(page.page_number),
                )
            if not markdown:
                raise ValueError("PDF_PAGE_EMPTY")
            return PdfPageResult(
                page_number=page.page_number,
                extraction_method=page.extraction_method,
                recognition_status="ready",
                markdown=markdown,
                native_text_chars=page.native_text_chars,
                image_count=page.image_count,
                vision_used=True,
                warning_code=page.warning_code,
            )
        except Exception as exc:
            return PdfPageResult(
                page_number=page.page_number,
                extraction_method=page.extraction_method,
                recognition_status="failed",
                native_text_chars=page.native_text_chars,
                image_count=page.image_count,
                vision_used=True,
                warning_code=page.warning_code,
                error_code=_safe_error_code(exc),
            )

    async def run() -> PdfRecognitionResult:
        pages = tuple(await asyncio.gather(*(one(page) for page in inspection.pages)))
        failed = [str(page.page_number) for page in pages if page.recognition_status == "failed"]
        warnings = (f"PDF_PARTIAL_PAGE_FAILURE:{','.join(failed)}",) if failed else ()
        return PdfRecognitionResult(
            page_count=inspection.page_count,
            pages=pages,
            warnings=warnings,
        )

    try:
        return await asyncio.wait_for(run(), timeout=policy.document_timeout_seconds)
    except TimeoutError as exc:
        raise ValueError("PDF_DOCUMENT_RECOGNITION_TIMEOUT") from exc
