from __future__ import annotations

import re
from pathlib import Path

import docx
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from .base import Element, ParsedDocument, ParseError
from .md_structure import table_rows_to_markdown

# Word 内建标题样式名中英文皆可能出现: "Heading 2" / "标题 2"
_HEADING_STYLE_RE = re.compile(r"^(?:heading|标题)\s*([1-6])$", re.IGNORECASE)


class DocxParser:
    """docx 无可靠分页信息, page 一律 None。"""

    suffixes: tuple[str, ...] = (".docx",)

    def parse(self, path: Path) -> ParsedDocument:
        try:
            document = docx.Document(str(path))
        except Exception as exc:
            raise ParseError(f"DOCX 解析失败 ({path.name}): {exc}") from exc

        elements: list[Element] = []
        # doc.paragraphs / doc.tables 各自成列会丢正文顺序, 需按 body XML 子节点还原
        for child in document.element.body.iterchildren():
            if isinstance(child, CT_P):
                para = Paragraph(child, document)
                text = para.text.strip()
                if not text:
                    continue
                style_name = para.style.name if para.style is not None else ""
                m = _HEADING_STYLE_RE.match((style_name or "").strip())
                if m:
                    elements.append(Element(type="heading", text=text, level=int(m.group(1))))
                else:
                    elements.append(Element(type="paragraph", text=text))
            elif isinstance(child, CT_Tbl):
                rows = [
                    [cell.text for cell in row.cells] for row in Table(child, document).rows
                ]
                md = table_rows_to_markdown(rows)
                if md:
                    elements.append(Element(type="table", text=md))
        return ParsedDocument(elements=elements, page_count=0, meta={"source": path.name})
