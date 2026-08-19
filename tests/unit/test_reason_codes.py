"""推荐理由稳定码的契约守卫 (第 1 步验收判据的可执行版本)。

守什么:

1. 码格式与唯一性 —— wire format 不许漂;
2. 参数白名单是闭合的 —— 未声明的键、float、bool、越界枚举一律拒绝;
3. 脱敏是结构性的 —— 没有任何码声明过掌握度/置信/概率类字段名;
4. 每个码都有声明的参数契约, 且都被至少一处生产代码产出 (死码即漂移前兆);
5. 散文不许回流 —— 只有已知的四个模块允许写 reason 散文, 新文件想写必须显式
   改这份白名单 (对应"禁止新增 reason 字段写入");
6. 过渡边界 —— reason 仍在响应里 (旧客户端), 但每条推荐必须同时给码。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from eduagents_api.practice_recommendation import PracticeContext, build_recommendation
from eduagents_api.profile_summary import difficulty_rationale
from eduagents_api.reason_codes import (
    FORBIDDEN_PARAM_KEYS,
    TEXT_MAX,
    ReasonCode,
    all_specs,
    reason_payload,
    validate_reason_params,
)
from eduagents_api.routers.next_actions import compose_next_actions

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
API_ROOT = Path(__file__).resolve().parents[2] / "apps/api/src/eduagents_api"

#: 允许写 reason 散文的模块 (过渡期存量)。新增文件必须显式加入本表 ——
#: 这条白名单就是"禁止新增 reason 字段写入"的执行点。
#: plan_adjust 是有意保留的例外: 它的 reason 落库进 plan_adjustments 审计表,
#: 历史散文无法反解为码, 改造需要迁移 + 不可回填的历史 (见 reason_codes 模块注释)。
PROSE_ALLOWLIST = {
    "routers/next_actions.py",
    "practice_recommendation.py",
    "profile_summary.py",
    "plan_adjust.py",
}

#: 中文标点 —— 出现在 reason 赋值里即为散文 (码是 snake_case ASCII)。
_CJK_PUNCT_RE = re.compile(r"[：，。；、“”]")
_REASON_ASSIGN_RE = re.compile(r"""(?:"reason":|reason=)\s*(?:f?["']|\()""")


# ---- 1. 码格式与唯一性 ----


def test_codes_are_stable_snake_case_and_unique():
    values = [str(code) for code in ReasonCode]
    assert len(values) == len(set(values))
    for value in values:
        assert _CODE_RE.match(value), f"码不合 wire format: {value}"


# ---- 2. 参数白名单闭合 ----


def test_undeclared_param_is_rejected():
    with pytest.raises(ValueError, match="undeclared key"):
        reason_payload(ReasonCode.REVIEW_DUE, count=3, p_mastery=0.73)


def test_missing_required_param_is_rejected():
    with pytest.raises(ValueError, match="missing required key"):
        reason_payload(ReasonCode.REVIEW_DUE)


def test_float_is_rejected_everywhere():
    """掌握度小数没有可表示的类型 —— 这是脱敏的结构性防线, 不是事后正则。"""
    with pytest.raises(ValueError, match="float"):
        validate_reason_params(ReasonCode.REVIEW_DUE, {"count": 0.73})
    with pytest.raises(ValueError, match="float"):
        validate_reason_params(
            ReasonCode.WEAK_KC_GENERIC, {"zone": 0.5}  # type: ignore[dict-item]
        )


def test_bool_is_rejected_so_flags_stay_enums():
    with pytest.raises(ValueError, match="bool"):
        validate_reason_params(ReasonCode.REVIEW_DUE, {"count": True})


@pytest.mark.parametrize("bad", ["0", "1", "0.73", "73%", "掌握度 73%"])
def test_enum_param_rejects_numeric_and_percentage_strings(bad: str):
    """数字/百分比冒充档位词会被枚举校验挡下 (用户点名的几种泄漏形态)。"""
    with pytest.raises(ValueError, match="枚举值"):
        validate_reason_params(ReasonCode.WEAK_KC_GENERIC, {"zone": bad})


def test_negative_count_and_overlong_text_are_rejected():
    with pytest.raises(ValueError, match="不得为负"):
        validate_reason_params(ReasonCode.REVIEW_DUE, {"count": -1})
    with pytest.raises(ValueError, match="超过"):
        validate_reason_params(
            ReasonCode.PLAN_TASK_PENDING, {"goal": "长" * (TEXT_MAX + 1)}
        )


def test_none_valued_optional_param_is_dropped_not_rejected():
    payload = reason_payload(ReasonCode.RESUME_CONFUSED, quote="卡在链式法则", kc_name=None)
    assert payload["reason_params"] == {"quote": "卡在链式法则"}


# ---- 3. 脱敏: 没有任何码声明过禁用字段名 ----


def test_no_spec_declares_a_forbidden_param_key():
    for code, spec in all_specs().items():
        leaked = sorted(set(spec.keys) & FORBIDDEN_PARAM_KEYS)
        assert not leaked, f"{code} 声明了禁用参数名: {leaked}"


# ---- 4. 每个码都有契约, 且都被生产代码产出 ----


def test_every_code_has_a_declared_spec():
    specs = all_specs()
    missing = [str(code) for code in ReasonCode if code not in specs]
    assert not missing, f"这些码没有参数契约: {missing}"


def test_every_code_is_produced_by_production_code():
    """死码即漂移前兆: 枚举里的每个码都必须能在生产代码里找到产出点。"""
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in API_ROOT.rglob("*.py")
        if "__pycache__" not in str(path)
    )
    unused = [code.name for code in ReasonCode if f"ReasonCode.{code.name}" not in sources]
    assert not unused, f"这些码没有任何产出点: {unused}"


