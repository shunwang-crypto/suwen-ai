"""quiz 领域工具的业务权限、校验与修复；原生执行覆盖见 test_native_agent_tasks.py。"""

from __future__ import annotations

import json

import pytest
from eduagents_core.config import Settings
from eduagents_core.education.context import EducationDeps
from eduagents_core.education.quiz import (
    QUESTION_SCHEMA,
    QuizTools,
    grade_objective,
    quiz_generation_max_tokens,
    review_questions,
    validate_questions,
)
from eduagents_core.gateway.base import BudgetExceeded, ChatRequest, ChatResult
from eduagents_core.harness.task import TaskRecord


class FakeGateway:
    """预置响应文本队列, 逐次弹出; 记录收到的 ChatRequest 供断言。"""

    def __init__(
        self,
        texts: list[str],
        *,
        critic_texts: list[str] | None = None,
        critic_errors: list[Exception] | None = None,
        finish_reasons: list[str | None] | None = None,
    ) -> None:
        self.texts = list(texts)
        self.critic_texts = list(critic_texts or [])
        self.critic_errors = list(critic_errors or [])
        self.finish_reasons = list(finish_reasons or [])
        self.requests: list[ChatRequest] = []

    async def chat(self, req: ChatRequest) -> ChatResult:
        self.requests.append(req)
        if req.feature == "quiz_verify":
            if self.critic_errors:
                raise self.critic_errors.pop(0)
            text = self.critic_texts.pop(0) if self.critic_texts else verify_json()
        else:
            text = self.texts.pop(0)
        return ChatResult(
            text=text,
            model="fake",
            tier=req.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="stub",
            finish_reason=self.finish_reasons.pop(0)
            if req.feature == "quiz_generate" and self.finish_reasons
            else None,
        )


def bp_item(kc: str = "泰勒公式", qtype: str = "single", difficulty: str = "easy") -> dict:
    return {"kc_name": kc, "type": qtype, "difficulty": difficulty}


def good_q(**over) -> dict:
    q = {
        "type": "single",
        "stem": "泰勒公式的余项是?",
        "options": ["拉格朗日余项", "皮亚诺余项", "积分余项", "以上都是"],
        "answer": "D",
        "explanation": "三种余项均是泰勒公式余项形式; 选 A 通常是只记得一种形式。",
        "kc_names": ["泰勒公式"],
        "difficulty": "easy",
        "source_hint": None,
    }
    q.update(over)
    return q


def verify_json(issues: list[dict] | None = None) -> str:
    return json.dumps({"issues": issues or [], "summary": "题目质量合格"}, ensure_ascii=False)


def base_state(**over) -> dict:
    state = {
        "tenant_id": "t1",
        "user_id": "u1",
        "request_id": "r1",
        "kb_id": "",
        "topic": "泰勒公式",
        "num_questions": 2,
        "difficulty_mix": {"easy": 1, "mid": 1},
        "kc_names": [],
    }
    state.update(over)
    return state


TWO_ITEMS = [
    bp_item(difficulty="easy"),
    bp_item(kc="拉格朗日余项", qtype="blank", difficulty="mid"),
]
BLANK_Q = {
    "type": "blank",
    "stem": "$e^x$ 在 $x=0$ 处的一阶泰勒多项式是 ____",
    "options": [],
    "answer": "1+x|$1+x$",
    "explanation": "由 $f(0)=f'(0)=1$ 得。",
    "kc_names": ["拉格朗日余项"],
    "difficulty": "mid",
    "source_hint": None,
}


def test_quiz_generation_token_budget_covers_reasoning_and_stays_bounded():
    assert quiz_generation_max_tokens(1, 0) == 7200
    assert quiz_generation_max_tokens(2, 0) == 8400
    assert quiz_generation_max_tokens(5, 1) == 14000
    assert quiz_generation_max_tokens(20, 2) == 16000


def test_grade_single_case_and_whitespace_insensitive():
    q = good_q(answer="B")
    assert grade_objective(q, " b ") is True
    assert grade_objective(q, "B") is True
    assert grade_objective(q, "A") is False
    assert grade_objective(q, "") is False


def test_grade_multi_set_equality():
    q = good_q(type="multi", answer="A,C")
    assert grade_objective(q, "C,A") is True
    assert grade_objective(q, " c , a ") is True
    assert grade_objective(q, "A") is False
    assert grade_objective(q, "A,B,C") is False
    assert grade_objective(q, "") is False


def test_grade_blank_exact_with_alternatives():
    q = {"type": "blank", "answer": "1+x | $1+x$"}
    assert grade_objective(q, "1+x") is True
    assert grade_objective(q, "  $1+x$  ") is True
    assert grade_objective(q, "1 + x") is False
    assert grade_objective(q, "x+1") is False


