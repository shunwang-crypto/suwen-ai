"""出题个性化单元测试 (P1-1 ZPD 难度自适应 + P1-2 画像注入): FakeGateway 脚本化, 全离线。

覆盖: ① difficulty_mix 按掌握度分档 (阈值同源 thresholds, 边界 0.3/0.5/0.8);
② 逐 KC 理由只用定性词 (P2-7: 不含掌握度小数); ③ generate 实收 prompt 的画像段 —
薄弱生注入、无画像不注入且无占位符残留; interrupt 负载携带 difficulty_reasons。
"""

from __future__ import annotations

import json
import re

from eduagents_api.profile_summary import difficulty_rationale
from eduagents_api.routers.quiz import adaptive_difficulty_mix
from eduagents_core.config import Settings
from eduagents_core.education.context import EducationDeps
from eduagents_core.education.quiz import PROFILE_SECTION_TEMPLATE, QuizTools
from eduagents_core.gateway.base import ChatRequest, ChatResult
from eduagents_core.harness.task import TaskRecord
from eduagents_core.mastery.thresholds import difficulty_for_mastery, zone_label


class FakeGateway:
    """预置响应文本队列, 逐次弹出; 记录收到的 ChatRequest 供断言。"""

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.requests: list[ChatRequest] = []

    async def chat(self, req: ChatRequest) -> ChatResult:
        self.requests.append(req)
        text = (
            json.dumps({"issues": [], "summary": "题目质量合格"}, ensure_ascii=False)
            if req.feature == "quiz_verify"
            else self.texts.pop(0)
        )
        return ChatResult(
            text=text,
            model="fake",
            tier=req.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="stub",
        )


BP_JSON = json.dumps(
    {"items": [{"kc_name": "导数定义", "type": "single", "difficulty": "easy"}], "summary": "测试"},
    ensure_ascii=False,
)
GEN_JSON = json.dumps(
    {
        "questions": [
            {
                "type": "single",
                "stem": "导数的定义是?",
                "options": ["极限", "积分", "级数", "微分方程"],
                "answer": "A",
                "explanation": "由定义即差商极限; 选 B 通常是混淆了微分与积分。",
                "kc_names": ["导数定义"],
                "difficulty": "easy",
                "source_hint": None,
            }
        ]
    },
    ensure_ascii=False,
)


def weak_item(name: str, p: float, recent: bool = False) -> dict:
    return {"kc_id": "k-" + name, "name": name, "p_mastery": p, "recent_error": recent}


def base_state(**over) -> dict:
    state = {
        "tenant_id": "t1",
        "user_id": "u1",
        "request_id": "r1",
        "kb_id": "",
        "topic": "薄弱点强化练习",
        "num_questions": 1,
        "difficulty_mix": {"easy": 1},
        "kc_names": ["导数定义"],
    }
    state.update(over)
    return state


def cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def test_difficulty_tier_boundaries():
    """边界值语义: 0.5 之下 easy, [0.5, 0.8) mid, 0.8 起 hard; 0.3 只影响定性区间词。"""
    assert difficulty_for_mastery(0.29) == "easy"
    assert difficulty_for_mastery(0.3) == "easy"
    assert difficulty_for_mastery(0.49) == "easy"
    assert difficulty_for_mastery(0.5) == "mid"
    assert difficulty_for_mastery(0.79) == "mid"
    assert difficulty_for_mastery(0.8) == "hard"
    assert zone_label(0.29) == "较弱"
    assert zone_label(0.3) == "待巩固"


def test_adaptive_mix_counts_by_kc_mastery():
    weak = [weak_item("导数定义", 0.2), weak_item("极值判定", 0.55)]
    assert adaptive_difficulty_mix(weak, 4) == {"easy": 2, "mid": 2, "hard": 0}
    assert adaptive_difficulty_mix(weak, 1) == {"easy": 1, "mid": 0, "hard": 0}


def test_adaptive_mix_sum_equals_num_questions():
    weak = [weak_item("a", 0.1), weak_item("b", 0.5), weak_item("c", 0.85)]
    for n in (1, 3, 5, 7):
        mix = adaptive_difficulty_mix(weak, n)
        assert sum(mix.values()) == n
    assert adaptive_difficulty_mix(weak, 3)["hard"] == 1


def test_rationale_uses_qualitative_zone_words_without_decimals():
    cases = [(0.12, "较弱"), (0.3, "待巩固"), (0.55, "待巩固"), (0.7, "接近掌握")]
    for p, expected_zone in cases:
        r = difficulty_rationale("导数定义", p, recent_error=False)
        assert r["kc_name"] == "导数定义"
        assert r["zone"] == expected_zone
        assert r["difficulty"] == difficulty_for_mastery(p)
        assert expected_zone in r["reason"]
        assert re.search("\\d\\.\\d", r["reason"]) is None
        assert str(p) not in r["reason"]


