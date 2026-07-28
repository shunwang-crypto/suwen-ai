"""掌握度阈值唯一来源 (个性化波次 G6: 此前 0.4/0.5/0.6/0.8 散落 5 处各自硬编码)。

语义锚定 eval/simulation/policies.py 的自适应臂 (闭环实验 26.0 题 vs 随机 80.9, p=0.0020):
难度档位与 ZPD 边界在仿真与生产必须同源, 否则实验结论无法迁移到产品行为。
统一 = 单一出处, 不是单一数值 —— 各阈值语义不同, 禁止合并:

- MASTERED_THRESHOLD: 达标线。>= 视为已掌握 (画像"已掌握"段 / 难度 hard 档 / 仿真毕业判定)。
- WEAK_THRESHOLD: 画像"薄弱点"叙述线 (进 QA 画像摘要)。
- QUIZ_WEAK_THRESHOLD: 薄弱点定向出题圈选线 (略宽于叙述线, 让"将巩固未牢"的点也进练习)。
- REINFORCE_THRESHOLD: 计划补强/学情报告红线 (低于此值触发补强任务插入与报告告警)。
- ZPD_LOWER: 最近发展区下界。低于此值地基未打好, 教学收益低, 自适应出题降为 easy 保底。
- ADAPTIVE_EASY_BELOW: 难度档位分界, easy/mid 之间; mid/hard 分界 = MASTERED_THRESHOLD。
"""

from __future__ import annotations

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


def evidence_strength(score: float) -> str:
    """证据累计分 -> 定性档 limited/moderate/strong (前后端同一出处)。"""
    if score < EVIDENCE_MODERATE_ABOVE:
        return "limited"
    if score < EVIDENCE_STRONG_ABOVE:
        return "moderate"
    return "strong"


def difficulty_for_mastery(p_mastery: float) -> str:
    """按当前掌握度估计匹配难度档 (脚手架原则, 同仿真自适应臂):

    P(L) < 0.5 -> easy; 0.5 <= P(L) < 0.8 -> mid; >= 0.8 -> hard。
    上界锚定达标线 (产品语义), 未达标不派最难档。
    """
    if p_mastery < ADAPTIVE_EASY_BELOW:
        return "easy"
    if p_mastery < MASTERED_THRESHOLD:
        return "mid"
    return "hard"


def zone_label(p_mastery: float) -> str:
    """给可解释推荐用的定性区间标签 (P2-7 脱敏口径: 出境只给定性词)。"""
    if p_mastery < ZPD_LOWER:
        return "较弱"
    if p_mastery < QUIZ_WEAK_THRESHOLD:
        return "待巩固"
    if p_mastery < MASTERED_THRESHOLD:
        return "接近掌握"
    return "已掌握"
