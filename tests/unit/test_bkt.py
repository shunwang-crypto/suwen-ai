"""BKT 纯函数单元测试 (§5.6): 手算精确值 + 单调性 + 权重插值 + 衰减 + 边界。

手算基准 (默认参数 p_l0=0.3, p_t=0.2, slip=0.1, guess=0.2, 从 p=0.3 出发):
- 答对: posterior = 0.3*0.9 / (0.3*0.9 + 0.7*0.2) = 0.27/0.41 = 27/41
        更新   = 27/41 + (14/41)*0.2 = 29.8/41 ≈ 0.7268292683
- 答错: posterior = 0.3*0.1 / (0.3*0.1 + 0.7*0.8) = 0.03/0.59 = 3/59
        更新   = 3/59 + (56/59)*0.2 = 14.2/59 ≈ 0.2406779661
"""

from __future__ import annotations

import math

import pytest
from eduagents_core.mastery.bkt import BktParams, bkt_update, decay

CORRECT_FROM_030 = 29.8 / 41  # ≈ 0.7268292683
INCORRECT_FROM_030 = 14.2 / 59  # ≈ 0.2406779661


def test_correct_once_from_prior_exact_value():
    assert bkt_update(0.3, True) == pytest.approx(CORRECT_FROM_030, abs=1e-12)


def test_incorrect_once_from_prior_exact_value():
    assert bkt_update(0.3, False) == pytest.approx(INCORRECT_FROM_030, abs=1e-12)


def test_consecutive_correct_monotonically_increases_below_one():
    p = 0.3
    for _ in range(6):
        new_p = bkt_update(p, True)
        assert new_p > p
        assert new_p < 1.0
        p = new_p
    assert p > 0.99  # 连对 6 次应逼近满掌握


def test_consecutive_incorrect_monotonically_decreases():
    p = 0.9
    for _ in range(4):
        new_p = bkt_update(p, False)
        assert new_p < p
        assert new_p > 0.0
        p = new_p


def test_weight_half_interpolates_between_original_and_full():
    full = bkt_update(0.3, True)
    half = bkt_update(0.3, True, weight=0.5)
    assert 0.3 < half < full
    assert half == pytest.approx(0.3 + (full - 0.3) * 0.5, abs=1e-12)


def test_weight_extremes():
    assert bkt_update(0.3, True, weight=0.0) == pytest.approx(0.3)  # 零权重不动
    assert bkt_update(0.3, True, weight=1.0) == pytest.approx(CORRECT_FROM_030)
    # 负权重按 0 处理, 不反向更新
    assert bkt_update(0.3, True, weight=-1.0) == pytest.approx(0.3)


def test_custom_params_used():
    # slip=0 guess=0 p_t=0: 答对必然掌握, 无转移
    params = BktParams(p_l0=0.3, p_t=0.0, slip=0.0, guess=0.0)
    assert bkt_update(0.5, True, params) == pytest.approx(1.0)
    assert bkt_update(0.5, False, params) == pytest.approx(0.0)


def test_decay_ten_days_regresses_toward_prior():
    decayed = decay(0.9, 10.0)  # 0.3 + 0.6*e^{-0.2}
    assert decayed == pytest.approx(0.3 + 0.6 * math.exp(-0.2), abs=1e-12)
    assert 0.3 < decayed < 0.9


def test_decay_never_below_prior_floor():
    assert decay(0.9, 10_000.0) == pytest.approx(0.3, abs=1e-9)
    assert decay(0.9, 10_000.0) >= 0.3
    assert decay(0.3, 50.0) == pytest.approx(0.3)


def test_decay_below_prior_stays_and_zero_days_identity():
    assert decay(0.15, 30.0) == pytest.approx(0.15)  # 低于先验不因遗忘反升
    assert decay(0.8, 0.0) == pytest.approx(0.8)  # 0 天不衰减
    assert decay(0.8, -5.0) == pytest.approx(0.8)  # 负天数按 0 处理


def test_boundary_probabilities_no_nan():
    for p in (0.0, 1.0):
        for correct in (True, False):
            result = bkt_update(p, correct)
            assert not math.isnan(result)
            assert 0.0 <= result <= 1.0
    assert bkt_update(0.0, True) == pytest.approx(0.2)  # posterior 0 -> 仅学习转移
    assert bkt_update(1.0, True) == pytest.approx(1.0)
    assert not math.isnan(decay(0.0, 10.0))
    assert not math.isnan(decay(1.0, 10.0))
