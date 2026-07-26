"""仿真-生产阈值同源守护 (G6 收口的最后一环)。

thresholds.py 的 docstring 声称"语义锚定 eval/simulation/policies.py 的自适应臂"——
闭环实验 (26.0 题 vs 随机 80.9, p=0.0020) 的结论要迁移到产品行为, 前提是两侧的
难度档位与 ZPD 边界数值一致。eval/ 不是安装包无法反向 import, 单靠注释无约束力,
任何一侧单独调参都会静默漂移; 本测试把"同源"从口头主张变成 CI 断言。
"""

from __future__ import annotations

import sys
from pathlib import Path

from eduagents_core.mastery.thresholds import (
    ADAPTIVE_EASY_BELOW,
    MASTERED_THRESHOLD,
    ZPD_LOWER,
    difficulty_for_mastery,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from simulation import policies  # noqa: E402


def test_zpd_bounds_match_simulation():
    assert ADAPTIVE_EASY_BELOW == policies.ADAPTIVE_EASY_BELOW
    assert ZPD_LOWER == policies.ZPD_LOWER
    # 仿真毕业阈值是 build_policy 的默认参 (实验按默认档跑), 生产达标线必须与其一致
    import inspect

    sim_threshold = inspect.signature(policies.build_policy).parameters["threshold"].default
    assert MASTERED_THRESHOLD == sim_threshold


def test_difficulty_banding_matches_adaptive_arm():
    """逐档对照: 生产 difficulty_for_mastery 与自适应臂**真方法** _select_difficulty 同分档。

    直接调仿真臂 (不复刻逻辑), 覆盖边界值本身与两侧邻域 —— 任何一侧改分档立刻红。
    """
    import numpy as np

    policy = policies.build_policy("adaptive", np.random.default_rng(0))
    probes = [0.0, 0.29, 0.3, 0.31, 0.49, 0.5, 0.51, 0.6, 0.79, 0.8, 0.81, 1.0]
    for p in probes:
        state = policies.KcState(kc_id=0, p_mastery=p, attempts=0, graduated=False)
        assert difficulty_for_mastery(p) == policy._select_difficulty(state, []), (
            f"p={p} 生产与仿真分档不一致"
        )
