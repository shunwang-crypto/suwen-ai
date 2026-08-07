"""quiz 子图单元测试 (F5): FakeGateway 脚本化, 全离线零网络。

覆盖: 蓝图生成 → interrupt 挂起 → approve 恢复 → 坏 schema 打回重试 → 修复通过;
拒绝蓝图直接结束; 蓝图数量程序性修正 (截断/补齐/兜底); resume 携带修改后蓝图;
重试耗尽; 检索素材 source_hint→source_chunk_id 映射; grade_objective 各题型正反例。
"""

from __future__ import annotations

import json

import pytest
from eduagents_core.config import Settings
from eduagents_core.gateway.base import BudgetExceeded, ChatRequest, ChatResult, Tier
from eduagents_core.graphs.quiz_graph import (
    QUESTION_SCHEMA,
    build_quiz_graph,
    grade_objective,
)
from eduagents_core.graphs.state import GraphDeps
from eduagents_core.retrieval.base import RetrievalResult, RetrievedChunk
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

# --- fakes ---


class FakeGateway:
    """预置响应文本队列, 逐次弹出; 记录收到的 ChatRequest 供断言。"""

    def __init__(
        self,
        texts: list[str],
        *,
        critic_texts: list[str] | None = None,
        critic_errors: list[Exception] | None = None,
    ) -> None:
        self.texts = list(texts)
        self.critic_texts = list(critic_texts or [])
        self.critic_errors = list(critic_errors or [])
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
        )


class FakeRetrieval:
    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[str, str, str]] = []

    async def retrieve(self, tenant_id: str, kb_id: str, query: str) -> RetrievalResult:
        self.calls.append((tenant_id, kb_id, query))
        return RetrievalResult(chunks=self.chunks)


def make_chunk(chunk_id: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=text,
        heading_path="第3章 > 3.2",
        doc_id="d1",
        page_start=1,
        page_end=1,
        score=0.9,
        source="fused",
    )


# --- 数据构造 ---


def bp_json(items: list[dict], summary: str = "测试蓝图") -> str:
    return json.dumps({"items": items, "summary": summary}, ensure_ascii=False)


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


def gen_json(questions: list[dict]) -> str:
    return json.dumps({"questions": questions}, ensure_ascii=False)


def verify_json(issues: list[dict] | None = None) -> str:
    return json.dumps(
        {"issues": issues or [], "summary": "题目质量合格"}, ensure_ascii=False
    )


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


def make_app(gateway: FakeGateway, retrieval: FakeRetrieval | None = None):
    deps = GraphDeps(settings=Settings(), gateway=gateway, retrieval=retrieval)
    return build_quiz_graph(deps).compile(checkpointer=MemorySaver())


def cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


TWO_ITEMS = [bp_item(difficulty="easy"), bp_item(kc="拉格朗日余项", qtype="blank", difficulty="mid")]
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


# --- 主流程 ---


async def test_happy_path_interrupt_resume_bad_schema_retry_then_pass():
    bad = good_q()
    bad.pop("explanation")  # 坏 schema: 缺 explanation
    gateway = FakeGateway(
        [
            bp_json(TWO_ITEMS),
            gen_json([good_q(), bad]),  # 第一轮: 1 题坏
            gen_json([good_q(), BLANK_Q]),  # 第二轮: 修复
        ]
    )
    app = make_app(gateway)
    config = cfg("happy")

    first = await app.ainvoke(base_state(), config)
    assert "__interrupt__" in first  # 蓝图确认处挂起
    payload = first["__interrupt__"][0].value
    assert payload["kind"] == "blueprint_confirm"
    assert len(payload["blueprint"]["items"]) == 2
    assert "questions" not in first  # 挂起时尚未生成

    final = await app.ainvoke(Command(resume={"approved": True}), config)
    assert final["blueprint_confirmed"] is True
    assert final["validation_errors"] == []
    assert final["retry_count"] == 1  # 恰好打回一轮
    assert len(final["questions"]) == 2
    assert final["questions"][1]["type"] == "blank"

    # trace: 失败一轮 (错误数>=1) 后通过
    validate_events = [e for e in final["trace"] if e["node"] == "validate"]
    assert [e["event"] for e in validate_events] == ["failed", "passed"]
    assert validate_events[0]["error_count"] >= 1

    # 网关调用: 蓝图 cheap + 生成 standard x2, 修复轮提示词带错误清单
    assert [r.feature for r in gateway.requests] == [
        "quiz_blueprint",
        "quiz_generate",
        "quiz_generate",
        "quiz_verify",
    ]
    assert gateway.requests[0].tier == Tier.cheap
    assert all(r.json_mode for r in gateway.requests)
    assert gateway.requests[1].tier == Tier.standard
    retry_prompt = gateway.requests[2].messages[0].content
    assert "上一轮输出未通过校验" in retry_prompt
    assert "explanation" in retry_prompt


