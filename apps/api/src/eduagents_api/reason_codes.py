"""推荐理由的稳定码与参数 —— 唯一出处。

为什么不要散文: 同一条理由此前在 next_actions / practice_recommendation /
profile_summary 各写一遍, 改一处就漂一处; 而且散文一旦写进掌握度数字, P2-7
脱敏口径就只能靠人眼守。稳定码把"为什么推荐这个"变成可测契约 —— 后端只产出
``reason_code`` + ``reason_params``, 展示文案集中在前端 ``lib/reason-text.ts``。
稳定键与展示文案分离的理由同 ``mastery.thresholds`` 的 ``mastery_band`` /
``zone_label``: 改一次文案不得静默改掉行为。

**覆盖范围**（说准确, 免得日后误以为已经全量收口）:

- ``routers/next_actions`` 五种 kind —— 已接入;
- ``practice_recommendation`` 九个分支 —— 已接入（同时仍写兼容散文, 见下）;
- ``profile_summary.weak_point_kcs`` / ``difficulty_rationale`` 逐 KC 理由 —— 已接入;
- ``plan_adjust`` 的计划调整理由 —— **未接入**, 且当前不打算接入（理由见文末）。

一句话口径: 稳定码覆盖**学习推荐与难度解释**, 不含**计划调整审计理由**。

过渡期契约（现状就是"双写", 不是"只给码"）:

- 后端新产出 ``reason_code`` + ``reason_params``;
- ``reason`` 散文字段**由后端继续生成**, 作为尚未升级旧客户端的兼容字段 ——
  它不是已删除字段, 也不是"已经没人写"的字段;
- 新前端代码只消费 ``reason_code`` / ``reason_params``, 禁止读 ``reason``;
- **禁止新增散文产出点**（守卫见 ``tests/unit/test_reason_codes.py`` 的
  ``PROSE_ALLOWLIST``）—— 约束的是"不再变多", 不是"已经归零"。

脱敏是结构性的, 不靠正则事后查:

1. 每个码的参数是**闭合白名单** (``_SPECS``), 传入未声明的键直接抛错 ——
   ``p_mastery`` 这类字段名根本没有落点;
2. ``float`` 在任何参数位上都被拒绝 —— 掌握度小数没有可表示的类型;
3. ``enum`` 参数必须落在声明的取值集合里 —— 数字冒充档位词会被挡下;
4. 一条元测试遍历全部 ``_SPECS``, 断言没有任何码声明过禁用字段名。

**不在本模块治理范围**: ``plan_adjust`` 的调整理由。它不是实时推荐理由, 而是
落库进 ``plan_adjustments.reason`` 的审计事实 —— 历史行里的散文无法反解为码,
改造需要迁移 + 不可回填的历史; 它另有一处口径问题 (理由里直接写掌握度百分比),
属产品决定, 不在本次技术整理内。守卫测试对它保留显式白名单, 不静默放过。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ReasonCode(StrEnum):
    """可解释推荐的稳定码。值即 wire format, 必须匹配 ``^[a-z][a-z0-9_]*$``。"""

    # --- 复习 / 记忆 ---
    REVIEW_DUE = "review_due"
    """FSRS 排程判定今日到期。params: count"""

    # --- 上次卡点 (learner_insights) ---
    RESUME_CONFUSED = "resume_confused"
    """对话反思里存下的学生原话。params: quote, kc_name?"""
    RESUME_MISCONCEPTION = "resume_misconception"
    """辅导小结记录的易错点。params: quote, kc_name?"""

    # --- 掌握度 / 薄弱点 ---
    MASTERY_ZONE = "mastery_zone"
    """逐 KC 难度分档理由 (取代 difficulty_rationale 的 16 种措辞组合)。"""
    WEAK_KC_GENERIC = "weak_kc_generic"
    """薄弱点但缺逐 KC 档位理由时的兜底。params: zone"""
    WEAK_KC_UNSTABLE = "weak_kc_unstable"
    """近期作答显示知识点不稳定。无参数。"""
    KC_UNVERIFIED = "kc_unverified"
    """该 KC 还没测过, 先快速验证 (冷启动逐 KC 理由)。params: kc_name"""
    PRACTICE_MAINTAIN = "practice_maintain"
    """有证据但无优先补强点, 保持熟练度。无参数。"""

    # --- 计划 ---
    PLAN_TASK_PROGRESS = "plan_task_progress"
    """计划进行到第 N 天且有未完成任务。params: goal, day_no, undone"""
    PLAN_TASK_PENDING = "plan_task_pending"
    """今日计划任务未完成。params: goal"""

    # --- 证据不足 / 资料状态 ---
    COLD_START_NO_EVIDENCE = "cold_start_no_evidence"
    """还没有真实作答证据。无参数。"""
    KB_READY_FIRST_QUIZ = "kb_ready_first_quiz"
    """资料就绪, 先按真实章节知识点摸起点。无参数。"""
    KB_CHOICE_REQUIRED = "kb_choice_required"
    """多份就绪资料, 上下文不足以判断练哪科。无参数。"""
    KB_PREPARING = "kb_preparing"
    """资料已收到, 知识点整理中。无参数。"""
    KB_EMPTY = "kb_empty"
    """该知识库还没有可用资料。无参数。"""
    KB_ABSENT = "kb_absent"
    """还没有任何可用的个人资料。无参数。"""


#: 绝不允许出现在任何码的参数里的字段名 (元测试遍历 _SPECS 断言)。
#: 掌握度估计、置信分、概率一律不出境展示层 —— 出境的只有定性档位词。
FORBIDDEN_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "mastery",
        "p_mastery",
        "mastery_probability",
        "p_l",
        "probability",
        "confidence",
        "confidence_score",
        "score",
        "percent",
        "percentage",
        "ratio",
    }
)

#: 定性区间词, 与 ``mastery.thresholds.zone_label`` 同源 (唯一出处在那边)。
ZONE_VALUES: tuple[str, ...] = ("较弱", "待巩固", "接近掌握", "已掌握")
#: 难度档位键, 与 ``mastery.thresholds.DIFFICULTIES`` 同源。
DIFFICULTY_VALUES: tuple[str, ...] = ("easy", "mid", "hard")
#: 布尔语义一律走枚举字符串: wire 上不出现 bool, 避免 0/1 与计数混淆。
FLAG_VALUES: tuple[str, ...] = ("yes", "no")

#: 展示字符串上界 (KC 名 / 计划目标 / 学生原话截断后)。
TEXT_MAX = 200


class ReasonSpec:
    """一个码的闭合参数契约。

    ``counts`` 只收非负 int; ``enums`` 只收声明取值集合内的字符串;
    ``texts`` 收有界展示字符串 (KC 名、计划目标、学生原话这类内容参数)。
    未声明的键一律拒绝 —— 这是脱敏的第一道结构防线。
    """

    __slots__ = ("counts", "enums", "texts", "optional")

    def __init__(
        self,
        *,
        counts: tuple[str, ...] = (),
        enums: dict[str, tuple[str, ...]] | None = None,
        texts: tuple[str, ...] = (),
        optional: tuple[str, ...] = (),
    ) -> None:
        self.counts = counts
        self.enums = enums or {}
        self.texts = texts
        self.optional = optional

    @property
    def keys(self) -> tuple[str, ...]:
        return (*self.counts, *self.enums.keys(), *self.texts)

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(key for key in self.keys if key not in self.optional)


_SPECS: dict[ReasonCode, ReasonSpec] = {
    ReasonCode.REVIEW_DUE: ReasonSpec(counts=("count",)),
    ReasonCode.RESUME_CONFUSED: ReasonSpec(texts=("quote", "kc_name"), optional=("kc_name",)),
    ReasonCode.RESUME_MISCONCEPTION: ReasonSpec(
        texts=("quote", "kc_name"), optional=("kc_name",)
    ),
    ReasonCode.MASTERY_ZONE: ReasonSpec(
        counts=("band_position",),
        enums={
            "zone": ZONE_VALUES,
            "difficulty": DIFFICULTY_VALUES,
            "recent_error": FLAG_VALUES,
        },
        texts=("kc_name",),
    ),
    ReasonCode.WEAK_KC_GENERIC: ReasonSpec(enums={"zone": ZONE_VALUES}),
    ReasonCode.WEAK_KC_UNSTABLE: ReasonSpec(),
    ReasonCode.KC_UNVERIFIED: ReasonSpec(texts=("kc_name",)),
    ReasonCode.PRACTICE_MAINTAIN: ReasonSpec(),
    ReasonCode.PLAN_TASK_PROGRESS: ReasonSpec(
        counts=("day_no", "undone"), texts=("goal",)
    ),
    ReasonCode.PLAN_TASK_PENDING: ReasonSpec(texts=("goal",)),
    ReasonCode.COLD_START_NO_EVIDENCE: ReasonSpec(),
    ReasonCode.KB_READY_FIRST_QUIZ: ReasonSpec(),
    ReasonCode.KB_CHOICE_REQUIRED: ReasonSpec(),
    ReasonCode.KB_PREPARING: ReasonSpec(),
    ReasonCode.KB_EMPTY: ReasonSpec(),
    ReasonCode.KB_ABSENT: ReasonSpec(),
}


def spec_for(code: ReasonCode) -> ReasonSpec:
    """取某个码的参数契约; 缺声明视为编码错误 (不静默放行)。"""
    try:
        return _SPECS[code]
    except KeyError:  # pragma: no cover - 元测试保证全覆盖
        raise ValueError(f"reason code has no declared params spec: {code}") from None


def all_specs() -> dict[ReasonCode, ReasonSpec]:
    """给元测试遍历用 (只读视图)。"""
    return dict(_SPECS)


def _reject_value(code: ReasonCode, key: str, value: Any, why: str) -> None:
    raise ValueError(f"reason_params[{key!r}] invalid for {code}: {why}")


def validate_reason_params(code: ReasonCode, params: dict[str, Any]) -> None:
    """按闭合白名单校验参数; 违约抛 ``ValueError`` (视为编码错误, 不是用户输入错误)。"""
    spec = spec_for(code)
    allowed = set(spec.keys)
    for key in params:
        if key not in allowed:
            raise ValueError(
                f"reason_params has undeclared key {key!r} for {code}; "
                f"allowed={sorted(allowed)}"
            )
    for key in spec.required:
        if key not in params:
            raise ValueError(f"reason_params missing required key {key!r} for {code}")

    for key, value in params.items():
        # bool 是 int 子类, 必须先挡掉: wire 上的旗标一律走 FLAG_VALUES 枚举串。
        if isinstance(value, bool):
            _reject_value(code, key, value, "bool 不上 wire, 请用 yes/no 枚举串")
        if isinstance(value, float):
            _reject_value(code, key, value, "float 一律拒绝 (掌握度小数不得出境)")
        if key in spec.counts:
            if not isinstance(value, int):
                _reject_value(code, key, value, "计数参数必须是 int")
            if value < 0:
                _reject_value(code, key, value, "计数参数不得为负")
            continue
        if key in spec.enums:
            if not isinstance(value, str) or value not in spec.enums[key]:
                _reject_value(
                    code, key, value, f"必须是枚举值之一 {list(spec.enums[key])}"
                )
            continue
        if not isinstance(value, str):
            _reject_value(code, key, value, "展示参数必须是字符串")
        if len(value) > TEXT_MAX:
            _reject_value(code, key, value, f"展示参数超过 {TEXT_MAX} 字")


def reason_payload(code: ReasonCode, /, **params: Any) -> dict[str, Any]:
    """产出 ``{"reason_code": ..., "reason_params": {...}}``。

    所有推荐理由的**唯一**产出入口: 走这里才有校验, 绕过它就绕过了脱敏防线。
    ``None`` 值按"未提供"丢弃, 免得调用方到处写条件分支。
    """
    cleaned = {key: value for key, value in params.items() if value is not None}
    validate_reason_params(code, cleaned)
    return {"reason_code": str(code), "reason_params": cleaned}
