"""检索评估指标 (纯函数, 无外部依赖)。

输入统一为 ``results: list[list[bool]]``:
外层每个元素对应一个问题, 内层为该问题的检索结果按排序位置
(名次 1 开始) 是否相关的布尔标记。
"""

from __future__ import annotations

import math


def hit_rate_at_k(results: list[list[bool]], k: int) -> float:
    """Hit Rate@k。

    公式::

        hit_rate@k = (1/N) * Σ_i 1[∃ j ≤ k, rel_{i,j} = 1]

    即前 k 个结果中至少命中一个相关 chunk 的问题占比。N = 0 时返回 0.0。
    """
    if not results:
        return 0.0
    hits = sum(1 for row in results if any(row[:k]))
    return hits / len(results)


def mrr(results: list[list[bool]]) -> float:
    """Mean Reciprocal Rank。

    公式::

        MRR = (1/N) * Σ_i 1 / rank_i

    rank_i 为第 i 个问题首个相关结果的名次 (1-based);
    整列无相关时该问题贡献 0。N = 0 时返回 0.0。
    """
    if not results:
        return 0.0
    total = 0.0
    for row in results:
        for rank, relevant in enumerate(row, start=1):
            if relevant:
                total += 1.0 / rank
                break
    return total / len(results)


def ndcg_at_k(results: list[list[bool]], k: int) -> float:
    """nDCG@k (二值相关度)。

    公式::

        DCG@k  = Σ_{j=1..k} rel_j / log2(j + 1)          (rel_j ∈ {0, 1})
        IDCG@k = 同列表理想排序 (相关全部排前) 的 DCG@k
        nDCG@k = DCG@k / IDCG@k                          (IDCG = 0 时记 0)

    对所有问题的 nDCG@k 取算术平均。N = 0 时返回 0.0。
    """
    if not results:
        return 0.0
    total = 0.0
    for row in results:
        dcg = sum(1.0 / math.log2(rank + 1) for rank, rel in enumerate(row[:k], start=1) if rel)
        n_relevant = sum(1 for rel in row if rel)
        idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(n_relevant, k) + 1))
        total += dcg / idcg if idcg > 0 else 0.0
    return total / len(results)
