from __future__ import annotations

from pathlib import Path

from pptx import Presentation

from .base import Element, ParsedDocument, ParseError
from .md_structure import table_rows_to_markdown


class PptxParser:
    """slide 序号 (1-based) 作 page; 标题占位符 -> heading(level=1)。"""

    suffixes: tuple[str, ...] = (".pptx",)

    def parse(self, path: Path) -> ParsedDocument:
        try:
            prs = Presentation(str(path))
        except Exception as exc:
            raise ParseError(f"PPTX 解析失败 ({path.name}): {exc}") from exc

        elements: list[Element] = []
        page_count = 0
        for page_no, slide in enumerate(prs.slides, start=1):
            page_count = page_no
            title = slide.shapes.title
            title_id = title.shape_id if title is not None else None
            for shape in slide.shapes:
                if shape.has_table:
                    rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                    md = table_rows_to_markdown(rows)
                    if md:
                        elements.append(Element(type="table", text=md, page=page_no))
                elif shape.has_text_frame:
                    text = shape.text_frame.text.strip()
                    if not text:
                        continue
                    if title_id is not None and shape.shape_id == title_id:
                        elements.append(Element(type="heading", text=text, page=page_no, level=1))
                    else:
                        elements.append(Element(type="paragraph", text=text, page=page_no))
        return ParsedDocument(elements=elements, page_count=page_count, meta={"source": path.name})