def test_rationale_mentions_recent_error_priority():
    with_err = difficulty_rationale("极值判定", 0.4, recent_error=True)
    without = difficulty_rationale("极值判定", 0.4, recent_error=False)
    assert "近 7 天答错过" in with_err["reason"]
    assert "近 7 天答错过" not in without["reason"]


def test_profile_section_template_discipline():
    """措辞纪律 (同 qa 的 PROFILE_SECTION_TEMPLATE): 画像仅调整出题, 不进题干不罗列原文。"""
    rendered = PROFILE_SECTION_TEMPLATE.replace("{{PROFILE}}", "摘要")
    assert "仅用于调整出题" in rendered
    assert "不得写进题干" in rendered
    assert "不得罗列画像原文" in rendered
    assert "错因分布" in rendered


def _bp_items(*difficulties: str, kc: str = "导数定义") -> dict:
    return {"items": [{"kc_name": kc, "type": "single", "difficulty": d} for d in difficulties]}


def test_blueprint_difficulty_pinned_by_kc_reasons():
    """蓝图 LLM 给薄弱 KC 配错难度: 按定向分档表钉回, 展示与实际必须一致。"""
    from eduagents_core.education.quiz import _fix_blueprint

    blueprint, _, enforced = _fix_blueprint(
        _bp_items("hard", "hard"),
        num_questions=2,
        difficulty_mix={"easy": 2, "mid": 0, "hard": 0},
        kc_names=["导数定义"],
        topic="导数",
        difficulty_by_kc={"导数定义": "easy"},
    )
    assert [it["difficulty"] for it in blueprint["items"]] == ["easy", "easy"]
    assert enforced == 2
    assert all("_pinned" not in it for it in blueprint["items"])


def test_blueprint_mix_enforced_when_llm_ignores_it():
    """题数够但难度全错 (审查 P2 场景): mix 程序性强制, 不再静默失效。"""
    from eduagents_core.education.quiz import _fix_blueprint

    blueprint, corrected, enforced = _fix_blueprint(
        _bp_items("mid", "mid", "mid"),
        num_questions=3,
        difficulty_mix={"easy": 2, "mid": 1, "hard": 0},
        kc_names=[],
        topic="导数",
    )
    counts = [it["difficulty"] for it in blueprint["items"]]
    assert counts.count("easy") == 2 and counts.count("mid") == 1
    assert corrected is False
    assert enforced == 2


def test_blueprint_mix_not_enforced_for_user_edit():
    """用户改过的蓝图: 学习者控制权, 难度不被系统改回 (enforce_mix=False)。"""
    from eduagents_core.education.quiz import _fix_blueprint

    blueprint, _, enforced = _fix_blueprint(
        _bp_items("hard", "hard", "hard"),
        num_questions=3,
        difficulty_mix={"easy": 2, "mid": 1, "hard": 0},
        kc_names=[],
        topic="导数",
        enforce_mix=False,
    )
    assert [it["difficulty"] for it in blueprint["items"]] == ["hard", "hard", "hard"]
    assert enforced == 0


def test_blueprint_mix_skipped_when_sum_mismatch():
    """mix 目标数之和 != 题数时不做配比改标 (无法整除的意图不猜), 钉住仍生效。"""
    from eduagents_core.education.quiz import _fix_blueprint

    blueprint, _, enforced = _fix_blueprint(
        _bp_items("mid", "mid", kc="极值判定"),
        num_questions=2,
        difficulty_mix={"easy": 5, "mid": 0, "hard": 0},
        kc_names=[],
        topic="导数",
        difficulty_by_kc={"极值判定": "mid"},
    )
    assert [it["difficulty"] for it in blueprint["items"]] == ["mid", "mid"]
    assert enforced == 0


async def test_profile_and_difficulty_reasons_reach_native_policy_and_confirmation():
    reasons = [difficulty_rationale("导数定义", 0.24, recent_error=True)]
    state = base_state(profile_summary="导数定义较弱", difficulty_reasons=reasons)
    record = TaskRecord(state=state, status="running")
    tools = QuizTools(
        EducationDeps(Settings(_env_file=None), FakeGateway([])), record, lambda _: None
    )
    assert "导数定义较弱" in tools.policy() and "不得写进题干" in tools.policy()
    await tools.submit_blueprint({"blueprint": json.loads(BP_JSON)})
    assert record.pending["difficulty_reasons"] == reasons


def test_no_profile_has_no_placeholder():
    tools = QuizTools(
        EducationDeps(Settings(_env_file=None), FakeGateway([])),
        TaskRecord(state=base_state()),
        lambda _: None,
    )
    assert "学生画像" not in tools.policy() and "{{PROFILE}}" not in tools.policy()
