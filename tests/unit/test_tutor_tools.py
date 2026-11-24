"""tutor 领域工具的业务权限、校验与修复；原生执行覆盖见 test_native_agent_tasks.py。"""

import json

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


async def test_answer_requests_keep_first_hint_then_grant_on_second_and_summary_is_qualitative():
    record = TaskRecord(
        state={
            "problem": PROBLEM,
            "hint_level": 0,
            "messages": [{"role": "user", "content": "答案"}],
        },
        status="running",
    )
    for turn in range(2):
        record.state["_active_request"] = str(turn)
        tools = TutorTools(
            EducationDeps(Settings(_env_file=None), FakeGateway()), record, lambda _: None
        )
        await tools.assess(classify(asked=True))
        assert record.state["hint_level"] == (0 if turn == 0 else 3)
        assert record.state["persist_gently"] == (turn == 0)
    tools._assessed = False
    await tools.assess(classify(solved=True))
    await tools.submit(
        {"reply": "用练习检验理解", "summary": SUMMARY | {"p_mastery": 1, "score": 100}}
    )
    summary = json.loads(record.state["session_summary"])
    assert set(summary) == {"misconceptions", "review_suggestions"}
    assert "p_mastery" not in record.state


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