# ---- 5. 散文不许回流 ----


def _prose_reason_lines(path: Path) -> list[str]:
    """返回该文件里"给 reason 赋中文散文"的行。

    要排掉一类误判: LLM 提示词里描述输出格式的字符串 (形如
    ``'只输出 JSON: {"score": …, "reason": "评分理由"}。'``) —— 那是提示词内容,
    不是 reason 字段写入。判据: 匹配点若落在另一个字符串字面量内部 (前缀里的
    引号数为奇数), 则跳过。
    """
    hits: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for match in _REASON_ASSIGN_RE.finditer(stripped):
            prefix = stripped[: match.start()]
            if prefix.count("'") % 2 == 1 or prefix.count('"') % 2 == 1:
                continue  # 嵌在别的字符串里 (提示词模板)
            if _CJK_PUNCT_RE.search(stripped):
                hits.append(stripped)
            break
    return hits


def test_prose_reason_writers_are_confined_to_the_allowlist():
    offenders = {
        path.relative_to(API_ROOT).as_posix()
        for path in API_ROOT.rglob("*.py")
        if "__pycache__" not in str(path) and _prose_reason_lines(path)
    }
    assert offenders <= PROSE_ALLOWLIST, (
        "新的 reason 散文产出点: "
        f"{sorted(offenders - PROSE_ALLOWLIST)}；请改用 reason_payload() 产出稳定码，"
        "确有过渡需要再显式加入 PROSE_ALLOWLIST"
    )


def test_prose_guard_actually_detects_a_new_offender(tmp_path: Path):
    """守卫自身必须真的会响 —— 否则它只是个恒真断言。"""
    fake = tmp_path / "fake_router.py"
    fake.write_text('item = {"reason": "因为这样，所以那样。"}\n', encoding="utf-8")
    assert _prose_reason_lines(fake)


def test_prose_guard_ignores_llm_prompt_templates(tmp_path: Path):
    fake = tmp_path / "fake_prompt.py"
    fake.write_text(
        "PROMPT = '只输出 JSON: {\"score\": 0到1的小数, \"reason\": \"评分理由\"}。'\n",
        encoding="utf-8",
    )
    assert not _prose_reason_lines(fake)


# ---- 6. 过渡边界: 码与散文并存, 每条推荐都有码 ----


def test_next_actions_items_carry_code_and_transitional_prose():
    items = compose_next_actions(due_count=3, insight=None, weak_points=[], plan=None)
    assert items[0]["reason_code"] == ReasonCode.REVIEW_DUE
    assert items[0]["reason_params"] == {"count": 3}
    assert items[0]["reason"], "过渡期仍要给旧客户端散文"


def test_cold_start_item_carries_code():
    items = compose_next_actions(due_count=0, insight=None, weak_points=[], plan=None)
    assert items[0]["reason_code"] == ReasonCode.COLD_START_NO_EVIDENCE
    assert items[0]["reason_params"] == {}


def test_weak_point_item_passes_through_mastery_zone_payload():
    """薄弱点条目透传 difficulty_rationale 的 payload, 不在 next_actions 重算参数。"""
    rationale = difficulty_rationale("链式法则", 0.35, recent_error=True)
    weak = {"name": "链式法则", **rationale}
    items = compose_next_actions(due_count=0, insight=None, weak_points=[weak], plan=None)
    assert items[0]["reason_code"] == ReasonCode.MASTERY_ZONE
    assert items[0]["reason_params"] == rationale["reason_params"]


def test_weak_point_without_payload_falls_back_to_generic_code():
    items = compose_next_actions(
        due_count=0,
        insight=None,
        weak_points=[{"name": "链式法则", "zone": "较弱"}],
        plan=None,
    )
    assert items[0]["reason_code"] == ReasonCode.WEAK_KC_GENERIC
    assert items[0]["reason_params"] == {"zone": "较弱"}


def test_difficulty_rationale_exposes_band_position_for_frontend_fidelity():
    """16 种措辞 = (zone, band_position, recent_error, difficulty) 的函数,
    四个参数缺一前端就无法无损复现。"""
    low = difficulty_rationale("导数", 0.05, recent_error=False)
    high = difficulty_rationale("导数", 0.25, recent_error=False)
    assert low["zone"] == high["zone"] == "较弱"
    assert (low["band_position"], high["band_position"]) == (0, 1)
    assert low["reason"] != high["reason"]
    assert low["reason_params"]["band_position"] == 0
    assert high["reason_params"]["recent_error"] == "no"


def test_practice_recommendation_json_carries_code_and_params():
    result = build_recommendation(PracticeContext(due_count=2)).to_dict()
    assert result["reason_code"] == ReasonCode.REVIEW_DUE
    assert result["reason_params"] == {"count": 2}
    assert result["reason"]


def test_practice_recommendation_upload_branch_carries_code():
    result = build_recommendation(PracticeContext()).to_dict()
    assert result["reason_code"] == ReasonCode.KB_ABSENT
    assert result["reason_params"] == {}
