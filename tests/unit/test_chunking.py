"""结构分块测试 (F2-A5 边界质量 / F2-A6 公式与表格原子性)。"""

from eduagents_core.chunking.base import chunk_document
from eduagents_core.parsing.base import Element, ParsedDocument
from eduagents_core.parsing.registry import get_parser


def _doc(*elements):
    return ParsedDocument(elements=list(elements))


def _h(text, level, page=None):
    return Element(type="heading", text=text, page=page, level=level)


def _p(text, page=None):
    return Element(type="paragraph", text=text, page=page)


def _t(text, page=None):
    return Element(type="table", text=text, page=page)


def _f(text, page=None):
    return Element(type="formula", text=text, page=page)


def test_heading_path_breadcrumb():
    parents = chunk_document(
        _doc(
            _h("第3章 函数", 1),
            _p("本章介绍函数与导数。"),
            _h("3.2 导数定义", 2),
            _p("导数定义为极限。"),
            _h("3.3 积分", 2),  # 同级 heading 弹栈后重建面包屑
            _p("积分是求和的极限。"),
        ),
        child_chars=200,
        parent_chars=800,
        overlap_chars=20,
    )
    assert [p.heading_path for p in parents] == [
        "第3章 函数",
        "第3章 函数 > 3.2 导数定义",
        "第3章 函数 > 3.3 积分",
    ]
    assert parents[1].children[0].heading_path == "第3章 函数 > 3.2 导数定义"


def test_empty_sections_skipped():
    parents = chunk_document(
        _doc(_h("第1章", 1), _h("1.1 小节", 2), _p("唯一内容。"), _h("1.2 空小节", 2)),
        child_chars=100,
        parent_chars=400,
        overlap_chars=10,
    )
    assert [p.heading_path for p in parents] == ["第1章 > 1.1 小节"]
    assert (
        chunk_document(_doc(_h("孤立标题", 1)), child_chars=100, parent_chars=400, overlap_chars=10)
        == []
    )


def test_parent_size_bounded_and_children_consistent():
    paras = [_p("知识点讲解段落。" * 20) for _ in range(12)]  # 每段 160 字
    parents = chunk_document(
        _doc(_h("第1章", 1), *paras), child_chars=200, parent_chars=600, overlap_chars=20
    )
    assert len(parents) >= 3
    for p in parents:
        assert len(p.text) <= 600 * 1.5
        assert p.heading_path == "第1章"
        assert p.children
        for c in p.children:
            assert c.text.strip()  # child 不为空串
            assert c.text in p.text  # child 原文都能在 parent 中找到


def test_long_paragraph_split_with_overlap():
    long_text = "".join(f"这是第{i:02d}句, 讲解导数的极限定义与几何意义。" for i in range(40))
    parents = chunk_document(
        _doc(_h("第3章", 1), _p(long_text)),
        child_chars=150,
        parent_chars=5000,
        overlap_chars=40,
    )
    assert len(parents) == 1
    children = parents[0].children
    assert len(children) >= 3
    for a, b in zip(children, children[1:], strict=False):
        assert a.text and b.text
        assert b.text[:20] in a.text  # 相邻切片有重叠 (无"拦腰截断且无重叠")
    # 切片只发生在句边界: 每个 child 结尾是句末标点
    assert all(c.text[-1] in "。！？.!?" for c in children)


TABLE_MD = "| 名称 | 值 |\n| --- |\n| a | 1 |"


def test_table_child_is_atomic():
    parents = chunk_document(
        _doc(_h("附录", 1), _p("表格前说明。"), _t(TABLE_MD), _p("表格后说明。")),
        child_chars=50,
        parent_chars=400,
        overlap_chars=10,
    )
    children = [c for p in parents for c in p.children]
    table_children = [c for c in children if c.is_table]
    assert len(table_children) == 1
    assert table_children[0].text == TABLE_MD  # 不与其它文本合并
    assert TABLE_MD in parents[0].text  # 表格原文含在 parent 内
    non_table = [c for c in children if not c.is_table]
    assert all("|" not in c.text for c in non_table)


def test_oversized_table_never_split():
    big_table = "\n".join(f"| 行{i} | 数值{i} |" for i in range(120))
    parents = chunk_document(
        _doc(_h("数据", 1), _t(big_table)),
        child_chars=100,
        parent_chars=300,
        overlap_chars=10,
    )
    children = [c for p in parents for c in p.children]
    assert len(children) == 1
    assert children[0].is_table
    assert children[0].text == big_table  # 超长表格也不切


def test_formula_attaches_to_previous_paragraph():
    parents = chunk_document(
        _doc(_h("能量", 1), _p("质能方程如下:"), _f("$$E=mc^2$$"), _p("其中 c 为光速。")),
        child_chars=500,
        parent_chars=2000,
        overlap_chars=20,
    )
    children = [c for p in parents for c in p.children]
    target = [c for c in children if "$$E=mc^2$$" in c.text]
    assert len(target) == 1
    assert "质能方程如下:" in target[0].text  # 附着在前一段落尾部


def test_formula_never_torn_across_children():
    sentences = "".join(f"第{i:02d}句包含行内公式 $\\alpha_{{{i}}}$ 并说明。" for i in range(30))
    parents = chunk_document(
        _doc(_h("推导", 1), _p(sentences), _f("$$\\sum_i \\alpha_i = 1$$")),
        child_chars=120,
        parent_chars=5000,
        overlap_chars=30,
    )
    children = [c for p in parents for c in p.children]
    assert len(children) >= 3
    for c in children:
        assert c.text.count("$") % 2 == 0  # 每个 $..$/$$..$$ 定界符成对, 未被切开
    assert any("$$\\sum_i \\alpha_i = 1$$" in c.text for c in children)


def test_pages_propagate_to_chunks():
    parents = chunk_document(
        _doc(_h("章", 1, page=1), _p("第一页内容。", page=1), _p("第二页内容。", page=2)),
        child_chars=500,
        parent_chars=2000,
        overlap_chars=10,
    )
    assert parents[0].page_start == 1
    assert parents[0].page_end == 2


# ---------- F2-A6: 端到端 '$' 计数守恒 ----------

FORMULA_MD = (
    r"""# 公式与表格

## 定义

行内公式 $x^2 + y^2 = z^2$ 出现在段落中。

$$\int_a^b f(x)dx = F(b) - F(a)$$

"""
    + "".join(f"第{i:02d}句含公式 $\\beta_{i}$ 与结论。" for i in range(40))
    + r"""

| 表头A | 表头B |
| --- |
| $a+b$ | $c$ |
"""
)


def test_dollar_count_preserved_end_to_end(tmp_path):
    path = tmp_path / "formula.md"
    path.write_text(FORMULA_MD, encoding="utf-8")
    doc = get_parser(".md").parse(path)
    parents = chunk_document(doc, child_chars=150, parent_chars=1200, overlap_chars=30)

    parsed_dollars = sum(e.text.count("$") for e in doc.elements)
    assert parsed_dollars == FORMULA_MD.count("$")  # 解析不丢公式
    assert sum(p.text.count("$") for p in parents) == parsed_dollars  # 分块后 '$' 计数不变
    children = [c for p in parents for c in p.children]
    assert children
    assert all(c.text.count("$") % 2 == 0 for c in children)  # 公式没有被截断
    assert all(c.text.strip() for c in children)
