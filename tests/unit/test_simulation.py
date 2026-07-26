"""M5 合成学生仿真单测: IRT 性质 / 复现性 / 策略契约 / 动态学习 / 毕业判定 / 统计手算。

eval/ 不是安装包, 把 eval/ 目录加进 sys.path 后按 simulation.* 导入。
统计函数校验全部用小样本手算值 (推导见各用例注释), 不依赖 scipy。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from simulation.policies import (  # noqa: E402
    ARMS,
    DIFFICULTIES,
    KcState,
    build_policy,
)
from simulation.stats import (  # noqa: E402
    cohens_d_paired,
    mean_ci,
    spearman_rho,
    wilcoxon_signed_rank,
)
from simulation.student import (  # noqa: E402
    GUESS_RATE,
    QTYPES,
    SyntheticStudent,
    irt_p_correct,
    make_cohort,
    sample_theta,
)

sys.path.insert(0, str(EVAL_DIR / "simulation"))
import run_experiment  # noqa: E402  (依赖 eval/ 与 eval/simulation 均在 sys.path)

# ---------- 1. IRT 概率单调性 ----------


def test_irt_monotonic_in_theta_and_difficulty():
    thetas = [0.1, 0.3, 0.5, 0.7, 0.9]
    for qtype in QTYPES:
        for diff in DIFFICULTIES:
            ps = [irt_p_correct(t, diff, qtype) for t in thetas]
            assert ps == sorted(ps), f"θ↑ 应 P↑: {qtype}/{diff}"
            assert all(p < q for p, q in zip(ps, ps[1:], strict=False)), "应严格递增"
    for theta in thetas:
        for qtype in QTYPES:
            p_easy = irt_p_correct(theta, "easy", qtype)
            p_mid = irt_p_correct(theta, "mid", qtype)
            p_hard = irt_p_correct(theta, "hard", qtype)
            assert p_easy > p_mid > p_hard, "难度 b↑ 应 P↓"


# ---------- 2. 猜对率下界 (θ 极低时 P≈c) 与上界 ----------


def test_irt_guess_floor_and_upper_bound():
    for qtype, c in GUESS_RATE.items():
        p_low = irt_p_correct(1e-6, "hard", qtype)
        assert p_low == pytest.approx(c, abs=1e-3), f"θ→0 时 P 应收敛到 c={c}"
        p_high = irt_p_correct(1.0 - 1e-9, "easy", qtype)
        assert c <= p_high <= 1.0
        assert p_high > 0.999
    # 手算一个精确点: θ=0.5 (z=0), mid (b=0), single → P = 0.25 + 0.75·0.5 = 0.625
    assert irt_p_correct(0.5, "mid", "single") == pytest.approx(0.625, abs=1e-12)


# ---------- 3. 同 seed 完全复现 ----------


def test_same_seed_reproduces_bitwise():
    cfg = run_experiment.Config(n_students=6, n_kcs=4, max_questions=60)
    r1 = run_experiment.run_arm_b(cfg, seed=3, arm="adaptive")
    r2 = run_experiment.run_arm_b(cfg, seed=3, arm="adaptive")
    assert r1 == r2, "同 seed 同臂两次运行必须逐位一致"
    r3 = run_experiment.run_arm_b(cfg, seed=4, arm="adaptive")
    assert r1 != r3, "不同 seed 应产生不同轨迹"


# ---------- 4. 三策略接口契约 ----------


def test_policy_interface_contract():
    states = [
        KcState(kc_id=0, p_mastery=0.2, attempts=1, graduated=False),
        KcState(kc_id=1, p_mastery=0.5, attempts=3, graduated=False),
        KcState(kc_id=2, p_mastery=0.9, attempts=7, graduated=True),
    ]
    seen: dict[str, set[int]] = {}
    for arm in ARMS:
        policy = build_policy(arm, np.random.default_rng(0), threshold=0.8)
        seen[arm] = set()
        for _ in range(50):
            kc, diff, qtype = policy.select(states, [])
            assert kc in (0, 1, 2)
            assert diff in DIFFICULTIES
            assert qtype in QTYPES
            seen[arm].add(kc)
    # 只有 adaptive 读 BKT 状态: KC0 (0.2) 低于 ZPD 下界、KC2 已毕业, 均被跳过 → 锁定 KC1
    assert seen["adaptive"] == {1}, "adaptive 应锁定 ZPD 内弱项且不选已毕业/未入区 KC"
    assert 2 in seen["random"], "random 是估计盲基线, 不应过滤已毕业 KC"
    ladder = build_policy("ladder", np.random.default_rng(0), threshold=0.8)
    assert ladder.select(states, [])[0] == 0, "ladder 按已练题数轮转 (估计盲)"
    with pytest.raises(ValueError):
        build_policy("nonexistent", np.random.default_rng(0))
    with pytest.raises(ValueError):
        build_policy("adaptive", np.random.default_rng(0)).select([], [])


def test_adaptive_difficulty_matches_mastery():
    # 档位锚定毕业阈值: <0.5→easy, <阈值→mid, ≥阈值→hard (全员达标后的挑战档)
    policy = build_policy("adaptive", np.random.default_rng(0), threshold=0.8)
    assert policy.select([KcState(0, 0.35, 1, False)], [])[1] == "easy"
    assert policy.select([KcState(0, 0.55, 1, False)], [])[1] == "mid"
    assert policy.select([KcState(0, 0.75, 1, False)], [])[1] == "mid"
    assert policy.select([KcState(0, 0.85, 1, True)], [])[1] == "hard"


# ---------- 5. 动态 θ 单调不降且不超 θ_max ----------


def test_dynamic_theta_monotone_and_capped():
    stu = SyntheticStudent(
        theta=np.array([0.3, 0.9]),
        rng=np.random.default_rng(7),
        dynamic=True,
        learning_rate=0.15,
        theta_max=0.95,
        prereqs=((), ()),
    )
    prev = stu.theta.copy()
    for _ in range(300):
        stu.answer(0, "easy", "single")
        stu.answer(1, "easy", "single")
        assert np.all(stu.theta >= prev - 1e-12), "θ 必须单调不降"
        assert np.all(stu.theta <= 0.95 + 1e-12), "θ 不得超过 θ_max"
        prev = stu.theta.copy()
    assert stu.theta[0] == pytest.approx(0.95, abs=1e-6), "长期练习应收敛到 θ_max"


def test_prereq_gate_halves_growth():
    # KC1 的前置 KC0 θ=0.2 < 0.5 → KC1 增长率减半
    gated = SyntheticStudent(
        theta=np.array([0.2, 0.5]),
        rng=np.random.default_rng(0),
        dynamic=True,
        prereqs=((), (0,)),
    )
    free = SyntheticStudent(
        theta=np.array([0.9, 0.5]),
        rng=np.random.default_rng(0),
        dynamic=True,
        prereqs=((), (0,)),
    )
    gated.apply_learning(1, correct=True)
    free.apply_learning(1, correct=True)
    gain_gated = gated.theta[1] - 0.5
    gain_free = free.theta[1] - 0.5
    assert gain_gated == pytest.approx(gain_free / 2, rel=1e-9)
    # 静态模式不学习
    static = SyntheticStudent(theta=np.array([0.5]), rng=np.random.default_rng(0), dynamic=False)
    static.answer(0, "easy", "single")
    assert static.theta[0] == 0.5


# ---------- 6. 毕业判定逻辑 ----------


def test_graduation_logic():
    assert run_experiment.all_graduated(np.array([0.81, 0.9, 0.99]), 0.8)
    assert not run_experiment.all_graduated(np.array([0.81, 0.79]), 0.8)
    assert not run_experiment.all_graduated(np.array([0.8, 0.9]), 0.8), "阈值须严格大于"
    # runner 层: 上限截断 + 未毕业按上限计
    cfg = run_experiment.Config(n_students=4, n_kcs=3, max_questions=15, threshold=0.99)
    res = run_experiment.run_arm_b(cfg, seed=0, arm="random")
    assert all(q <= 15 for q in res["questions"])
    assert all(q == 15 for q, g in zip(res["questions"], res["graduated"], strict=True) if not g)


# ---------- 7. Wilcoxon 手算校验 ----------


def test_wilcoxon_exact_hand_computed():
    # 差值 [1,-2,3,-4,5,6]: |d| 秩 = 值本身, W+ = 1+3+5+6 = 15, W- = 2+4 = 6, W = 6, n = 6。
    # 秩和 ≤ 6 的子集数: sum 0..6 → 1+1+1+2+2+3+4 = 14; p = 2·14/2^6 = 0.4375。
    res = wilcoxon_signed_rank([1, -2, 3, -4, 5, 6], [0, 0, 0, 0, 0, 0])
    assert res.method == "exact"
    assert res.statistic == 6.0
    assert res.p_value == pytest.approx(0.4375, abs=1e-12)
    # 全正差值 [1..5]: W = 0, p = 2·(1/32) = 0.0625 (n=5 双尾最小 p)
    res2 = wilcoxon_signed_rank([1, 2, 3, 4, 5], [0, 0, 0, 0, 0])
    assert res2.p_value == pytest.approx(0.0625, abs=1e-12)
    # n=10 全同向: p = 2/1024 < 0.05 → 实验主检验在一致优势下可达显著
    res3 = wilcoxon_signed_rank(list(range(1, 11)), [0] * 10)
    assert res3.p_value == pytest.approx(2 / 1024, abs=1e-12)
    # 全零差值: 无证据, p = 1
    res4 = wilcoxon_signed_rank([1, 2], [1, 2])
    assert res4.p_value == 1.0 and res4.n_effective == 0


# ---------- 8. Cohen's d 手算校验 ----------


def test_cohens_d_hand_computed():
    # 差值 [1,2,3,4,5]: mean=3, sd=√2.5 → d = 3/√2.5 = 1.8973665961…
    d = cohens_d_paired([2, 4, 6, 8, 10], [1, 2, 3, 4, 5])
    assert d == pytest.approx(3 / math.sqrt(2.5), abs=1e-12)
    assert cohens_d_paired([1, 2], [1, 2]) == 0.0
    with pytest.raises(ValueError):
        cohens_d_paired([1], [2])


# ---------- 9. t 分布 95% CI 手算校验 ----------


def test_mean_ci_hand_computed():
    # [1..5]: mean=3, sem=√(2.5/5)=0.7071068, t(4)=2.776 → 3 ± 1.962932
    mean, lo, hi = mean_ci([1, 2, 3, 4, 5])
    assert mean == pytest.approx(3.0)
    assert lo == pytest.approx(3 - 2.776 * math.sqrt(0.5), abs=1e-9)
    assert hi == pytest.approx(3 + 2.776 * math.sqrt(0.5), abs=1e-9)
    m, lo1, hi1 = mean_ci([7.0])
    assert (m, lo1, hi1) == (7.0, 7.0, 7.0)


# ---------- 9b. Spearman 秩相关手算校验 ----------


def test_spearman_hand_computed():
    # x 秩 [1,2,3,4], y=[10,30,20,40] 秩 [1,3,2,4]: ρ = 1 - 6·(0+1+1+0)/(4·15) = 0.8
    assert spearman_rho([1, 2, 3, 4], [10, 30, 20, 40]) == pytest.approx(0.8, abs=1e-12)
    assert spearman_rho([1, 2, 3], [7, 8, 9]) == pytest.approx(1.0)
    assert spearman_rho([1, 2, 3], [9, 8, 7]) == pytest.approx(-1.0)
    assert math.isnan(spearman_rho([1, 1, 1], [1, 2, 3])), "常数列无定义"


# ---------- 10. 配对设计: 同 seed 三臂共享学生真值 ----------


def test_paired_design_shares_theta_across_arms():
    rng_a = np.random.default_rng([5, 0])
    rng_b = np.random.default_rng([5, 0])
    theta_a = sample_theta(rng_a, 30, 8)
    theta_b = sample_theta(rng_b, 30, 8)
    np.testing.assert_array_equal(theta_a, theta_b)
    assert theta_a.min() >= 0.1 and theta_a.max() <= 0.9
    cohort = make_cohort(theta_a, seed=5, dynamic=True)
    assert len(cohort) == 30 and cohort[0].n_kcs == 8


# ---------- 11. runner 冒烟: 小配置产出结构完整 ----------


def test_runner_smoke_small_config():
    cfg = run_experiment.Config(n_students=5, n_kcs=3, t_rounds=10, max_questions=40)
    seeds = [0, 1]
    exp_a = run_experiment.run_experiment_a(cfg, seeds)
    for arm in ARMS:
        curves = np.asarray(exp_a[arm]["mae_curves"])
        assert curves.shape == (2, 11)
        assert np.all((curves >= 0) & (curves <= 1))
        assert len(exp_a[arm]["theta"]) == 2 * 5 * 3
    exp_b = run_experiment.run_experiment_b(cfg, seeds)
    for arm in ARMS:
        assert len(exp_b["arms"][arm]["seed_mean_questions"]) == 2
        assert 0 < exp_b["arms"][arm]["mean"] <= 40
    assert "adaptive_vs_random" in exp_b["comparisons"]
    assert 0.0 <= exp_b["comparisons"]["adaptive_vs_random"]["p_value"] <= 1.0
