from __future__ import annotations

from dataclasses import dataclass, field

from ..parsing.base import ParsedDocument


@dataclass
class ChildChunk:
    """检索命中单元 (子块)。text 不含 heading_path 前缀, 入向量时由调用方拼接。"""

    text: str
    heading_path: str
    page_start: int | None = None
    page_end: int | None = None
    is_table: bool = False


@dataclass
class ParentChunk:
    """喂给 LLM 的上下文单元 (父块), 含其全部子块。"""

    text: str
    heading_path: str
    page_start: int | None = None
    page_end: int | None = None
    children: list[ChildChunk] = field(default_factory=list)


def chunk_document(
    doc: ParsedDocument,
    *,
    child_chars: int = 700,
    parent_chars: int = 2400,
    overlap_chars: int = 80,
) -> list[ParentChunk]:
    """结构优先三层分块 (§5.1), 由 chunking/structural.py 实现:

    1. 按 heading 层级切 section, chunk 携带面包屑 heading_path
       (如 "第3章 函数 > 3.2 导数定义")
    2. section 内聚合成 parent (~parent_chars), parent 内切 child
       (~child_chars, 相邻 child 重叠 overlap_chars, 禁止拦腰截断后无重叠)
    3. table Element 原子: 单独成 child, 永不切分 (F2-A6);
       formula 文本 ($...$) 必须完整保留在所属 chunk 内
    """
    from .structural import chunk_document_impl

    return chunk_document_impl(
        doc,
        child_chars=child_chars,
        parent_chars=parent_chars,
        overlap_chars=overlap_chars,
    )
