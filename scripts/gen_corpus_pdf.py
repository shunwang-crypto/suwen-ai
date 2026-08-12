"""把 corpus/calculus.md 真实排版为 >=20 页 PDF (corpus/calculus.pdf)。

生成本地摄取测试使用的多页数学 PDF（走 pymupdf4llm 解析路径）。
本脚本用 pymupdf (fitz) 新建文档, 逐块 insert_textbox 排版:
- markdown # 级数映射标题字号 (# 章 / ## 节 / ### 小节), 标题加大字号并叠印仿粗;
- 段落正文按内容宽度自动折行 (中文逐字、英文/LaTeX 按词, 超宽 token 硬断);
- 表格行、块级公式 ($$...$$) 原样保留 LaTeX 源码 —— 全程真实数学文本, 无注水乱码;
- 每页页脚含页码; 单遍排不满 20 页时按章节重复排版, 页脚标注 "第N遍排版"。

内置 china-s (简体中文) 字体, 无需外部字体文件。
用法: python scripts/gen_corpus_pdf.py
"""

from __future__ import annotations

from pathlib import Path

import fitz

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_MD = REPO_ROOT / "corpus" / "calculus.md"
OUT_PDF = REPO_ROOT / "corpus" / "calculus.pdf"
MIN_PAGES = 20
FONT = "china-s"

# 版面 (A4)
PAGE_W, PAGE_H = fitz.paper_size("a4")
MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 62, 58, 72, 62

BODY_FS = 10.5
HEADING_FS = {1: 19.0, 2: 15.0, 3: 12.5}
FORMULA_FS = 10.0
TABLE_FS = 9.5
LINE_FACTOR = 1.5

FORMULA_COLOR = (0.15, 0.15, 0.45)
TABLE_COLOR = (0.10, 0.30, 0.20)
FOOTER_COLOR = (0.5, 0.5, 0.5)


def _tokenize(text: str) -> list[str]:
    """切成折行 token: 连续 ASCII (含 LaTeX/数字) 为一个词, 空格与其他字符各自成 token。"""
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if ch == " ":
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(" ")
        elif ord(ch) < 128:
            buf += ch
        else:
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
    if buf:
        tokens.append(buf)
    return tokens


def _wrap(text: str, font: fitz.Font, fs: float, max_w: float) -> list[str]:
    """按 max_w 折行; 单个超宽 ASCII token 逐字硬断, 保证不溢出版心。"""
    lines: list[str] = []
    cur = ""
    for tok in _tokenize(text):
        if cur == "" and tok == " ":
            continue
        if font.text_length(cur + tok, fontsize=fs) <= max_w:
            cur += tok
            continue
        if cur:
            lines.append(cur)
            cur = ""
            if tok == " ":
                continue
        while font.text_length(tok, fontsize=fs) > max_w and len(tok) > 1:
            i = 1
            while i < len(tok) and font.text_length(tok[: i + 1], fontsize=fs) <= max_w:
                i += 1
            lines.append(tok[:i])
            tok = tok[i:]
        cur = tok
    if cur:
        lines.append(cur)
    return lines or [""]