async def test_reject_blueprint_ends_without_questions():
    gateway = FakeGateway([bp_json(TWO_ITEMS)])
    app = make_app(gateway)
    config = cfg("reject")

    first = await app.ainvoke(base_state(), config)
    assert "__interrupt__" in first
    final = await app.ainvoke(Command(resume={"approved": False}), config)

    assert final["blueprint_confirmed"] is False
    assert "questions" not in final  # 未生成任何题目
    assert len(gateway.requests) == 1  # 只调了蓝图, 未调生成
    assert any(
        e["node"] == "confirm_blueprint" and e["event"] == "rejected" for e in final["trace"]
    )


async def test_resume_with_modified_blueprint_overrides_and_renumbers():
    gateway = FakeGateway([bp_json(TWO_ITEMS), gen_json([BLANK_Q])])
    app = make_app(gateway)
    config = cfg("modify")

    await app.ainvoke(base_state(), config)
    modified = {"items": [bp_item(kc="拉格朗日余项", qtype="blank", difficulty="mid")], "summary": "改小"}
    final = await app.ainvoke(
        Command(resume={"approved": True, "blueprint": modified}), config
    )

    assert final["num_questions"] == 1  # 跟随修改后蓝图条数
    assert final["blueprint"]["items"] == modified["items"]
    assert final["validation_errors"] == []
    assert len(final["questions"]) == 1
    assert "拉格朗日余项" in gateway.requests[1].messages[0].content  # 生成用的是改后蓝图


async def test_validation_retry_exhausted_ends_with_errors():
    bad = good_q(answer="E")  # single 题 answer 越界, 永远修不好
    gateway = FakeGateway([bp_json([bp_item()]), gen_json([bad]), gen_json([bad]), gen_json([bad])])
    app = make_app(gateway)
    config = cfg("exhaust")

    await app.ainvoke(base_state(num_questions=1, difficulty_mix={"easy": 1}), config)
    final = await app.ainvoke(Command(resume={"approved": True}), config)

    assert final["retry_count"] == 3  # 首轮 + 2 次重试全失败
    assert final["validation_errors"]
    assert len(gateway.requests) == 4  # 1 蓝图 + 3 生成
    failed = [e for e in final["trace"] if e["node"] == "validate" and e["event"] == "failed"]
    assert len(failed) == 3
    assert failed[-1]["will_retry"] is False


# --- 蓝图数量程序性修正 ---


async def test_blueprint_truncated_when_llm_returns_too_many():
    four = [bp_item(kc=f"kc{i}") for i in range(4)]
    gateway = FakeGateway([bp_json(four)])
    app = make_app(gateway)
    config = cfg("trunc")

    first = await app.ainvoke(base_state(num_questions=2), config)
    items = first["__interrupt__"][0].value["blueprint"]["items"]
    assert len(items) == 2
    assert [it["kc_name"] for it in items] == ["kc0", "kc1"]
    state = app.get_state(config).values
    assert any(e["node"] == "make_blueprint" and e["corrected"] for e in state["trace"])


async def test_blueprint_padded_by_difficulty_mix_deficit():
    gateway = FakeGateway([bp_json([bp_item(difficulty="easy")])])  # 只给 1 条
    app = make_app(gateway)
    config = cfg("pad")

    first = await app.ainvoke(
        base_state(
            num_questions=3,
            difficulty_mix={"easy": 1, "mid": 1, "hard": 1},
            kc_names=["泰勒公式", "洛必达法则"],
        ),
        config,
    )
    items = first["__interrupt__"][0].value["blueprint"]["items"]
    assert len(items) == 3
    # 补齐按 mix 缺口: easy 已有 1, 追加 mid 与 hard; kc 轮转自 kc_names
    assert [it["difficulty"] for it in items[1:]] == ["mid", "hard"]
    assert {it["kc_name"] for it in items[1:]} <= {"泰勒公式", "洛必达法则"}


