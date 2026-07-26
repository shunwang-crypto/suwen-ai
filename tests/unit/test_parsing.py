"""解析层测试: 每种格式在测试内现场生成真实 fixture, 不依赖外部文件与网络。"""

import fitz
import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from eduagents_core.parsing.base import ParseError
from eduagents_core.parsing.registry import SUPPORTED_SUFFIXES, get_parser
from pptx import Presentation
from pptx.util import Inches

MD_SAMPLE = r"""# 第3章 函数

## 3.2 导数定义

导数刻画瞬时变化率, 行内公式 $x^2$ 原样保留在段落文本中。

$$E=mc^2$$

$$
\int_a^b f(x)dx = F(b) - F(a)
$$

| 名称 | 值 |
| --- | --- |
| a | 1 |
| b | 2 |

表格之后的结尾段落。
"""


def _parse(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return get_parser(path.suffix).parse(path)


# ---------- markdown ----------


def test_markdown_headings(tmp_path):
    doc = _parse(tmp_path, "sample.md", MD_SAMPLE)
    assert doc.elements
    headings = [(e.level, e.text) for e in doc.elements if e.type == "heading"]
    assert headings == [(1, "第3章 函数"), (2, "3.2 导数定义")]


def test_markdown_table_is_single_element(tmp_path):
    doc = _parse(tmp_path, "sample.md", MD_SAMPLE)
    tables = [e for e in doc.elements if e.type == "table"]
    assert len(tables) == 1
    assert "| a | 1 |" in tables[0].text
    assert "| b | 2 |" in tables[0].text


def test_markdown_formula_elements(tmp_path):
    doc = _parse(tmp_path, "sample.md", MD_SAMPLE)
    formulas = [e for e in doc.elements if e.type == "formula"]
    assert any(f.text == "$$E=mc^2$$" for f in formulas)
    # 多行 $$..$$ 块也要完整保留定界符与 LaTeX 源码
    assert any(
        f.text.startswith("$$") and f.text.endswith("$$") and r"\int_a^b" in f.text
        for f in formulas
    )


def test_markdown_inline_math_stays_in_paragraph(tmp_path):
    doc = _parse(tmp_path, "sample.md", MD_SAMPLE)
    paras = [e for e in doc.elements if e.type == "paragraph"]
    assert any("$x^2$" in p.text for p in paras)
    assert not any(e.type == "formula" and e.text == "$x^2$" for e in doc.elements)


# ---------- txt ----------


def test_txt_paragraphs_split_on_blank_lines(tmp_path):
    doc = _parse(tmp_path, "notes.txt", "第一段。\n\n第二段。\n仍属第二段。\n\n\n第三段。")
    assert [e.type for e in doc.elements] == ["paragraph"] * 3
    assert doc.elements[1].text == "第二段。\n仍属第二段。"


# ---------- docx ----------


def test_docx_parser(tmp_path):
    d = Document()
    d.add_heading("第一章 微积分", level=1)
    d.add_heading("1.1 导数", level=2)
    d.add_paragraph("导数是变化率的极限, 行内公式 $f'(x)$ 保留。")
    d.styles.add_style("标题 3", WD_STYLE_TYPE.PARAGRAPH)
    d.add_paragraph("中文样式标题", style="标题 3")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "名称"
    table.cell(0, 1).text = "值"
    table.cell(1, 0).text = "极限"
    table.cell(1, 1).text = "0"
    path = tmp_path / "doc.docx"
    d.save(str(path))

    doc = get_parser(".docx").parse(path)
    assert doc.elements
    headings = [(e.level, e.text) for e in doc.elements if e.type == "heading"]
    assert (1, "第一章 微积分") in headings
    assert (2, "1.1 导数") in headings
    assert (3, "中文样式标题") in headings  # 中文 "标题 N" 样式
    paras = [e for e in doc.elements if e.type == "paragraph"]
    assert any("$f'(x)$" in p.text for p in paras)
    tables = [e for e in doc.elements if e.type == "table"]
    assert len(tables) == 1
    assert "| 名称 | 值 |" in tables[0].text
    assert "| 极限 | 0 |" in tables[0].text
    assert all(e.page is None for e in doc.elements)  # docx 拿不到可靠页码


# ---------- pptx ----------


def test_pptx_parser(tmp_path):
    prs = Presentation()
    layout = prs.slide_layouts[1]
    s1 = prs.slides.add_slide(layout)
    s1.shapes.title.text = "第一页标题"
    s1.placeholders[1].text_frame.text = "第一页正文要点"
    s2 = prs.slides.add_slide(layout)
    s2.shapes.title.text = "第二页标题"
    tbl = s2.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    tbl.cell(0, 0).text = "科目"
    tbl.cell(0, 1).text = "学分"
    tbl.cell(1, 0).text = "高数"
    tbl.cell(1, 1).text = "5"
    path = tmp_path / "deck.pptx"
    prs.save(str(path))

    doc = get_parser(".pptx").parse(path)
    headings = [(e.text, e.page, e.level) for e in doc.elements if e.type == "heading"]
    assert headings == [("第一页标题", 1, 1), ("第二页标题", 2, 1)]
    paras = [e for e in doc.elements if e.type == "paragraph"]
    assert any(p.text == "第一页正文要点" and p.page == 1 for p in paras)
    tables = [e for e in doc.elements if e.type == "table"]
    assert len(tables) == 1
    assert tables[0].page == 2
    assert "| 高数 | 5 |" in tables[0].text
    assert doc.page_count == 2


# ---------- pdf ----------


def _make_pdf(path):
    # 标题用更大字号, pymupdf4llm 按字号推断 heading 层级
    pdf = fitz.open()
    page1 = pdf.new_page()
    page1.insert_text((72, 80), "Chapter 1 Calculus", fontsize=24)
    page1.insert_text((72, 130), "1.1 Derivatives", fontsize=18)
    y = 170
    for i in range(8):
        page1.insert_text(
            (72, y), f"The derivative measures instant rate of change, line {i}.", fontsize=11
        )
        y += 18
    page2 = pdf.new_page()
    page2.insert_text((72, 80), "Second page paragraph about integrals and limits.", fontsize=11)
    pdf.save(str(path))
    pdf.close()


def test_pdf_parser(tmp_path):
    path = tmp_path / "calc.pdf"
    _make_pdf(path)
    doc = get_parser(".pdf").parse(path)
    assert doc.elements
    assert doc.page_count == 2
    h1 = [e for e in doc.elements if e.type == "heading" and e.level == 1]
    h2 = [e for e in doc.elements if e.type == "heading" and e.level == 2]
    assert any("Chapter 1 Calculus" in h.text for h in h1)
    assert any("1.1 Derivatives" in h.text for h in h2)
    assert all(e.page == 1 for e in h1 + h2)  # page 为真实页码
    page2_elems = [e for e in doc.elements if e.page == 2]
    assert page2_elems
    assert any("Second page" in e.text for e in page2_elems)


# ---------- registry ----------


def test_registry_supported_suffixes():
    assert SUPPORTED_SUFFIXES == (".pdf", ".docx", ".pptx", ".md", ".txt")
    for suffix in SUPPORTED_SUFFIXES:
        parser = get_parser(suffix)
        assert suffix in parser.suffixes
    # 大小写 / 无点写法归一化
    assert type(get_parser("MD")) is type(get_parser(".md"))


def test_registry_rejects_unknown_suffix():
    with pytest.raises(ParseError):
        get_parser(".xls")
