"""选题策略 (智能体侧) — 三条对照臂。

统一接口::

    policy.select(kc_states, history) -> (kc_id, difficulty, qtype)

任何臂都看不到学生真值 θ 与题型真实猜对率 c。**只有 adaptive 臂读取 BKT 估计**;
random/ladder 是估计盲 (estimate-blind) 基线 —— 若让基线也按"已达标 KC 不再出题"
过滤, 就等于把 BKT 信号泄漏给对照组, 对照差异将无法归因于"BKT 驱动"本身。
毕业判定 (全 KC P(L)>阈值 即停) 是**环境级**规则, 三臂一致, 不属于策略知识。

- ``adaptive``: 产品策略 (唯一 BKT 驱动)。跳过已达标 KC; 优先"最近发展区"
  0.3 < P(L) < 阈值 内的弱项 (P(L)≤0.3 地基未打好, 两端教学收益低);
  难度匹配当前估计: P(L)<0.5→easy, <阈值→mid, ≥阈值→hard —— 档位锚定产品
  掌握阈值, 未达标不派最难档 (脚手架原则, 同辅导子图提示阶梯的设计)。
- ``random``: 随机基线。全 KC 均匀随机 + 难度均匀随机, 零状态。
- ``ladder``: 非自适应启发式基线。KC 轮转 (练得最少者优先) + 固定
  easy→mid→hard 阶梯 (按该 KC 已练题数每 5 题升一档)。用于回应"你只是赢了随机"。

题型对三条臂**同分布随机抽取**, 不作为策略变量 —— 否则自适应臂可以靠专挑低猜对率
题型 (多选/填空) 来提高 BKT 信息量, 那样的胜利不可归因于教学逻辑。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from simulation.student import DIFFICULTIES, QTYPES

#: 自适应难度匹配: P(L) < 0.5 派 easy; 0.5 ≤ P(L) < 阈值 派 mid; ≥ 阈值 派 hard。
#: 上界锚定毕业阈值 (产品语义), 不是自由参数。
ADAPTIVE_EASY_BELOW = 0.5

#: 最近发展区下界 (上界 = 毕业阈值, 由构造参数传入)
ZPD_LOWER = 0.3

#: ladder 策略每档难度的题数
LADDER_STEP = 5


@dataclass(frozen=True)
class KcState:
    """策略可见的单个 KC 状态 (纯估计量, 无真值)。"""

    kc_id: int
    p_mastery: float
    attempts: int
    graduated: bool


@dataclass(frozen=True)
class Attempt:
    """一次作答记录 (策略可见的历史)。"""

    kc_id: int
    difficulty: str
    qtype: str
    correct: bool


Selection = tuple[int, str, str]


class Policy(ABC):
    """选题策略基类。子类只实现 ``_select_kc`` 与 ``_select_difficulty``。"""

    name: str = "base"

    def __init__(self, rng: np.random.Generator, threshold: float = 0.8) -> None:
        self.rng = rng
        self.threshold = threshold

    def select(self, kc_states: list[KcState], history: list[Attempt]) -> Selection:
        """返回 (kc_id, difficulty, qtype)。kc_states 非空时必然返回合法三元组。

        基类不做任何基于估计量的过滤 —— graduated/p_mastery 只有 adaptive 读取,
        基线臂保持估计盲 (见模块 docstring)。
        """
        if not kc_states:
            raise ValueError("kc_states 不能为空")
        state = self._select_kc(list(kc_states), history)
        difficulty = self._select_difficulty(state, history)
        return state.kc_id, difficulty, self._select_qtype()

    def _select_qtype(self) -> str:
        return str(QTYPES[int(self.rng.integers(len(QTYPES)))])

    @abstractmethod
    def _select_kc(self, candidates: list[KcState], history: list[Attempt]) -> KcState: ...

    @abstractmethod
    def _select_difficulty(self, state: KcState, history: list[Attempt]) -> str: ...


class AdaptivePolicy(Policy):
    """BKT 驱动 (唯一读估计量的臂): 跳过已达标 + ZPD 优先 + 难度匹配。"""

    name = "adaptive"

    def _select_kc(self, candidates: list[KcState], history: list[Attempt]) -> KcState:
        pending = [s for s in candidates if not s.graduated] or candidates
        zpd = [s for s in pending if ZPD_LOWER < s.p_mastery < self.threshold]
        pool = zpd or pending
        # 弱项优先 (掌握度最低者先补), 同分按 kc_id 稳定排序 —— 完全确定, 不消耗随机数
        return min(pool, key=lambda s: (s.p_mastery, s.kc_id))

    def _select_difficulty(self, state: KcState, history: list[Attempt]) -> str:
        if state.p_mastery < ADAPTIVE_EASY_BELOW:
            return "easy"
        if state.p_mastery < self.threshold:
            return "mid"
        return "hard"


class RandomPolicy(Policy):
    """随机基线 (估计盲): 全 KC 均匀随机, 难度均匀随机。"""

    name = "random"

    def _select_kc(self, candidates: list[KcState], history: list[Attempt]) -> KcState:
        return candidates[int(self.rng.integers(len(candidates)))]

    def _select_difficulty(self, state: KcState, history: list[Attempt]) -> str:
        return str(DIFFICULTIES[int(self.rng.integers(len(DIFFICULTIES)))])


class LadderPolicy(Policy):
    """难度阶梯基线 (估计盲): KC 轮转 + 固定 easy→mid→hard。"""

    name = "ladder"

    def _select_kc(self, candidates: list[KcState], history: list[Attempt]) -> KcState:
        # 练得最少的 KC 优先 = round-robin (不看 graduated/p_mastery), 同分按 kc_id
        return min(candidates, key=lambda s: (s.attempts, s.kc_id))

    def _select_difficulty(self, state: KcState, history: list[Attempt]) -> str:
        if state.attempts < LADDER_STEP:
            return "easy"
        if state.attempts < 2 * LADDER_STEP:
            return "mid"
        return "hard"


POLICY_REGISTRY: dict[str, type[Policy]] = {
    "adaptive": AdaptivePolicy,
    "random": RandomPolicy,
    "ladder": LadderPolicy,
}

#: 报告与图表中三条臂的固定顺序
ARMS: tuple[str, ...] = ("adaptive", "random", "ladder")


def build_policy(name: str, rng: np.random.Generator, threshold: float = 0.8) -> Policy:
    """按名字构造策略; 未知名字直接抛错 (禁止静默回落)。"""
    try:
        cls = POLICY_REGISTRY[name]
    except KeyError:
        raise ValueError(f"未知策略: {name!r} (合法值 {tuple(POLICY_REGISTRY)})") from None
    return cls(rng=rng, threshold=threshold)