class Layout:
    def __init__(self) -> None:
        self.doc = fitz.open()
        self.font = fitz.Font(FONT)
        self.page: fitz.Page | None = None
        self.y = 0.0
        self.n = 0
        self.pass_no = 1
        self._new_page()

    def _new_page(self) -> None:
        self.n += 1
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self.y = MARGIN_T
        self._footer()

    def _footer(self) -> None:
        txt = f"高等数学讲义 · 第 {self.n} 页"
        if self.pass_no > 1:
            txt += f"   (第{self.pass_no}遍排版)"
        assert self.page is not None
        self.page.insert_textbox(
            fitz.Rect(MARGIN_L, PAGE_H - 42, PAGE_W - MARGIN_R, PAGE_H - 24),
            txt, fontname=FONT, fontsize=8, color=FOOTER_COLOR, align=1,
        )

    def space(self, dh: float) -> None:
        self.y += dh
        if self.y > PAGE_H - MARGIN_B:
            self._new_page()

    def _line(self, text: str, fs: float, *, indent: float, color, bold: bool) -> None:
        lh = fs * LINE_FACTOR
        if self.y + lh > PAGE_H - MARGIN_B:
            self._new_page()
        x0 = MARGIN_L + indent
        rect = fitz.Rect(x0, self.y, PAGE_W - MARGIN_R, self.y + lh + 2)
        assert self.page is not None
        self.page.insert_textbox(rect, text, fontname=FONT, fontsize=fs, color=color)
        if bold:  # 叠印 0.4pt 仿粗 (内置 CJK 无粗体变体)
            rect2 = fitz.Rect(x0 + 0.4, self.y, PAGE_W - MARGIN_R, self.y + lh + 2)
            self.page.insert_textbox(rect2, text, fontname=FONT, fontsize=fs, color=color)
        self.y += lh

    def paragraph(self, text: str, fs: float = BODY_FS, *, indent: float = 0.0,
                  color=(0, 0, 0), bold: bool = False) -> None:
        max_w = PAGE_W - MARGIN_R - (MARGIN_L + indent)
        for ln in _wrap(text, self.font, fs, max_w):
            self._line(ln, fs, indent=indent, color=color, bold=bold)

    def heading(self, level: int, text: str) -> None:
        fs = HEADING_FS.get(level, BODY_FS)
        self.space(12 if level < 3 else 7)
        max_w = PAGE_W - MARGIN_R - MARGIN_L
        for ln in _wrap(text, self.font, fs, max_w):
            self._line(ln, fs, indent=0, color=(0, 0, 0), bold=True)
        self.space(4)


def _parse_blocks(md: str) -> list[tuple[str, object]]:
    """md -> 有序块列表: ("heading",(level,text)) / ("para",text) /
    ("formula",[行...]) / ("table",[行...])。保留真实数学内容与 LaTeX 源。"""
    blocks: list[tuple[str, object]] = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(("para", "".join(para)))
            para.clear()

    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith("<!--") or s == "---":
            flush_para()
            i += 1
            continue
        if s.startswith("#"):
            flush_para()
            level = len(s) - len(s.lstrip("#"))
            blocks.append(("heading", (min(level, 3), s.lstrip("#").strip())))
            i += 1
            continue
        if s == "$$":  # 多行块级公式
            flush_para()
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "$$":
                body.append(lines[i].strip())
                i += 1
            i += 1  # 跳过结束 $$
            blocks.append(("formula", body))
            continue
        if s.startswith("$$") and s.endswith("$$") and len(s) > 4:  # 单行块级公式
            flush_para()
            blocks.append(("formula", [s[2:-2].strip()]))
            i += 1
            continue
        if s.startswith("|"):  # markdown 表格
            flush_para()
            rows: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set("-: ") for c in cells):  # 跳过分隔行
                    rows.append("  |  ".join(cells))
                i += 1
            blocks.append(("table", rows))
            continue
        para.append(s)
        i += 1
    flush_para()
    return blocks


def _render_pass(lay: Layout, blocks: list[tuple[str, object]]) -> None:
    for kind, payload in blocks:
        if kind == "heading":
            level, text = payload  # type: ignore[misc]
            lay.heading(level, text)
        elif kind == "para":
            lay.paragraph(payload)  # type: ignore[arg-type]
            lay.space(3)
        elif kind == "formula":
            lay.space(3)
            for ln in payload:  # type: ignore[union-attr]
                lay.paragraph(ln, fs=FORMULA_FS, indent=18, color=FORMULA_COLOR)
            lay.space(4)
        elif kind == "table":
            lay.space(3)
            for r, row in enumerate(payload):  # type: ignore[arg-type]
                lay.paragraph(row, fs=TABLE_FS, indent=10, color=TABLE_COLOR, bold=(r == 0))
            lay.space(4)


def generate() -> Path:
    md = SRC_MD.read_text(encoding="utf-8")
    blocks = _parse_blocks(md)
    lay = Layout()
    while True:
        _render_pass(lay, blocks)
        if lay.n >= MIN_PAGES or lay.pass_no >= 6:
            break
        lay.pass_no += 1
        lay._new_page()
    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    lay.doc.save(str(OUT_PDF), deflate=True)
    lay.doc.close()
    return OUT_PDF


if __name__ == "__main__":
    out = generate()
    doc = fitz.open(str(out))
    print(f"生成 {out} : {doc.page_count} 页, {out.stat().st_size} 字节")
    doc.close()
