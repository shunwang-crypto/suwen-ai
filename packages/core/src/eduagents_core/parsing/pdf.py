from __future__ import annotations

from pathlib import Path

import pymupdf4llm

from .base import Element, ParsedDocument, ParseError
from .md_structure import parse_markdown_elements


class PdfParser:
    """PDF -> 逐页 markdown (pymupdf4llm) -> markdown 结构解析, page 用真实页码。"""

    suffixes: tuple[str, ...] = (".pdf",)

    def parse(self, path: Path) -> ParsedDocument:
        try:
            pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
        except Exception as exc:
            raise ParseError(f"PDF 解析失败 ({path.name}): {exc}") from exc

        elements: list[Element] = []
        for idx, page_data in enumerate(pages):
            meta = page_data.get("metadata") or {}
            # pymupdf4llm 不同版本页码键名不同 (page_number / page), 都是 1-based
            page_no = meta.get("page_number") or meta.get("page") or idx + 1
            elements.extend(parse_markdown_elements(page_data.get("text") or "", page=page_no))
        return ParsedDocument(elements=elements, page_count=len(pages), meta={"source": path.name})
