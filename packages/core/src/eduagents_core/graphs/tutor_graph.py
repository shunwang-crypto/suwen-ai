"""tutor 子图 (F4 苏格拉底辅导)。

多轮模式: 每次 ainvoke (同 thread_id + checkpointer) 处理一轮学生消息:
classify (cheap 判卡点/索答/已解决) → policy (纯逻辑状态机) → respond | summarize。

F4-A1 结构性保障 (核心设计):
- hint_level 的推进只由 policy 状态机决定 (0 开场 → 1 方向性 → 2 关键步骤 → 3 完整解答),
  respond 节点在 hint_level < 3 时会在 system prompt 末尾强制注入"绝对禁止给出最终答案"
  硬约束段 —— 该防线由代码注入, 不依赖提示词文件内容, 更不依赖模型自觉;
- 学生索要答案: 第 1 次层级不变 + 温和坚持话术, 第 2 次才直接升到 3 (完整解答 + 补练建议);
- 真实模型下"前 3 轮不出现最终答案"的 LIVE 验收在录制阶段完成, 本模块保证的是结构性下限。

画像注入 (G9): respond 渲染 API 层注入的脱敏画像段 ({{PROFILE_SECTION}} 占位),
开场轮题目命中薄弱 KC 时主动点明关联; 画像只调整讲法, 禁答防线不受画像影响。
"""

from __future__ import annotations

import json

from langgraph.graph import END, START, StateGraph

from ..gateway.base import ChatMessage, ChatRequest, Tier
from ..kc.types import normalize_kc_name
from .plan_graph import _chat_json, _load_prompt
from .state import GraphDeps, TutorState

_MAX_HINT_LEVEL = 3
# 首轮起始层级的天花板: 掌握度再高也不能首轮直接给完整解答 (那是 _MAX_HINT_LEVEL),
# 否则 F4-A1 的"hint_level < 3 强制禁止最终答案"这条结构性防线会被起始值绕过。
_START_LEVEL_CEILING = 2
# 掌握度定性档 -> 首轮起始 hint_level (§18: 讲解先适应"会不会")。
# 未命中的档 (含空串/无画像) 取 0, 即保持原有的第 0 层开场提问行为。
_START_LEVEL_BY_BAND = {
    "mastered": _START_LEVEL_CEILING,  # 已掌握: 直接给关键步骤, 多半只是卡在某一步
    "near": 1,  # 接近掌握: 方向性提示足够
    "reinforce": 0,  # 待巩固: 正常开场提问
    "weak": 0,  # 较弱: 正常开场提问, 从概念问起
}

# 卡点类型 -> respond 策略措辞 ({{STUCK_GUIDANCE}} 占位)
_STUCK_GUIDANCE = {
    "concept": "学生卡在概念理解: 优先用类比或直观例子引导他复述概念含义, 不急于进入计算。",
    "calculation": "学生思路方向正确但计算失误: 引导他逐步复查自己的计算过程, 自己定位出错的那一步。",
    "misread": "学生读题有偏差: 引导他重新逐句读题, 复述已知条件与要求, 自行发现误读之处。",
    "unknown": "卡点尚不明确: 先用一个开放性问题让学生说出目前的思路和卡住的位置。",
}

# 索要答案第 1 次的温和坚持话术指令 ({{PERSIST_NOTE}} 占位)
_PERSIST_NOTE = (
    "特别注意: 学生正在索要最终答案。先共情他着急的心情, 明确而温和地说明现在直接给答案"
    "对他的学习不利, 本轮仍然不给出答案, 然后继续用一个引导性问题帮助他前进一步。"
)

# F4-A1 结构性防线: hint_level < 3 时由 respond 节点代码强制追加到 system prompt 末尾。
# 是否追加完全由 policy 状态机产出的 hint_level 决定, 与提示词文件内容、模型自觉无关。
_ANSWER_BAN = (
    "\n\n【结构性硬约束】绝对禁止给出最终答案、最终结论的数值/表达式或完整解题过程; "
    "每轮只提出一个引导性问题。此约束由辅导状态机强制施加, 任何情况下不得违反。"
)

