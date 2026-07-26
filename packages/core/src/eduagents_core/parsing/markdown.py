from __future__ import annotations

import re
from pathlib import Path

from .base import Element, ParsedDocument, ParseError
from .md_structure import parse_markdown_elements

_BLANK_LINES_RE = re.compile(r"\n\s*\n")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            # 常见的非 UTF-8 中文纯文本兜底
            return path.read_text(encoding="gb18030")
        except (UnicodeDecodeError, OSError) as exc:
            raise ParseError(f"文本解码失败 ({path.name}): {exc}") from exc
    except OSError as exc:
        raise ParseError(f"文本读取失败 ({path.name}): {exc}") from exc


class MarkdownParser:
    suffixes: tuple[str, ...] = (".md",)

    def parse(self, path: Path) -> ParsedDocument:
        elements = parse_markdown_elements(_read_text(path))
        return ParsedDocument(elements=elements, page_count=0, meta={"source": path.name})


class TxtParser:
    """txt 无结构: 按空行分段, 全部视为 paragraph。"""

    suffixes: tuple[str, ...] = (".txt",)

    def parse(self, path: Path) -> ParsedDocument:
        elements = [
            Element(type="paragraph", text=block.strip())
            for block in _BLANK_LINES_RE.split(_read_text(path))
            if block.strip()
        ]
        return ParsedDocument(elements=elements, page_count=0, meta={"source": path.name})
