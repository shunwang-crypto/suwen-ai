"""BKT 贝叶斯知识追踪纯函数 (§5.6)。

四参数模型: P(L0) 初始掌握概率 / P(T) 学习转移概率 / slip 会而做错 / guess 不会而蒙对。
本模块只做数学, 无 IO 无时间源 — days_since_seen 由调用方经 Clock 抽象换算后传入。

更新公式 (correct 观测):
    P(L|obs) = P(L)(1-slip) / (P(L)(1-slip) + (1-P(L))·guess)
incorrect 观测 (对称):
    P(L|obs) = P(L)·slip / (P(L)·slip + (1-P(L))(1-guess))
再应用学习转移:
    P(L)' = P(L|obs) + (1 - P(L|obs))·P(T)
证据权重 weight<1 时 (主观题 0.5 权重规则 §5.6), 结果向原 p_mastery 线性插值:
    result = p_mastery + (P(L)' - p_mastery)·weight
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BktParams:
    """BKT 四参数 (§5.6 写死默认值)。"""

    p_l0: float = 0.3
    p_t: float = 0.2
    slip: float = 0.1
    guess: float = 0.2


def _clamp01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def bkt_update(
    p_mastery: float,
    correct: bool,
    params: BktParams = BktParams(),
    weight: float = 1.0,
) -> float:
    """单次作答证据的贝叶斯更新, 返回新掌握度 (始终落在 [0,1], 边界不出 NaN)。"""
    p = _clamp01(p_mastery)
    if correct:
        numerator = p * (1.0 - params.slip)
        denominator = numerator + (1.0 - p) * params.guess
    else:
        numerator = p * params.slip
        denominator = numerator + (1.0 - p) * (1.0 - params.guess)
    posterior = numerator / denominator if denominator > 0.0 else p
    updated = posterior + (1.0 - posterior) * params.p_t
    if weight < 1.0:
        updated = p + (updated - p) * max(weight, 0.0)
    return _clamp01(updated)


def decay(
    p_mastery: float,
    days_since_seen: float,
    rate: float = 0.02,
    p_l0: float = 0.3,
) -> float:
    """掌握度随时间指数衰减, 向先验 p_l0 回归, 下界 p_l0。

    公式: p' = p_l0 + (p - p_l0)·exp(-rate·days_since_seen)。
    已低于 p_l0 的值不因"遗忘"反而升高, 原样返回。
    """
    p = _clamp01(p_mastery)
    if p <= p_l0:
        return p
    days = max(days_since_seen, 0.0)
    return p_l0 + (p - p_l0) * math.exp(-rate * days)