async def test_blueprint_unparseable_output_falls_back_programmatically():
    gateway = FakeGateway(["模型跑飞了, 这不是 JSON {"])
    app = make_app(gateway)
    config = cfg("fallback")

    first = await app.ainvoke(
        base_state(num_questions=2, difficulty_mix={"mid": 2}, kc_names=["泰勒公式"]), config
    )
    blueprint = first["__interrupt__"][0].value["blueprint"]
    assert len(blueprint["items"]) == 2
    assert all(it["kc_name"] == "泰勒公式" for it in blueprint["items"])
    assert all(it["difficulty"] == "mid" for it in blueprint["items"])
    assert blueprint["summary"]  # 程序性 summary 兜底非空


# --- 检索素材溯源 ---


async def test_kb_materials_and_source_hint_mapping():
    retrieval = FakeRetrieval([make_chunk("c1", "泰勒公式素材"), make_chunk("c2", "余项素材")])
    gateway = FakeGateway(
        [
            bp_json(TWO_ITEMS),
            gen_json([good_q(source_hint=2), dict(BLANK_Q, source_hint=99)]),
        ]
    )
    app = make_app(gateway, retrieval=retrieval)
    config = cfg("kb")

    await app.ainvoke(base_state(kb_id="kb1"), config)
    final = await app.ainvoke(Command(resume={"approved": True}), config)

    assert retrieval.calls == [
        ("t1", "kb1", "泰勒公式"),
        ("t1", "kb1", "泰勒公式"),
    ]
    gen_prompt = gateway.requests[1].messages[0].content
    assert "[素材 1]" in gen_prompt and "泰勒公式素材" in gen_prompt
    assert final["questions"][0]["source_chunk_id"] == "c2"  # hint=2 -> 第 2 条 chunk
    assert final["questions"][1]["source_chunk_id"] is None  # 越界 hint 置 None


# --- 独立质量评审 (critic-guided) ---


async def test_critic_major_issue_retries_generate_once_then_passes():
    gateway = FakeGateway(
        [
            bp_json([bp_item()]),
            gen_json([good_q()]),
            gen_json([good_q()]),
        ],
        critic_texts=[
            verify_json(
                [
                    {
                        "question_idx": 1,
                        "severity": "major",
                        "reason": "easy 题需要多步复合推导，难度标注明显不符",
                    }
                ]
            ),
            verify_json(),
        ],
    )
    app = make_app(gateway)
    config = cfg("critic-major")

    await app.ainvoke(base_state(num_questions=1, difficulty_mix={"easy": 1}), config)
    final = await app.ainvoke(Command(resume={"approved": True}), config)

    assert final["critic_retry_count"] == 1
    assert final["critic_issues"] == []
    assert [request.feature for request in gateway.requests] == [
        "quiz_blueprint",
        "quiz_generate",
        "quiz_verify",
        "quiz_generate",
        "quiz_verify",
    ]
    repair_prompt = gateway.requests[3].messages[0].content
    assert "独立质量评审要求返工" in repair_prompt
    assert "难度标注明显不符" in repair_prompt
    critic_events = [event for event in final["trace"] if event["node"] == "critic_review"]
    assert [event["event"] for event in critic_events] == ["failed", "passed"]


async def test_critic_minor_issue_does_not_trigger_rework():
    gateway = FakeGateway(
        [bp_json([bp_item()]), gen_json([good_q()])],
        critic_texts=[
            verify_json(
                [{"question_idx": 1, "severity": "minor", "reason": "干扰项可再打磨"}]
            )
        ],
    )
    app = make_app(gateway)
    config = cfg("critic-minor")

    await app.ainvoke(base_state(num_questions=1, difficulty_mix={"easy": 1}), config)
    final = await app.ainvoke(Command(resume={"approved": True}), config)

    assert final.get("critic_retry_count", 0) == 0
    assert [request.feature for request in gateway.requests] == [
        "quiz_blueprint",
        "quiz_generate",
        "quiz_verify",
    ]
    event = [event for event in final["trace"] if event["node"] == "critic_review"][-1]
    assert event["event"] == "passed"
    assert event["issues"] == 1


