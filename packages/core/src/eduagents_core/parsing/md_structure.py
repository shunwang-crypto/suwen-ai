"""markdown 结构解析: pdf (pymupdf4llm 输出) 与 .md 文件共用的逐行状态机。"""

from __future__ import annotations

import re

from .base import Element

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
# pymupdf4llm 在页尾/分节处输出 ----- 水平线, 不是内容
_RULE_RE = re.compile(r"^-{3,}\s*$")


def table_rows_to_markdown(rows: list[list[str]]) -> str:
    """单元格矩阵 -> markdown 表格文本 (docx/pptx 表格共用)。"""
    if not rows:
        return ""
    normalized = [[" ".join(str(c).split()).replace("|", "\\|") for c in row] for row in rows]
    lines = ["| " + " | ".join(cells) + " |" for cells in normalized]
    lines.insert(1, "|" + " --- |" * len(normalized[0]))
    return "\n".join(lines)


def _is_table_line(stripped: str) -> bool:
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def parse_markdown_elements(md_text: str, page: int | None = None) -> list[Element]:
    """markdown 文本 -> Element 序列。

    - 连续 |..| 行合并为一个 table Element
    - 独立 $$..$$ 块 (单行或多行) 为 formula Element
    - 行内 $..$ 原样留在段落文本中, 不做任何拆分
    """
    elements: list[Element] = []
    para: list[str] = []
    table: list[str] = []
    formula: list[str] = []
    in_formula = False

    def flush_para() -> None:
        text = "\n".join(para).strip()
        para.clear()
        if text:
            elements.append(Element(type="paragraph", text=text, page=page))

    def flush_table() -> None:
        text = "\n".join(table).strip()
        table.clear()
        if text:
            elements.append(Element(type="table", text=text, page=page))

    for raw in md_text.splitlines():
        line = raw.strip()

        if in_formula:
            formula.append(line)
            if "$$" in line:
                elements.append(
                    Element(type="formula", text="\n".join(formula).strip(), page=page)
                )
                formula.clear()
                in_formula = False
            continue

        if _is_table_line(line):
            flush_para()
            table.append(line)
            continue
        if table:
            flush_table()

        if line.startswith("$$"):
            flush_para()
            if len(line) > 3 and line.endswith("$$"):
                elements.append(Element(type="formula", text=line, page=page))
            else:  # 只有开定界符, 进入多行公式块
                formula.append(line)
                in_formula = True
            continue

        m = _HEADING_RE.match(line)
        if m:
            flush_para()
            # pymupdf4llm 对粗体标题会输出 # **title**, 去掉包裹的星号
            title = m.group(2).strip().strip("*").strip()
            if title:
                elements.append(
                    Element(type="heading", text=title, page=page, level=len(m.group(1)))
                )
            continue

        if not line or _RULE_RE.match(line):
            flush_para()
            continue

        para.append(line)

    if formula:  # 未闭合的公式块兜底, 不丢内容
        elements.append(Element(type="formula", text="\n".join(formula).strip(), page=page))
    flush_table()
    flush_para()
    return elements