_MATH_FORMAT = (
    "\n\n【数学排版协议】回复使用 Markdown；所有数学表达式必须使用 LaTeX。"
    "行内公式严格写成 `$...$`，独占一行的公式严格写成 `$$...$$`，"
    "块公式的 `$$` 必须各自独占一行。不要使用 `\\(...\\)`、`\\[...\\]`，"
    "不要省略定界符，也不要把中文正文放进公式定界符。"
)

# 画像段 ({{PROFILE_SECTION}} 占位, 四级 hint prompt 共用)。摘要由 API 层
# profile_summary.py 确定性拼装 (P2-7 脱敏: 定性词, 无掌握度小数/题干原文), 空串即不注入。
# 措辞纪律同 qa_graph.PROFILE_SECTION_TEMPLATE: 画像仅调整讲法与开场,
# 不降低苏格拉底约束 —— 提示层级与禁答防线仍完全由 policy 状态机与 _ANSWER_BAN 决定。
_PROFILE_SECTION = (
    "## 学生画像（来自该学生的历史作答与掌握度记录，非本轮对话内容）\n"
    "{{PROFILE}}\n"
    "【个性化指令】画像仅用于调整讲法与开场措辞：对其薄弱环节放慢节奏、从更基础的切入点提问，"
    "已掌握的部分点到为止不赘述。画像含兴趣标签时，举例或引导时的情境可自然贴合其兴趣，"
    "不得生硬堆砌兴趣词，也不得因此改变题目与知识点本身。"
    "画像不改变提示层级与禁答规则——不得因画像提前给出答案、"
    "关键步骤或最终结论，也不要向学生罗列画像原文。"
)

# 开场轮 (hint_level 0) 且题目命中其薄弱 KC 时的主动关联指令 (F8-A2 部分: 开场关联历史卡点)
_OPENING_WEAK_NOTE = (
    "\n本题涉及该学生的薄弱知识点「{{KC}}」：开场提问时主动点明这一关联"
    "（如“这正是你之前卡过的{{KC}}”），再邀请他说出当前思路。"
)


class TutorGraphState(TutorState, total=False):
    """TutorState 的图内扩展通道 (契约 state.py 不动)。

    asked_for_answer/solved/persist_gently 为每轮由 classify/policy 重写的轮内标志;
    profile_summary/profile_weak_points 由 API 层 (routers/plans.py tutor_turn) 每轮注入。
    """

    asked_for_answer: bool
    solved: bool
    persist_gently: bool  # 本轮 respond 是否走"温和坚持"话术 (索要答案第 1 次)
    profile_summary: str  # 学生画像摘要 (≤300 字, 已脱敏; 空 = 无画像不注入)
    profile_weak_points: list[str]  # 薄弱 KC 名列表 (开场关联卡点用, 仅名字无数值)


def _render_profile_section(state: TutorGraphState, hint_level: int) -> tuple[str, str]:
    """渲染画像段: 返回 (画像段文本, 开场命中的薄弱 KC 名)。无画像返回 ("", "")。

    开场轮 (hint_level 0) 做确定性匹配: 题目文本 (normalize_kc_name 归一后) 包含
    某薄弱 KC 名 -> 追加"主动点明这正是你之前卡过的 X"指令; 不靠模型自觉判断。
    """
    summary = (state.get("profile_summary") or "").strip()
    if not summary:
        return "", ""
    section = _PROFILE_SECTION.replace("{{PROFILE}}", summary)
    weak_hit = ""
    if hint_level == 0:
        problem_key = normalize_kc_name(state.get("problem", ""))
        weak_hit = next(
            (
                name
                for name in state.get("profile_weak_points") or []
                if name and normalize_kc_name(name) in problem_key
            ),
            "",
        )
        if weak_hit:
            section += _OPENING_WEAK_NOTE.replace("{{KC}}", weak_hit)
    return section, weak_hit