async def test_critic_retry_exhaustion_is_explicit_and_does_not_loop():
    gateway = FakeGateway(
        [
            bp_json([bp_item()]),
            gen_json([good_q()]),
            gen_json([good_q()]),
        ],
        critic_texts=[
            verify_json([{"question_idx": 1, "severity": "major", "reason": "题干有歧义"}]),
            verify_json([{"question_idx": 1, "severity": "major", "reason": "仍有歧义"}]),
        ],
    )
    app = make_app(gateway)
    config = cfg("critic-exhausted")

    await app.ainvoke(base_state(num_questions=1, difficulty_mix={"easy": 1}), config)
    final = await app.ainvoke(Command(resume={"approved": True}), config)

    assert final["critic_retry_count"] == 1
    assert final["critic_issues"] == []
    assert len(final["questions"]) == 1
    critic_events = [event for event in final["trace"] if event["node"] == "critic_review"]
    assert [event["event"] for event in critic_events] == ["failed", "exhausted"]
    assert len(gateway.requests) == 5


async def test_critic_invalid_response_and_gateway_error_degrade_to_valid_questions():
    for suffix, critic_texts, critic_errors, reason in (
        ("parse", ["not-json {"], [], "invalid_response"),
        ("gateway", [], [RuntimeError("fake failure")], "gateway_error"),
    ):
        gateway = FakeGateway(
            [bp_json([bp_item()]), gen_json([good_q()])],
            critic_texts=critic_texts,
            critic_errors=critic_errors,
        )
        app = make_app(gateway)
        config = cfg(f"critic-degraded-{suffix}")
        await app.ainvoke(base_state(num_questions=1, difficulty_mix={"easy": 1}), config)
        final = await app.ainvoke(Command(resume={"approved": True}), config)

        assert len(final["questions"]) == 1
        event = [event for event in final["trace"] if event["node"] == "critic_review"][-1]
        assert event["event"] == "degraded"
        assert event["reason"] == reason


async def test_critic_budget_exceeded_is_not_swallowed():
    gateway = FakeGateway(
        [bp_json([bp_item()]), gen_json([good_q()])],
        critic_errors=[BudgetExceeded(retry_after_seconds=60)],
    )
    app = make_app(gateway)
    config = cfg("critic-budget")
    await app.ainvoke(base_state(num_questions=1, difficulty_mix={"easy": 1}), config)

    with pytest.raises(BudgetExceeded):
        await app.ainvoke(Command(resume={"approved": True}), config)


# --- grade_objective 纯函数 ---


def test_grade_single_case_and_whitespace_insensitive():
    q = good_q(answer="B")
    assert grade_objective(q, " b ") is True
    assert grade_objective(q, "B") is True
    assert grade_objective(q, "A") is False
    assert grade_objective(q, "") is False


def test_grade_multi_set_equality():
    q = good_q(type="multi", answer="A,C")
    assert grade_objective(q, "C,A") is True  # 顺序无关
    assert grade_objective(q, " c , a ") is True
    assert grade_objective(q, "A") is False  # 少选
    assert grade_objective(q, "A,B,C") is False  # 多选
    assert grade_objective(q, "") is False


def test_grade_blank_exact_with_alternatives():
    q = {"type": "blank", "answer": "1+x | $1+x$"}
    assert grade_objective(q, "1+x") is True
    assert grade_objective(q, "  $1+x$  ") is True  # 去首尾空白
    assert grade_objective(q, "1 + x") is False  # 精确匹配, 不吞内部空格
    assert grade_objective(q, "x+1") is False


def test_grade_short_raises():
    with pytest.raises(ValueError):
        grade_objective({"type": "short", "answer": "要点"}, "作答")


def test_question_schema_constant_exposes_required_fields():
    required = set(QUESTION_SCHEMA["required"])
    assert {"type", "stem", "answer", "explanation", "kc_names", "difficulty"} <= required
