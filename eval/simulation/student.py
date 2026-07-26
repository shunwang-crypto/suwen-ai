"""合成学生 — 实验环境侧的**作答模型**与**学习模型**。

作答模型采用 IRT (项目反应理论) 三参数族, 与产品侧的 BKT 估计器**结构异构、参数不共享**:

    P(答对) = c + (1 - c) · sigmoid(1.7 · (z - b)),   z = logit(θ) = ln(θ / (1-θ))

- θ ∈ (0,1): 学生在某 KC 上的**真值掌握度**, 连续潜变量, 策略与估计器永远看不到。
  先取 logit 再进 sigmoid 是为了统一量纲 —— θ 与 BKT 的 P(L) 同在 [0,1] 上,
  MAE(P(L), θ) 才是可比的; 同时 z 的值域约 ±2.2 恰好落在标准 IRT 能力尺度上,
  若直接把 θ∈(0.1,0.9) 代入 sigmoid(1.7(θ-b)), 最差学生做 easy 题正确率仍有 0.87, 区分度失效。
- b: 题目难度, easy=-1 / mid=0 / hard=+1。
- c: 题型异质的猜对率下界 (下渐近线), 见 ``GUESS_RATE``。

学习模型 (仅动态模式/实验 B 启用, 对三条策略臂完全一致且隐藏于环境内):

    θ_k ← θ_k + rate · (θ_max - θ_k)
    rate = λ (答对) / λ·0.5 (答错, 练习即有学习, doer effect)
    若该 KC 任一前置 KC 的 θ < 0.5, rate 再减半 (先修 DAG 约束)

随机源纪律 (§14 可复现): 本模块**不使用**全局 ``random`` / ``np.random`` 单例,
所有随机数来自显式传入的 ``numpy.random.Generator``, 同 seed 必然逐位复现。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# 题库参数 (作答模型专属, 估计器不知情)
# ---------------------------------------------------------------------------

#: 难度标签 → IRT 难度参数 b
DIFFICULTY_B: dict[str, float] = {"easy": -1.0, "mid": 0.0, "hard": 1.0}

DIFFICULTIES: tuple[str, ...] = ("easy", "mid", "hard")

#: 题型 → 猜对率 c (IRT 3PL 的下渐近线)。
#: 单选 4 选 1 = 0.25; 判断题二选一 = 0.5; 多选 (4 选 n 全对才得分) ≈ 0.06; 填空/简答 ≈ 0.05。
#: 估计器侧的 BKT 只有**一个**全局 guess=0.2, 不知道题型异质性 —— 这正是"参数不共享"的体现。
GUESS_RATE: dict[str, float] = {
    "single": 0.25,
    "judge": 0.5,
    "multi": 0.06,
    "blank": 0.05,
    "short": 0.05,
}

#: 默认题库题型 (客观题, 与 quiz_graph 的可程序判分类型对齐; short 不可程序判分故不入默认库)
QTYPES: tuple[str, ...] = ("single", "judge", "multi", "blank")

#: 8 个 KC 的先修 DAG (取自产品先修图的一条子图形态): 索引 → 前置 KC 索引元组
DEFAULT_PREREQS: tuple[tuple[int, ...], ...] = (
    (),  # 0 根
    (0,),  # 1
    (0,),  # 2
    (1,),  # 3
    (2,),  # 4
    (1, 3),  # 5
    (2, 4),  # 6
    (5, 6),  # 7 汇聚
)

#: 前置未达标 (θ < 该阈值) 时后继 KC 的学习率折减
PREREQ_THETA_GATE = 0.5
PREREQ_RATE_PENALTY = 0.5

_THETA_EPS = 1e-9


def logit(theta: float) -> float:
    """ln(θ/(1-θ)); θ 被夹到 (0,1) 开区间内, 边界不出 ±inf。"""
    p = min(max(float(theta), _THETA_EPS), 1.0 - _THETA_EPS)
    return math.log(p / (1.0 - p))


def sigmoid(z: float) -> float:
    """数值稳定的 logistic 函数。"""
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def irt_p_correct(theta: float, difficulty: str, qtype: str) -> float:
    """3PL IRT 答对概率: ``c + (1-c)·sigmoid(1.7·(logit(θ) - b))``。

    性质 (单元测试逐条断言):
    - 关于 θ 严格单调递增; 关于 b 严格单调递减;
    - θ → 0 时收敛到猜对率 c (下渐近线), θ → 1 时收敛到 1;
    - 取值恒在 [c, 1] 内。
    """
    if difficulty not in DIFFICULTY_B:
        raise ValueError(f"未知难度: {difficulty!r} (合法值 {tuple(DIFFICULTY_B)})")
    if qtype not in GUESS_RATE:
        raise ValueError(f"未知题型: {qtype!r} (合法值 {tuple(GUESS_RATE)})")
    c = GUESS_RATE[qtype]
    b = DIFFICULTY_B[difficulty]
    return c + (1.0 - c) * sigmoid(1.7 * (logit(theta) - b))


@dataclass
class SyntheticStudent:
    """一名合成学生: 持有每个 KC 的真值 θ, 按 IRT 作答, 动态模式下练习即学习。

    ``rng`` 必须由调用方显式构造 (如 ``np.random.default_rng([seed, student_idx])``),
    这样同一 seed 下不同策略臂拿到的是**同一批学生 + 同一条随机流**, 即配对设计。
    """

    theta: np.ndarray
    rng: np.random.Generator
    dynamic: bool = False
    learning_rate: float = 0.15
    theta_max: float = 0.95
    prereqs: tuple[tuple[int, ...], ...] = DEFAULT_PREREQS
    theta0: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self.theta = np.asarray(self.theta, dtype=float).copy()
        if self.theta.ndim != 1:
            raise ValueError("theta 必须是一维数组 (每个 KC 一个真值)")
        self.theta0 = self.theta.copy()

    @property
    def n_kcs(self) -> int:
        return int(self.theta.shape[0])

    def p_correct(self, kc_id: int, difficulty: str, qtype: str) -> float:
        """该学生在指定题目上的真实答对概率 (确定性, 不消耗随机数)。"""
        return irt_p_correct(float(self.theta[kc_id]), difficulty, qtype)

    def answer(self, kc_id: int, difficulty: str, qtype: str) -> bool:
        """作答一题: 抽一个 U(0,1) 判定对错; 动态模式下顺带应用学习增长。"""
        p = self.p_correct(kc_id, difficulty, qtype)
        correct = bool(self.rng.random() < p)
        if self.dynamic:
            self.apply_learning(kc_id, correct)
        return correct

    def apply_learning(self, kc_id: int, correct: bool) -> None:
        """练习后的真值成长; θ 单调不降且恒不超过 θ_max。"""
        rate = self.learning_rate if correct else self.learning_rate * 0.5
        if self._prereq_blocked(kc_id):
            rate *= PREREQ_RATE_PENALTY
        current = float(self.theta[kc_id])
        if current >= self.theta_max:
            return
        self.theta[kc_id] = min(current + rate * (self.theta_max - current), self.theta_max)

    def _prereq_blocked(self, kc_id: int) -> bool:
        if kc_id >= len(self.prereqs):
            return False
        return any(float(self.theta[p]) < PREREQ_THETA_GATE for p in self.prereqs[kc_id])


def sample_theta(rng: np.random.Generator, n_students: int, n_kcs: int) -> np.ndarray:
    """抽取 (N, K) 的真值能力矩阵, θ ~ U(0.1, 0.9) (方案定案值)。"""
    return rng.uniform(0.1, 0.9, size=(n_students, n_kcs))


def make_cohort(
    theta_matrix: np.ndarray,
    seed: int,
    *,
    dynamic: bool = False,
    learning_rate: float = 0.15,
    theta_max: float = 0.95,
    prereqs: Sequence[Sequence[int]] | None = None,
) -> list[SyntheticStudent]:
    """由共享的 θ 矩阵构造一届学生。

    第 i 名学生的作答随机流固定为 ``default_rng([seed, i, 1])`` —— 与策略臂无关,
    因此三条臂面对的是**同一批学生的同一条随机流**(共同随机数, 配对设计的方差消减来源)。
    """
    prereq_tuple = (
        DEFAULT_PREREQS if prereqs is None else tuple(tuple(int(p) for p in row) for row in prereqs)
    )
    return [
        SyntheticStudent(
            theta=theta_matrix[i],
            rng=np.random.default_rng([seed, i, 1]),
            dynamic=dynamic,
            learning_rate=learning_rate,
            theta_max=theta_max,
            prereqs=prereq_tuple,
        )
        for i in range(theta_matrix.shape[0])
    ]
