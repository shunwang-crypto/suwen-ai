"""eval/metrics.py 指标手算校验 + scripts/gen_corpus.py 冒烟测试。

eval/ 与 scripts/ 不是包, 按文件路径动态加载。
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metrics = _load_module("eval_metrics", "eval/metrics.py")
gen_corpus = _load_module("gen_corpus", "scripts/gen_corpus.py")


# ---------- metrics: 手算小例子 ----------

# q1: 相关结果在第 2 位; q2: 相关结果在第 1、3 位
MATRIX = [
    [False, True, False, False, False],
    [True, False, True, False, False],
]


def test_hit_rate_at_k_exact():
    assert metrics.hit_rate_at_k(MATRIX, 5) == 1.0
    assert metrics.hit_rate_at_k(MATRIX, 1) == 0.5  # 只有 q2 在 top1 命中
    # k 截断: 相关在第 3 位, k=2 不算命中
    assert metrics.hit_rate_at_k([[False, False, True]], 2) == 0.0


def test_mrr_exact():
    # q1 首个相关名次 2 -> 1/2; q2 名次 1 -> 1; 均值 0.75
    assert metrics.mrr(MATRIX) == pytest.approx(0.75)
    # 全不相关 -> 0
    assert metrics.mrr([[False, False]]) == 0.0


def test_ndcg_at_k_exact():
    # q1: DCG = 1/log2(3), IDCG = 1/log2(2) = 1
    ndcg_q1 = (1 / math.log2(3)) / 1.0
    # q2: DCG = 1/log2(2) + 1/log2(4) = 1.5, IDCG = 1 + 1/log2(3)
    ndcg_q2 = (1.0 + 1 / math.log2(4)) / (1.0 + 1 / math.log2(3))
    expected = (ndcg_q1 + ndcg_q2) / 2
    assert metrics.ndcg_at_k(MATRIX, 5) == pytest.approx(expected)

    # 理想排序 (相关全部在前) -> nDCG = 1
    assert metrics.ndcg_at_k([[True, True, False]], 3) == pytest.approx(1.0)
    # 全不相关 -> 0
    assert metrics.ndcg_at_k([[False, False, False]], 3) == 0.0


def test_metrics_empty_input():
    assert metrics.hit_rate_at_k([], 5) == 0.0
    assert metrics.mrr([]) == 0.0
    assert metrics.ndcg_at_k([], 10) == 0.0


# ---------- gen_corpus 冒烟 ----------


def test_gen_corpus_smoke(tmp_path):
    corpus_path, golden_path = gen_corpus.generate(tmp_path)

    assert corpus_path.exists()
    assert golden_path.exists()

    corpus = corpus_path.read_text(encoding="utf-8")
    # 总量 >= 8000 字
    assert len(corpus) >= 8000
    # 块级公式: $$ 成对且 >= 12 个 (即 >= 6 个块级公式)
    dollar_count = corpus.count("$$")
    assert dollar_count >= 12
    assert dollar_count % 2 == 0
    # 表格: 至少 3 条分隔行 (| --- | ... |)
    separator_rows = re.findall(r"^\|[\s:|-]+\|$", corpus, flags=re.MULTILINE)
    assert len(separator_rows) >= 3
    # 版本标记
    assert "v1-2026-07-26" in corpus

    heading_lines = [
        line.lstrip("#").strip() for line in corpus.splitlines() if line.startswith("#")
    ]
    assert len(heading_lines) >= 5 + 5 * 3  # 5 章 x >=3 小节 (+ 书名)

    golden_lines = [
        line for line in golden_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(golden_lines) >= 12
    qids = set()
    for line in golden_lines:
        row = json.loads(line)  # 每行必须可解析
        for key in (
            "qid",
            "question",
            "relevant_doc",
            "relevant_headings",
            "ground_truth",
            "version",
        ):
            assert key in row, f"golden 行缺字段 {key}: {row}"
        qids.add(row["qid"])
        assert row["version"] == "v1-2026-07-26"
        assert row["relevant_doc"] == "calculus.md"
        assert row["relevant_headings"], "relevant_headings 不能为空"
        assert row["ground_truth"].strip()
        # 每条 relevant_heading 必须真实出现在语料标题中
        for rh in row["relevant_headings"]:
            assert any(rh in heading for heading in heading_lines), (
                f"relevant_heading 未出现在语料标题中: {rh}"
            )
    assert len(qids) == len(golden_lines), "qid 必须唯一"
