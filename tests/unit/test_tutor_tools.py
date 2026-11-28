"""tutor 领域工具的业务权限、校验与修复；原生执行覆盖见 test_native_agent_tasks.py。"""

import json
from types import SimpleNamespace

import pytest
from eduagents_core.config import Settings
from eduagents_core.education.context import EducationDeps
from eduagents_core.education.tutor import TutorTools
from eduagents_core.gateway.base import ChatRequest, ChatResult
from eduagents_core.harness.task import TaskRecord

BAN = "绝对禁止给出最终答案"
PERSIST = "索要最终答案"
PROBLEM = "求 f(x)=x^3-3x 的极值点"
SUMMARY = {
    "misconceptions": ["把导数为零当成极值的充分条件"],
    "review_suggestions": ["重做 3 道判定极值的基础题"],
}


class FakeGateway:
    """脚本化网关: 按 feature 排队出词 (末项粘性可复用), 记录全部请求供断言 system prompt。"""

    def __init__(self, script: dict[str, list] | None = None):
        self.requests: list[ChatRequest] = []
        self._script = {k: list(v) for k, v in (script or {}).items()}

    async def chat(self, req: ChatRequest) -> ChatResult:
        self.requests.append(req)
        queue = self._script.get(req.feature, [])
        if not queue:
            payload = "{}"
        elif len(queue) == 1:
            payload = queue[0]
        else:
            payload = queue.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return ChatResult(
            text=text,
            model="fake",
            tier=str(req.tier),
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="replay",
        )


def classify(stuck="concept", asked=False, solved=False) -> dict:
    return {"stuck_type": stuck, "asked_for_answer": asked, "solved": solved}


@pytest.mark.parametrize(
    "band,expected", [("weak", 0), ("developing", 0), ("near", 1), ("mastered", 2)]
)
async def test_tutor_hint_progression_and_once_per_turn_assessment(band, expected):
    record = TaskRecord(state={"mastery_band": band, "problem": PROBLEM, "messages": []})
    for turn in range(4):
        record.status = "running"
        record.state["_active_request"] = str(turn)
        record.state["messages"].append({"role": "user", "content": "不会"})
        tools = TutorTools(
            EducationDeps(Settings(_env_file=None), FakeGateway()), record, lambda _: None
        )
        tools.prepare_turn()
        await tools.assess(classify())
        level = record.state["hint_level"]
        await tools.assess(classify())
        assert record.state["hint_level"] == level == min(expected + turn, 3)
        assert (BAN in tools.policy()) == (level < 3)
        await tools.submit({"reply": "提示"})
        assert record.state["messages"][-1]["role"] == "assistant"


async def test_two_explicit_answer_requests_grant_a_solution_despite_mistaken_model_labels():
    record = TaskRecord(
        state={
            "problem": PROBLEM,
            "hint_level": 0,
            "messages": [],
            "answer_requests": 1,
        },
        status="running",
    )
    gateway = FakeGateway()
    deps = EducationDeps(Settings(_env_file=None), gateway)
    turns = [
        ("请直接给我最终答案", False, 0, 1),
        ("请给我一个提示", True, 1, 1),
        ("请给我完整的解法", False, 3, 2),
    ]
    for turn, (message, model_asked, level, count) in enumerate(turns):
        record.state["_active_request"] = str(turn)
        record.state["messages"].append({"role": "user", "content": message})
        tools = TutorTools(deps, record, lambda _: None)
        tools.prepare_turn()
        result = await tools.assess(classify(asked=model_asked))
        assert result["hint_level"] == level
        assert result["allow_complete_answer"] is (turn == 2)
        assert record.state["answer_requests"] == count
        assert record.state["persist_gently"] == (turn == 0)
        assert (BAN in tools.policy()) is (turn != 2)
    assert not gateway.requests


@pytest.mark.parametrize("message", [
    "请给我一个提示", "请告诉我答案对不对？", "请给我答案的提示。",
    "不要直接说答案，告诉我怎么自查", "我还想自己找答案，下一步该怎么检查？",
    "Please give me a hint", "Can you check my answer?", "Don't give me the answer",
])
async def test_hints_and_self_checks_advance_without_using_an_answer_request(message):
    gateway = FakeGateway()
    record = TaskRecord(state={
        "problem": PROBLEM, "_active_request": "current-turn", "answer_requests": 2,
        "messages": [
            {"role": "user", "content": "这一题我想试试"},
            {"role": "assistant", "content": "先看一看要求的条件。"},
            {"role": "user", "content": message},
        ],
    }, status="running")
    tools = TutorTools(EducationDeps(Settings(_env_file=None), gateway), record, lambda _: None)
    result = await tools.assess(classify(asked=True, solved=True))
    assert result["hint_level"] == 1 and not result["allow_complete_answer"] and not result["solved"]
    assert record.state["answer_requests"] == 0 and not record.state["persist_gently"]
    assert not gateway.requests