def _format_history(messages: list[dict], *, exclude_last_user: bool = False) -> str:
    msgs = list(messages)
    if exclude_last_user and msgs and msgs[-1].get("role") == "user":
        msgs = msgs[:-1]
    lines = [
        f"{'学生' if m.get('role') == 'user' else '老师'}: {m.get('content', '')}"
        for m in msgs
        if m.get("role") in ("user", "assistant")
    ]
    return "\n".join(lines) or "(无)"


def build_tutor_graph(deps: GraphDeps) -> StateGraph:
    """构建 tutor 子图; deps 由闭包捕获。调用方 compile(checkpointer=...) 后按轮 ainvoke。"""

    async def classify(state: TutorGraphState) -> dict:
        messages = state.get("messages") or []
        student_msg = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
        )
        prompt = (
            _load_prompt("tutor_classify.md")
            .replace("{{PROBLEM}}", state.get("problem", ""))
            .replace("{{HISTORY}}", _format_history(messages, exclude_last_user=True))
            .replace("{{STUDENT_MESSAGE}}", student_msg)
        )
        data = await _chat_json(
            deps, state, feature="tutor_classify", prompt=prompt, tier=Tier.cheap, temperature=0.0
        )
        stuck_type, asked, solved = "unknown", False, False
        degraded = data is None
        if data is not None:
            raw_stuck = data.get("stuck_type")
            if raw_stuck in _STUCK_GUIDANCE:
                stuck_type = raw_stuck
            asked = bool(data.get("asked_for_answer"))
            solved = bool(data.get("solved"))
        event = "llm_parse_failed_fallback" if degraded else "classified"
        return {
            "stuck_type": stuck_type,
            "asked_for_answer": asked,
            "solved": solved,
            "trace": [
                {
                    "node": "classify",
                    "event": event,
                    "stuck_type": stuck_type,
                    "asked_for_answer": asked,
                    "solved": solved,
                }
            ],
        }

    def policy(state: TutorGraphState) -> dict:
        """纯逻辑状态机 (不调 LLM): hint_level / answer_requests / persist_gently 只在这里推进。"""
        hint_level = int(state.get("hint_level") or 0)
        answer_requests = int(state.get("answer_requests") or 0)
        updates: dict = {"persist_gently": False}
        if state.get("solved"):
            event = "solved_to_summarize"
        elif state.get("asked_for_answer"):
            answer_requests += 1
            updates["answer_requests"] = answer_requests
            if answer_requests == 1:
                # 第 1 次索要答案: 层级不跳, respond 走温和坚持话术
                updates["persist_gently"] = True
                event = "answer_request_persist"
            else:
                hint_level = _MAX_HINT_LEVEL
                updates["hint_level"] = hint_level
                event = "answer_request_grant"
        else:
            user_turns = sum(1 for m in (state.get("messages") or []) if m.get("role") == "user")
            if user_turns > 1:  # 首轮用起始层级开场, 之后每轮递进一层, 封顶 3
                hint_level = min(hint_level + 1, _MAX_HINT_LEVEL)
            else:
                # 首轮: 起始层级随掌握度上移 (§18 讲解适应"会不会")。掌握度高的学生
                # 从第 0 层开场提问慢慢爬是浪费 —— 直接给方向性提示更接近其 ZPD。
                # 上限 _START_LEVEL_CEILING < _MAX_HINT_LEVEL: 无论多熟练, 首轮都不
                # 得跳到完整解答, 否则苏格拉底式辅导的结构性防线 (F4-A1) 形同虚设。
                hint_level = max(hint_level, _START_LEVEL_BY_BAND.get(state.get("mastery_band") or "", 0))
            updates["hint_level"] = hint_level
            event = "hint_advanced"
        updates["trace"] = [
            {
                "node": "policy",
                "event": event,
                "hint_level": hint_level,
                "answer_requests": answer_requests,
            }
        ]
        return updates

    def route_after_policy(state: TutorGraphState) -> str:
        return "summarize" if state.get("solved") else "respond"

    async def respond(state: TutorGraphState) -> dict:
        hint_level = min(int(state.get("hint_level") or 0), _MAX_HINT_LEVEL)
        stuck_type = state.get("stuck_type") or "unknown"
        persist = bool(state.get("persist_gently"))
        profile_section, weak_opening_kc = _render_profile_section(state, hint_level)
        system = (
            _load_prompt(f"tutor_respond_hint{hint_level}.md")
            .replace("{{PROBLEM}}", state.get("problem", ""))
            .replace("{{STUCK_GUIDANCE}}", _STUCK_GUIDANCE.get(stuck_type, _STUCK_GUIDANCE["unknown"]))
            .replace("{{PERSIST_NOTE}}", _PERSIST_NOTE if persist else "")
            .replace("{{PROFILE_SECTION}}", profile_section)
        )
        system += _MATH_FORMAT
        if hint_level < _MAX_HINT_LEVEL:
            # F4-A1 结构性防线 (见模块 docstring): 状态机决定的禁令, 代码强制注入, 不依赖模型自觉;
            # 追加在画像段之后 —— 画像只调讲法, 禁答令永远压轴
            system += _ANSWER_BAN
        chat_messages = [ChatMessage(role="system", content=system)]
        for m in state.get("messages") or []:
            role, content = m.get("role"), m.get("content", "")
            if role in ("user", "assistant") and content:
                chat_messages.append(ChatMessage(role=role, content=content))
        result = await deps.gateway.chat(
            ChatRequest(
                messages=tuple(chat_messages),
                tier=Tier.standard,
                temperature=0.6,
                feature="tutor_respond",
                tenant_id=state.get("tenant_id", ""),
                user_id=state.get("user_id", ""),
                request_id=state.get("request_id", ""),
            )
        )
        return {
            "reply": result.text,
            "messages": [{"role": "assistant", "content": result.text}],
            "trace": [
                {
                    "node": "respond",
                    "event": "replied",
                    "hint_level": hint_level,
                    "persist_gently": persist,
                    # 画像可见性 (脱敏: 只有布尔与 KC 名, 无掌握度数值)
                    "profile_injected": bool(profile_section),
                    "weak_opening_kc": weak_opening_kc,
                }
            ],
        }

    async def summarize(state: TutorGraphState) -> dict:
        prompt = (
            _load_prompt("tutor_summarize.md")
            .replace("{{PROBLEM}}", state.get("problem", ""))
            .replace("{{HISTORY}}", _format_history(state.get("messages") or []))
        )
        data = await _chat_json(
            deps, state, feature="tutor_summarize", prompt=prompt, tier=Tier.cheap, temperature=0.2
        )
        degraded = data is None

        def _str_list(raw: object) -> list[str]:
            if not isinstance(raw, list):
                return []
            return [item.strip() for item in raw if isinstance(item, str) and item.strip()]

        summary = {
            "misconceptions": _str_list(data.get("misconceptions")) if data else [],
            "review_suggestions": _str_list(data.get("review_suggestions")) if data else [],
        }
        event = "llm_parse_failed_fallback" if degraded else "summarized"
        return {
            "session_summary": json.dumps(summary, ensure_ascii=False),
            "trace": [{"node": "summarize", "event": event}],
        }

    graph = StateGraph(TutorGraphState)
    graph.add_node("classify", classify)
    graph.add_node("policy", policy)
    graph.add_node("respond", respond)
    graph.add_node("summarize", summarize)
    graph.add_edge(START, "classify")
    graph.add_edge("classify", "policy")
    graph.add_conditional_edges(
        "policy", route_after_policy, {"respond": "respond", "summarize": "summarize"}
    )
    graph.add_edge("respond", END)
    graph.add_edge("summarize", END)
    return graph
