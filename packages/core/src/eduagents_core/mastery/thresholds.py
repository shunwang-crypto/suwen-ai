"""掌握度阈值的唯一来源。

统一指单一出处，不是把不同语义强行合并成同一数值：

- MASTERED_THRESHOLD: 达标线。>= 视为已掌握（画像“已掌握”段 / 难度 hard 档）。
- WEAK_THRESHOLD: 画像"薄弱点"叙述线 (进 QA 画像摘要)。
- QUIZ_WEAK_THRESHOLD: 薄弱点定向出题圈选线 (略宽于叙述线, 让"将巩固未牢"的点也进练习)。
- REINFORCE_THRESHOLD: 计划补强/学情报告红线 (低于此值触发补强任务插入与报告告警)。
- ZPD_LOWER: 最近发展区下界。低于此值地基未打好, 教学收益低, 自适应出题降为 easy 保底。
- ADAPTIVE_EASY_BELOW: 难度档位分界, easy/mid 之间; mid/hard 分界 = MASTERED_THRESHOLD。
"""

from __future__ import annotations

from typing import Literal

MASTERED_THRESHOLD = 0.8
WEAK_THRESHOLD = 0.5
QUIZ_WEAK_THRESHOLD = 0.6
REINFORCE_THRESHOLD = 0.4
ZPD_LOWER = 0.3
ADAPTIVE_EASY_BELOW = 0.5

#: 难度档位 (与出题 difficulty_mix 的键一致)
DIFFICULTIES = ("easy", "mid", "hard")

# --- 证据强度档位 (语义独立于掌握度阈值, 故单独命名, 不许与上面几档混用) ---
# mastery_records.confidence 是证据累计分, **不是**校准过的统计置信概率 ——
# 界面上写"置信 100%"会与我们自己的校准实验结论冲突, 故只对外给定性档。
EVIDENCE_MODERATE_ABOVE = 0.4
EVIDENCE_STRONG_ABOVE = 0.8

# Evidence state is derived from valid MasteryLog rows, never from the BKT
# prior or from the current p_mastery value.  Three independent observations
# are the minimum stable sample used by the product for a weak/strong claim.
EVIDENCE_OBSERVED_MIN_EVENTS = 3
EvidenceState = Literal["unobserved", "estimated", "observed"]
VALID_MASTERY_EVIDENCE_TYPES = frozenset(
    {"quiz_attempt", "review", "assessment_answer", "appeal_regrade"}
)
EVIDENCE_STATE_LABELS: dict[EvidenceState, str] = {
    "unobserved": "未观测",
    "estimated": "估计中",
    "observed": "已观测",
}


def evidence_state(evidence_count: int) -> EvidenceState:
    """Classify the number of independent verified mastery events.

    Callers must count distinct valid ``MasteryLog.evidence_id`` values.  A
    BKT prior or the legacy confidence accumulator is not evidence.
    """
    count = max(0, int(evidence_count))
    if count == 0:
        return "unobserved"
    if count < EVIDENCE_OBSERVED_MIN_EVENTS:
        return "estimated"
    return "observed"


def evidence_state_label(state: EvidenceState) -> str:
    """Return the stable Chinese display label for an evidence state."""
    return EVIDENCE_STATE_LABELS[state]


def evidence_strength(score: float) -> str:
    """证据累计分 -> 定性档 limited/moderate/strong (前后端同一出处)。"""
    if score < EVIDENCE_MODERATE_ABOVE:
        return "limited"
    if score < EVIDENCE_STRONG_ABOVE:
        return "moderate"
    return "strong"


def difficulty_for_mastery(p_mastery: float) -> str:
    """按当前掌握度估计匹配难度档：

    P(L) < 0.5 -> easy; 0.5 <= P(L) < 0.8 -> mid; >= 0.8 -> hard。
    上界锚定达标线 (产品语义), 未达标不派最难档。
    """
    if p_mastery < ADAPTIVE_EASY_BELOW:
        return "easy"
    if p_mastery < MASTERED_THRESHOLD:
        return "mid"
    return "hard"


def zone_label(p_mastery: float) -> str:
    """给可解释推荐使用的定性区间标签。"""
    if p_mastery < ZPD_LOWER:
        return "较弱"
    if p_mastery < QUIZ_WEAK_THRESHOLD:
        return "待巩固"
    if p_mastery < MASTERED_THRESHOLD:
        return "接近掌握"
    return "已掌握"


def mastery_band(p_mastery: float) -> str:
    """zone_label 的稳定英文键版本 (weak/reinforce/near/mastered), 同一组阈值。

    zone_label 的中文串是**出境展示文案**, 会随文案打磨变动; 需要按档位分支的代码
    (如 tutor 起始 hint_level) 必须用这里的稳定键, 否则改一次文案就静默改掉行为。
    """
    if p_mastery < ZPD_LOWER:
        return "weak"
    if p_mastery < QUIZ_WEAK_THRESHOLD:
        return "reinforce"
    if p_mastery < MASTERED_THRESHOLD:
        return "near"
    return "mastered"