@pytest.mark.parametrize("failed_check", [None, "complete", "correct", "independent"])
async def test_only_verified_complete_independent_solution_allows_qualitative_summary(failed_check):
    solution = (
        "先求导得 $3x^2-3$，令其为零得 $x=-1,1$。导数在 $-1$ 左侧为正、右侧为负，"
        "在 $1$ 左侧为负、右侧为正。因此 $(-1,2)$ 为极大值点，$(1,-2)$ 为极小值点。"
    )
    verdict = {key: key != failed_check for key in ("complete", "correct", "independent")}
    gateway = FakeGateway({"tutor_verify_solution": [verdict]})
    verified = failed_check is None
    record = TaskRecord(state={
        "problem": PROBLEM, "_active_request": "solution-turn", "hint_level": 1,
        "messages": [{"role": "user", "content": solution}],
    }, status="running")
    tools = TutorTools(EducationDeps(Settings(_env_file=None), gateway), record, lambda _: None)
    result = await tools.assess({**classify(solved=True), "student_solution": solution})
    assert result["solved"] is verified
    [review] = gateway.requests
    assert review.feature == "tutor_verify_solution"
    assert json.loads(review.messages[-1].content)["student_solution"] == solution
    await tools.submit(
        {"reply": "用练习检验理解", "summary": SUMMARY | {"p_mastery": 1, "score": 100}}
    )
    assert bool(record.state.get("session_summary")) is verified
    if verified:
        assert json.loads(record.state["session_summary"]) == SUMMARY
    assert "p_mastery" not in record.state


@pytest.mark.parametrize("message", ["我已经理解了，谢谢！", "请给我一个提示", "我完全答对了"])
async def test_claim_or_hint_cannot_be_used_as_student_solution(message):
    gateway = FakeGateway()
    record = TaskRecord(state={
        "problem": PROBLEM, "_active_request": "no-solution", "messages": [
            {"role": "user", "content": message},
        ],
    }, status="running")
    tools = TutorTools(EducationDeps(Settings(_env_file=None), gateway), record, lambda _: None)
    result = await tools.assess({**classify(solved=True), "student_solution": message})
    assert not result["solved"] and not gateway.requests
    await tools.submit({"reply": "你能说明理由吗？", "summary": SUMMARY})
    assert not record.state.get("session_summary")


async def test_reply_requires_assessment_and_retry_does_not_advance_hint_again():
    record = TaskRecord(
        state={"_active_request": "request", "messages": [{"role": "user", "content": "不会"}]},
        status="running",
    )
    deps = EducationDeps(Settings(_env_file=None), FakeGateway())
    tools = TutorTools(deps, record, lambda _: None)
    assert not (await tools.submit({"reply": "提示"}))["accepted"]
    await tools.assess(classify())
    retry = TutorTools(deps, record, lambda _: None)
    await retry.assess(classify(asked=True))
    assert record.state["hint_level"] == 0 and (not record.state.get("answer_requests"))


async def test_tutor_repair_reads_only_material_from_the_current_authorized_search():
    class Retrieval:
        async def retrieve(self, owner, kb_id, query):
            assert (owner, kb_id, query) == ("learner", "course", "等腰三角形")
            chunk = SimpleNamespace(
                chunk_id="real-chunk", doc_id="doc", text="等腰三角形的两腰相等。",
                heading_path="三角形", payload={"kb_id": "course"}, page_start=1, score=0.8,
            )
            return SimpleNamespace(chunks=[chunk], confident=True)

    record = TaskRecord(state={
        "tenant_id": "learner", "kb_id": "course", "_active_request": "turn",
        "problem": "如何辨认等腰三角形？", "messages": [{"role": "user", "content": "我不确定"}],
    }, status="running")
    tools = TutorTools(EducationDeps(Settings(_env_file=None), FakeGateway(), retrieval=Retrieval()),
                       record, lambda _: None)
    tools.prepare_turn()
    assert tools.repair_tool_call()[0] == "assess_student_turn"
    await tools.handlers["assess_student_turn"](classify())
    await tools.handlers["search_knowledge_base"]({"queries": ["等腰三角形"]})
    name, evidence, parameters = tools.repair_tool_call()
    assert name == "read_material"
    assert evidence["available_materials"] == [{"id": "real-chunk", "heading": "三角形"}]
    assert parameters["properties"]["ids"]["items"]["enum"] == ["real-chunk"]
    assert (await tools.handlers["read_material"]({"ids": ["other-tenant"]}))["sources"] == []
    assert not (await tools.handlers["submit_tutor_reply"]({"reply": "提示"}))["accepted"]

    await tools.handlers["read_material"]({"ids": ["real-chunk"]})
    name, evidence, parameters = tools.repair_tool_call()
    assert name == "submit_tutor_reply"
    assert evidence["materials"] == [{"heading": "三角形", "text": "等腰三角形的两腰相等。"}]
    assert "source_ids" not in parameters["properties"]
    assert (await tools.handlers["submit_tutor_reply"]({"reply": "想想哪些边可能相等？"}))["accepted"]

    record.status = "running"
    record.state["_active_request"] = "new-turn"
    next_turn = TutorTools(tools.deps, record, lambda _: None)
    next_turn.prepare_turn()
    assert (await next_turn.handlers["read_material"]({"ids": ["real-chunk"]}))["sources"] == []