def test_grade_short_raises():
    with pytest.raises(ValueError):
        grade_objective({"type": "short", "answer": "要点"}, "作答")


def test_question_schema_constant_exposes_required_fields():
    required = set(QUESTION_SCHEMA["required"])
    assert {"type", "stem", "answer", "explanation", "kc_names", "difficulty"} <= required


def tools_for(state=None, gateway=None):
    record = TaskRecord(state=state or base_state(), status="running")
    tools = QuizTools(
        EducationDeps(Settings(_env_file=None), gateway or FakeGateway([])), record, lambda _: None
    )
    return (tools, record)


@pytest.mark.parametrize(
    "count,mix", [(1, {"easy": 1}), (3, {"easy": 1, "mid": 1, "hard": 1}), (20, {"mid": 20})]
)
async def test_blueprint_repaired_to_requested_count_and_mix(count, mix):
    tools, record = tools_for(base_state(num_questions=count, difficulty_mix=mix))
    await tools.handlers["submit_blueprint"]({"blueprint": {"items": []}})
    assert record.status == "awaiting_confirmation"
    items = record.pending["blueprint"]["items"]
    assert len(items) == count
    assert {k: sum(x["difficulty"] == k for x in items) for k in mix} == mix
    assert "submit_quiz" not in tools.handlers


async def test_rejected_blueprint_never_generates_and_modified_blueprint_is_authoritative():
    tools, record = tools_for()
    await tools.submit_blueprint({"blueprint": {"items": TWO_ITEMS}})
    tools.confirm({"approved": False})
    assert record.status == "cancelled" and (not record.state.get("questions"))
    tools, record = tools_for()
    await tools.submit_blueprint({"blueprint": {"items": TWO_ITEMS}})
    tools.confirm(
        {"approved": True, "blueprint": {"items": [bp_item(qtype="blank", difficulty="hard")]}}
    )
    assert record.state["num_questions"] == 1
    assert record.state["blueprint"]["items"][0]["difficulty"] == "hard"
    assert "submit_quiz" in QuizTools(tools.deps, record, lambda _: None).handlers


@pytest.mark.parametrize(
    "change",
    [
        {"difficulty": "hard"},
        {"type": "blank", "options": []},
        {"kc_names": ["不在范围"]},
        {"answer": "E"},
        {"options": ["A", "B"]},
    ],
)
def test_invalid_questions_and_blueprint_deviation_rejected(change):
    state = base_state(
        num_questions=1, blueprint={"items": [bp_item()]}, questions=[good_q(**change)]
    )
    assert validate_questions(state)["validation_errors"]


@pytest.mark.parametrize(
    "raw,expected", [([], "generation_failed"), ([good_q(answer="E")], "validation_failed")]
)
def test_validation_retries_have_explicit_terminal_outcome(raw, expected):
    state = base_state(num_questions=1, questions=raw, ever_generated_nonempty=bool(raw))
    for _ in range(3):
        state.update(validate_questions(state))
    assert state["retry_count"] == 3 and state["terminal_outcome"] == expected


async def test_quiz_source_ids_only_resolve_real_evidence():
    gateway = FakeGateway([], critic_texts=[verify_json()])
    tools, record = tools_for(
        base_state(
            num_questions=1,
            blueprint_confirmed=True,
            blueprint={"items": [bp_item()]},
            evidences=[{"chunk_id": "real", "text": "素材", "heading_path": "教材"}],
        ),
        gateway,
    )
    await tools.submit_quiz({"questions": [good_q(source_chunk_id="fake")]})
    assert record.state["questions"][0]["source_chunk_id"] is None


@pytest.mark.parametrize(
    "critics,events",
    [
        ([verify_json()], ["passed"]),
        (["invalid"], ["degraded"]),
        ([verify_json([{"question_idx": 1, "severity": "minor", "reason": "建议"}])], ["passed"]),
        (
            [verify_json([{"question_idx": 1, "severity": "major", "reason": "题干歧义"}])] * 2,
            ["failed", "exhausted"],
        ),
    ],
)
async def test_independent_critic_has_bounded_repair_and_visible_degradation(critics, events):
    gateway = FakeGateway([], critic_texts=critics)
    state = base_state(questions=[good_q()])
    actual = []
    for _ in events:
        result = await review_questions(EducationDeps(Settings(_env_file=None), gateway), state)
        state.update(result)
        actual.append(result["trace"][0]["event"])
    assert actual == events


async def test_critic_budget_failure_propagates():
    gateway = FakeGateway([], critic_errors=[BudgetExceeded()])
    with pytest.raises(BudgetExceeded):
        await review_questions(
            EducationDeps(Settings(_env_file=None), gateway), base_state(questions=[good_q()])
        )
