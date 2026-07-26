from __future__ import annotations

from .base import DocumentParser, ParseError
from .docx import DocxParser
from .markdown import MarkdownParser, TxtParser
from .pdf import PdfParser
from .pptx import PptxParser

SUPPORTED_SUFFIXES: tuple[str, ...] = (".pdf", ".docx", ".pptx", ".md", ".txt")

_PARSER_CLASSES: dict[str, type] = {
    ".pdf": PdfParser,
    ".docx": DocxParser,
    ".pptx": PptxParser,
    ".md": MarkdownParser,
    ".txt": TxtParser,
}


def get_parser(suffix: str) -> DocumentParser:
    key = suffix.strip().lower()
    if not key.startswith("."):
        key = f".{key}"
    cls = _PARSER_CLASSES.get(key)
    if cls is None:
        raise ParseError(f"不支持的文件类型: {suffix} (支持: {', '.join(SUPPORTED_SUFFIXES)})")
    return cls()
