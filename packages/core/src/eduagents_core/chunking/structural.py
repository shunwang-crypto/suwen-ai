"""结构优先父子分块实现 (§5.1, F2-A5/F2-A6), 入口见 chunking/base.chunk_document。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..parsing.base import ParsedDocument
from .base import ChildChunk, ParentChunk

# 句边界: 超长段落只在这些字符之后切, 找不到边界宁可超限也不硬切
_SENTENCE_END = frozenset("。！？.!?\n")
# $..$ / $$..$$ 数学区间: 切点与 overlap 起点都不许落在区间内部 (F2-A6)
_MATH_SPAN_RE = re.compile(r"\$\$.+?\$\$|\$[^$\n]+?\$", re.DOTALL)


@dataclass
class _Unit:
    """分块聚合的最小单位: 一个段落 (含附着公式) 或一张原子表格。"""

    text: str
    is_table: bool = False
    page_start: int | None = None
    page_end: int | None = None


def _math_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _MATH_SPAN_RE.finditer(text)]


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s < pos < e for s, e in spans)


def _page_range(units: list[_Unit]) -> tuple[int | None, int | None]:
    starts = [u.page_start for u in units if u.page_start is not None]
    ends = [u.page_end for u in units if u.page_end is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _build_sections(doc: ParsedDocument) -> list[tuple[str, list[_Unit]]]:
    """按 heading 面包屑栈切 section, 并把 formula 附着到前一段落尾部。"""
    sections: list[tuple[str, list[_Unit]]] = []
    stack: list[tuple[int, str]] = []
    path = ""
    units: list[_Unit] = []

    def flush() -> None:
        nonlocal units
        if units:
            sections.append((path, units))
        units = []

    for el in doc.elements:
        text = (el.text or "").strip()
        if el.type == "heading":
            flush()
            level = min(max(el.level, 1), 6)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            path = " > ".join(t for _, t in stack)
            continue
        if not text:
            continue
        if el.type == "table":
            units.append(_Unit(text, is_table=True, page_start=el.page, page_end=el.page))
        elif el.type == "formula" and units and not units[-1].is_table:
            # 附着后公式与上下文同块, $$..$$ 由数学区间保护保证不被切开
            prev = units[-1]
            prev.text = f"{prev.text}\n{text}"
            if el.page is not None:
                prev.page_end = el.page
                if prev.page_start is None:
                    prev.page_start = el.page
        else:  # paragraph / image_text / 无前置段落的孤立 formula
            units.append(_Unit(text, page_start=el.page, page_end=el.page))
    flush()
    return sections


def _split_long_text(text: str, *, child_chars: int, overlap_chars: int) -> list[str]:
    """单段超长: 只在句边界切, 相邻切片头部回退 overlap_chars 形成重叠。"""
    spans = _math_spans(text)
    cuts = [
        i + 1 for i, ch in enumerate(text) if ch in _SENTENCE_END and not _inside(i + 1, spans)
    ]
    if not cuts or cuts[-1] != len(text):
        cuts.append(len(text))

    pieces: list[str] = []
    seg_start = 0
    while seg_start < len(text):
        limit = seg_start + child_chars
        end: int | None = None
        for c in cuts:
            if c <= seg_start:
                continue
            if c <= limit:
                end = c
            else:
                if end is None:
                    end = c  # 限内无句边界: 取下一个边界, 宁超不硬切
                break
        if end is None:
            end = len(text)
        start = seg_start
        if pieces and overlap_chars > 0:
            start = max(seg_start - overlap_chars, 0)
            for s, e in spans:
                if s < start < e:
                    start = s  # overlap 起点落进公式则左移到公式开头
                    break
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        seg_start = end
    return pieces


def _split_children(
    units: list[_Unit], path: str, *, child_chars: int, overlap_chars: int
) -> list[ChildChunk]:
    children: list[ChildChunk] = []
    buf: list[_Unit] = []

    def flush_buf() -> None:
        if not buf:
            return
        text = "\n\n".join(u.text for u in buf)
        ps, pe = _page_range(buf)
        if len(text) <= child_chars:
            children.append(ChildChunk(text=text, heading_path=path, page_start=ps, page_end=pe))
        else:
            # 超限的 buf 必然是单个超长段落 (聚合阶段不会把多段凑过限)
            for piece in _split_long_text(text, child_chars=child_chars, overlap_chars=overlap_chars):
                children.append(
                    ChildChunk(text=piece, heading_path=path, page_start=ps, page_end=pe)
                )
        buf.clear()

    for u in units:
        if u.is_table:
            flush_buf()
            children.append(
                ChildChunk(
                    text=u.text,
                    heading_path=path,
                    page_start=u.page_start,
                    page_end=u.page_end,
                    is_table=True,
                )
            )
            continue
        joined_len = sum(len(b.text) for b in buf) + 2 * max(len(buf) - 1, 0)
        if buf and joined_len + 2 + len(u.text) > child_chars:
            flush_buf()
        buf.append(u)
    flush_buf()
    return children


def chunk_document_impl(
    doc: ParsedDocument,
    *,
    child_chars: int,
    parent_chars: int,
    overlap_chars: int,
) -> list[ParentChunk]:
    parents: list[ParentChunk] = []
    for path, units in _build_sections(doc):
        # section 内按 unit 边界聚合 parent, 不在段落中间硬切
        groups: list[list[_Unit]] = []
        cur: list[_Unit] = []
        size = 0
        for u in units:
            if cur and size + len(u.text) > parent_chars:
                groups.append(cur)
                cur, size = [], 0
            cur.append(u)
            size += len(u.text)
        if cur:
            groups.append(cur)

        for group in groups:
            children = _split_children(
                group, path, child_chars=child_chars, overlap_chars=overlap_chars
            )
            if not children:
                continue
            ps, pe = _page_range(group)
            # parent.text 用原始 unit 顺序拼接: 与 children 顺序一致,
            # 且不重复计入 overlap 部分 (保证公式/表格原文恰好出现一次)
            parents.append(
                ParentChunk(
                    text="\n\n".join(u.text for u in group),
                    heading_path=path,
                    page_start=ps,
                    page_end=pe,
                    children=children,
                )
            )
    return parents
