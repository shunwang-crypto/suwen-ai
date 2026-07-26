"""配对统计检验纯函数 (零 scipy 依赖, 便于 CI 与离线复现)。

- ``wilcoxon_signed_rank``: 配对样本的非参数检验。n ≤ 25 且无 |d| 并列时走**精确分布**
  (动态规划枚举符号秩和), 否则走带并列校正的正态近似。
- ``cohens_d_paired``: 配对设计的效应量 mean(d) / sd(d), ddof=1。
- ``mean_ci``: 基于 t 分布的均值 95% 置信区间 (内置双尾临界值表, df ≤ 30 精确, 更大用 1.96)。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

#: 双尾 95% t 临界值, 索引 = 自由度 (df=0 占位)
_T_CRIT_95: tuple[float, ...] = (
    float("nan"),
    12.706,
    4.303,
    3.182,
    2.776,
    2.571,
    2.447,
    2.365,
    2.306,
    2.262,
    2.228,
    2.201,
    2.179,
    2.160,
    2.145,
    2.131,
    2.120,
    2.110,
    2.101,
    2.093,
    2.086,
    2.080,
    2.074,
    2.069,
    2.064,
    2.060,
    2.056,
    2.052,
    2.048,
    2.045,
    2.042,
)

#: 超出上表用正态临界值
_Z_CRIT_95 = 1.959964


@dataclass(frozen=True)
class WilcoxonResult:
    statistic: float  # W = min(W+, W-)
    p_value: float
    n_effective: int  # 剔除零差值后的样本数
    method: str  # "exact" | "normal-approx"


def _average_ranks(values: Sequence[float]) -> list[float]:
    """并列取平均秩 (1 起)。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _exact_two_sided_p(w: int, n: int) -> float:
    """符号秩和精确双尾 p: 2·P(W ≤ w), 秩为 1..n 无并列。

    counts[s] = 恰好取到秩和 s 的符号组合数 (共 2^n 种, 等概率)。
    """
    total = n * (n + 1) // 2
    counts = [0] * (total + 1)
    counts[0] = 1
    for rank in range(1, n + 1):
        for s in range(total, rank - 1, -1):
            counts[s] += counts[s - rank]
    tail = sum(counts[: w + 1])
    p = 2.0 * tail / (2.0**n)
    return min(p, 1.0)


def wilcoxon_signed_rank(x: Sequence[float], y: Sequence[float]) -> WilcoxonResult:
    """配对 Wilcoxon 符号秩检验 (双尾, H0: 差值分布中位数为 0)。

    全部差值为 0 时返回 p=1.0 (无任何证据), 不抛错。
    """
    if len(x) != len(y):
        raise ValueError("配对检验要求两组样本等长")
    diffs = [float(a) - float(b) for a, b in zip(x, y, strict=True)]
    nonzero = [d for d in diffs if d != 0.0]
    n = len(nonzero)
    if n == 0:
        return WilcoxonResult(statistic=0.0, p_value=1.0, n_effective=0, method="exact")

    abs_d = [abs(d) for d in nonzero]
    ranks = _average_ranks(abs_d)
    w_plus = sum(r for r, d in zip(ranks, nonzero, strict=True) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, nonzero, strict=True) if d < 0)
    w = min(w_plus, w_minus)

    has_ties = len(set(abs_d)) != n
    if n <= 25 and not has_ties:
        return WilcoxonResult(
            statistic=w,
            p_value=_exact_two_sided_p(int(round(w)), n),
            n_effective=n,
            method="exact",
        )

    mean_w = n * (n + 1) / 4.0
    # 并列校正项: Σ(t^3 - t) / 48
    tie_groups: dict[float, int] = {}
    for v in abs_d:
        tie_groups[v] = tie_groups.get(v, 0) + 1
    tie_term = sum(t**3 - t for t in tie_groups.values()) / 48.0
    var_w = n * (n + 1) * (2 * n + 1) / 24.0 - tie_term
    if var_w <= 0:
        return WilcoxonResult(statistic=w, p_value=1.0, n_effective=n, method="normal-approx")
    z = (w - mean_w + 0.5) / math.sqrt(var_w)  # 连续性校正 (w 在均值下方)
    p = 2.0 * 0.5 * math.erfc(-z / math.sqrt(2.0)) if z < 0 else 2.0 * (1.0 - _norm_cdf(z))
    return WilcoxonResult(
        statistic=w, p_value=min(max(p, 0.0), 1.0), n_effective=n, method="normal-approx"
    )


def _norm_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def cohens_d_paired(x: Sequence[float], y: Sequence[float]) -> float:
    """配对 Cohen's d = mean(x-y) / sd(x-y), sd 用 ddof=1。差值全同 (sd=0) 返回 0.0/±inf 的安全值。"""
    if len(x) != len(y):
        raise ValueError("配对效应量要求两组样本等长")
    diffs = [float(a) - float(b) for a, b in zip(x, y, strict=True)]
    n = len(diffs)
    if n < 2:
        raise ValueError("样本量不足以计算效应量 (n < 2)")
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0.0:
        return 0.0 if mean == 0.0 else math.copysign(float("inf"), mean)
    return mean / sd


def mean_ci(values: Sequence[float], confidence: float = 0.95) -> tuple[float, float, float]:
    """返回 (mean, lo, hi)。当前仅支持 95%; 样本 <2 时区间退化为点估计。"""
    if confidence != 0.95:
        raise ValueError("当前仅内置 95% 临界值表")
    n = len(values)
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    mean = sum(float(v) for v in values) / n
    if n < 2:
        return (mean, mean, mean)
    var = sum((float(v) - mean) ** 2 for v in values) / (n - 1)
    sem = math.sqrt(var / n)
    df = n - 1
    crit = _T_CRIT_95[df] if df < len(_T_CRIT_95) else _Z_CRIT_95
    return (mean, mean - crit * sem, mean + crit * sem)


def spearman_rho(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman 秩相关 (并列取平均秩后对秩做 Pearson)。"""
    if len(x) != len(y):
        raise ValueError("秩相关要求两组样本等长")
    n = len(x)
    if n < 2:
        return float("nan")
    rx = _average_ranks([float(v) for v in x])
    ry = _average_ranks([float(v) for v in y])
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0.0 or vy == 0.0:
        return float("nan")
    return cov / math.sqrt(vx * vy)


def r_squared(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """针对 y=x 对角线的 R² (校准图用): 1 - SS_res/SS_tot, SS_res 相对 45° 线。"""
    if len(y_true) != len(y_pred):
        raise ValueError("R² 要求两组样本等长")
    n = len(y_true)
    if n == 0:
        return float("nan")
    mean_true = sum(float(v) for v in y_true) / n
    ss_tot = sum((float(v) - mean_true) ** 2 for v in y_true)
    ss_res = sum(
        (float(a) - float(b)) ** 2 for a, b in zip(y_true, y_pred, strict=True)
    )
    if ss_tot == 0.0:
        return float("nan")
    return 1.0 - ss_res / ss_tot
