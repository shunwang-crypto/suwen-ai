from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PdfExtractionMethod = Literal["native_text", "scanned", "mixed", "failed"]
PdfRecognitionStatus = Literal["ready", "failed"]


@dataclass(frozen=True)
class PdfRecognitionPolicy:
    """Central limits and conservative page-classification thresholds."""

    max_pages: int = 100
    native_min_chars: int = 180
    native_no_visual_min_chars: int = 60
    native_min_printable_ratio: float = 0.80
    native_min_text_coverage: float = 0.015
    body_min_char_ratio: float = 0.55
    mixed_image_area_ratio: float = 0.18
    mixed_image_count: int = 3
    render_dpi: int = 144
    max_render_pixels: int = 12_000_000
    max_render_bytes: int = 6 * 1024 * 1024
    max_vision_pages: int = 12
    vision_concurrency: int = 2
    page_timeout_seconds: float = 45.0
    document_timeout_seconds: float = 300.0

    @classmethod
    def from_settings(cls, settings, *, max_pages: int | None = None) -> PdfRecognitionPolicy:
        configured_pages = int(settings.max_pdf_pages)
        if max_pages is not None:
            configured_pages = (
                max_pages
                if configured_pages <= 0
                else configured_pages
                if max_pages <= 0
                else min(configured_pages, max_pages)
            )
        return cls(
            max_pages=configured_pages,
            native_min_chars=settings.pdf_native_min_chars,
            native_no_visual_min_chars=settings.pdf_native_no_visual_min_chars,
            native_min_printable_ratio=settings.pdf_native_min_printable_ratio,
            native_min_text_coverage=settings.pdf_native_min_text_coverage,
            body_min_char_ratio=settings.pdf_body_min_char_ratio,
            mixed_image_area_ratio=settings.pdf_mixed_image_area_ratio,
            mixed_image_count=settings.pdf_mixed_image_count,
            render_dpi=settings.pdf_render_dpi,
            max_render_pixels=settings.pdf_max_render_pixels,
            max_render_bytes=settings.pdf_max_render_mb * 1024 * 1024,
            max_vision_pages=settings.pdf_vision_max_pages,
            vision_concurrency=settings.pdf_vision_concurrency,
            page_timeout_seconds=settings.pdf_page_timeout_seconds,
            document_timeout_seconds=settings.pdf_document_timeout_seconds,
        )


@dataclass(frozen=True)
class PdfPageInspection:
    page_number: int
    extraction_method: PdfExtractionMethod
    native_text: str
    native_text_chars: int
    printable_ratio: float
    text_coverage: float
    body_char_ratio: float
    image_count: int
    image_area_ratio: float
    drawing_count: int
    warning_code: str | None = None


@dataclass(frozen=True)
class PdfInspection:
    page_count: int
    pages: tuple[PdfPageInspection, ...]


@dataclass(frozen=True)
class PdfPageResult:
    page_number: int
    extraction_method: PdfExtractionMethod
    recognition_status: PdfRecognitionStatus
    markdown: str = ""
    native_text_chars: int = 0
    image_count: int = 0
    vision_used: bool = False
    confidence: float | None = None
    warning_code: str | None = None
    error_code: str | None = None

    def metadata(self) -> dict:
        value = {
            "page_number": self.page_number,
            "extraction_method": self.extraction_method,
            "recognition_status": self.recognition_status,
            "native_text_chars": self.native_text_chars,
            "image_count": self.image_count,
            "vision_used": self.vision_used,
        }
        if self.confidence is not None:
            value["confidence"] = self.confidence
        if self.warning_code:
            value["warning_code"] = self.warning_code
        if self.error_code:
            value["error_code"] = self.error_code
        return value


@dataclass(frozen=True)
class PdfRecognitionResult:
    page_count: int
    pages: tuple[PdfPageResult, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def successful_pages(self) -> tuple[PdfPageResult, ...]:
        return tuple(page for page in self.pages if page.recognition_status == "ready")

    @property
    def failed_pages(self) -> tuple[PdfPageResult, ...]:
        return tuple(page for page in self.pages if page.recognition_status == "failed")

    @property
    def markdown(self) -> str:
        return "\n\n".join(page.markdown for page in self.successful_pages if page.markdown)

    def bounded_metadata(self) -> dict:
        return {
            "page_count": self.page_count,
            "pages": [page.metadata() for page in self.pages],
            "warnings": list(self.warnings[:20]),
        }
