"""tutor_graph (F4 苏格拉底辅导) 单测: 多轮驱动 (同 thread_id 连续 ainvoke), 无网络。

核心断言: hint_level 状态机递进、F4-A1 结构性禁令注入 system prompt、索答两段式、solved 小结。
"""

import json

from eduagents_core.config import Settings
from eduagents_core.gateway.base import ChatRequest, ChatResult
from eduagents_core.harness.workflow import MemoryStore
from eduagents_core.workflows.state import WorkflowDeps
from eduagents_core.workflows.tutor import build_tutor_workflow

BAN = "绝对禁止给出最终答案"  # F4-A1 结构性禁令 (代码注入 system prompt)
PERSIST = "索要最终答案"  # 温和坚持话术指令标记
PROBLEM = "求 f(x)=x^3-3x 的极值点"
SUMMARY = {"misconceptions": ["把导数为零当成极值的充分条件"], "review_suggestions": ["重做 3 道判定极值的基础题"]}


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


def make_graph(script: dict):
    gateway = FakeGateway(script)
    deps = WorkflowDeps(settings=Settings(_env_file=None), gateway=gateway)
    graph = build_tutor_workflow(deps).with_store(MemoryStore())
    config = {"configurable": {"thread_id": "tutor-t1"}}
    return gateway, graph, config


async def run_round(graph, config, text: str) -> dict:
    return await graph.ainvoke(
        {
            "tenant_id": "t-1",
            "user_id": "u-1",
            "request_id": "r-1",
            "problem": PROBLEM,
            "messages": [{"role": "user", "content": text}],
        },
        config,
    )


def respond_systems(gateway: FakeGateway) -> list[str]:
    """每次 respond 调用送入网关的 system prompt (按轮次顺序)。"""
    return [
        req.messages[0].content
        for req in gateway.requests
        if req.feature == "tutor_respond" and req.messages[0].role == "system"
    ]


async def test_hint_level_progresses_0_1_2_with_ban_in_every_prompt():
    gateway, graph, config = make_graph(
        {"tutor_classify": [classify()], "tutor_respond": ["我们先看看你的思路?"]}
    )
    levels = []
    for text in ["这道题我不会", "还是没思路", "然后呢?"]:
        state = await run_round(graph, config, text)
        levels.append(state["hint_level"])
        assert state["reply"] == "我们先看看你的思路?"
    assert levels == [0, 1, 2]  # 首轮开场 0, 之后逐轮递进
    systems = respond_systems(gateway)
    assert len(systems) == 3
    assert all("数学排版协议" in s and "`$...$`" in s and "`$$...$$`" in s for s in systems)
    # F4-A1: hint_level < 3 的每一轮, system prompt 都被强制注入禁答令
    assert all(BAN in s for s in systems)


async def test_fourth_round_reaches_level3_and_ban_removed():
    gateway, graph, config = make_graph(
        {"tutor_classify": [classify()], "tutor_respond": ["回复"]}
    )
    state = {}
    for text in ["第1轮", "第2轮", "第3轮", "第4轮"]:
        state = await run_round(graph, config, text)
    assert state["hint_level"] == 3  # 第 4 轮才到 3
    systems = respond_systems(gateway)
    assert all(BAN in s for s in systems[:3])
    assert BAN not in systems[3]  # 第 3 层是完整解答层, 无禁令
    assert "完整" in systems[3]


async def test_answer_request_first_time_no_jump_and_persist_wording():
    gateway, graph, config = make_graph(
        {
            "tutor_classify": [classify(), classify(asked=True)],
            "tutor_respond": ["回复"],
        }
    )
    state1 = await run_round(graph, config, "这道题怎么做?")
    assert state1["hint_level"] == 0
    state2 = await run_round(graph, config, "直接告诉我答案吧")
    # 第 1 次索答: 层级不跳, 走温和坚持话术, 且禁令仍在
    assert state2["hint_level"] == 0
    assert state2["answer_requests"] == 1
    systems = respond_systems(gateway)
    assert PERSIST not in systems[0]
    assert PERSIST in systems[1]
    assert BAN in systems[1]


async def test_answer_request_second_time_jumps_to_level3():
    gateway, graph, config = make_graph(
        {
            "tutor_classify": [classify(), classify(asked=True), classify(asked=True)],
            "tutor_respond": ["回复"],
        }
    )
    for text in ["不会做", "给我答案", "别绕了, 就要答案"]:
        state = await run_round(graph, config, text)
    assert state["hint_level"] == 3
    assert state["answer_requests"] == 2
    systems = respond_systems(gateway)
    assert BAN not in systems[2]  # 第 2 次索答直接完整解答
    assert PERSIST not in systems[2]


async def test_solved_routes_to_summarize_with_session_summary():
    gateway, graph, config = make_graph(
        {
            "tutor_classify": [classify(), classify(solved=True)],
            "tutor_respond": ["回复"],
            "tutor_summarize": [SUMMARY],
        }
    )
    await run_round(graph, config, "没思路")
    state = await run_round(graph, config, "我懂了, 极值点是 x=±1, 谢谢!")
    summary = json.loads(state["session_summary"])
    assert summary["misconceptions"] == SUMMARY["misconceptions"]
    assert summary["review_suggestions"] == SUMMARY["review_suggestions"]
    # solved 轮不再生成辅导回复
    assert len(respond_systems(gateway)) == 1
    assert any(e.get("event") == "solved_to_summarize" for e in state["trace"])


async def test_classify_garbage_falls_back_to_unknown_and_still_responds():
    gateway, graph, config = make_graph(
        {"tutor_classify": ["这不是 JSON"], "tutor_respond": ["回复"]}
    )
    state = await run_round(graph, config, "不会")
    assert state["stuck_type"] == "unknown"
    assert state["reply"] == "回复"
    assert state["hint_level"] == 0
    assert any(e.get("event") == "llm_parse_failed_fallback" for e in state["trace"])
    # 解析失败重试了一次: classify 请求 2 次
    assert sum(1 for r in gateway.requests if r.feature == "tutor_classify") == 2


async def test_messages_accumulate_across_rounds():
    _, graph, config = make_graph(
        {"tutor_classify": [classify()], "tutor_respond": ["老师回复"]}
    )
    await run_round(graph, config, "第一问")
    state = await run_round(graph, config, "第二问")
    roles = [m["role"] for m in state["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert state["messages"][0]["content"] == "第一问"
    assert state["messages"][-1]["content"] == "老师回复"
