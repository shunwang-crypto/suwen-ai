from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

ElementType = Literal["heading", "paragraph", "table", "formula", "image_text"]


@dataclass
class Element:
    """统一 IR: 所有 parser 输出 Element 序列, 下游与文件格式解耦 (§5.1)。

    - table: 原子, 禁止跨块切分 (F2-A6)
    - formula: LaTeX 源码保留在 text 中
    - heading: level 1-6, text 为标题文本
    """

    type: ElementType
    text: str
    page: int | None = None
    level: int = 0  # heading 专用


@dataclass
class ParsedDocument:
    elements: list[Element]
    page_count: int = 0
    meta: dict = field(default_factory=dict)


class DocumentParser(Protocol):
    """每种格式一个实现; 注册表见 parsing/registry.py。解析失败抛 ParseError。"""

    suffixes: tuple[str, ...]

    def parse(self, path: Path) -> ParsedDocument: ...


class ParseError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)
