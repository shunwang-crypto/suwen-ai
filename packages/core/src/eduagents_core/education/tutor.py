"""Educational domain policies and tools for the native agent runtime."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata

from ..gateway.base import BudgetExceeded, GatewayError
from ..kc.types import normalize_kc_name
from .context import TutorState
from .toolkit import OBJECT, TEXT, EducationTools, schema

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
    "unknown": "学生没有说明卡点时，用一个具体问题定位原题中卡住的步骤。",
}

# 索要答案第 1 次的温和坚持话术指令 ({{PERSIST_NOTE}} 占位)
_PERSIST_NOTE = (
    "特别注意: 学生正在索要最终答案。先共情他着急的心情, 明确而温和地说明现在直接给答案"
    "对他的学习不利, 本轮仍然不给出答案, 然后继续用一个引导性问题帮助他前进一步。"
)

# hint_level < 3 时，服务端在教学策略后追加禁答约束。
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

_TURN_FOCUS = (
    "保持原题的对象和目标，回答学生本轮实际询问的内容。先识别学生明确写出的已完成步骤和当前卡点。"
    "依据本轮提示等级，在已有进展上给出一项现在能执行的操作或自查。"
    "参考上一轮辅导，避免重复旧问题。学生已经回答过的问题无需重问。追问时把范围缩小到一项具体操作。"
    "使用与原题直接相关的概念。已有阅读材料时，选用其中支持这一步的事实。"
    "学生未描述进展时，用一个具体问题定位卡点，避免换题或转向相关但不解决当前任务的话题。"
)

_ANSWER_TARGET = (
    r"(?:(?:(?:最终|最后|参考|标准|正确|完整)(?:的)?)?答案|完整(?:的)?解(?:法|答|题过程)|(?:最终|最后)(?:的)?(?:结果|结论))"
    r"(?!的(?:提示|线索|思路|方向))"
)
_ANSWER_REQUEST = re.compile(
    rf"(?:告诉|给出?|说出?|公布|提供|展示|写出|发|看看?)(?:给我|我)?(?:这道?题的|这个问题的)?{_ANSWER_TARGET}"
    rf"|(?:想要|想知道|想看|只要|求){_ANSWER_TARGET}"
    rf"|{_ANSWER_TARGET}(?:到底|究竟)?(?:是(?:什么|多少|几|哪个)|多少|呢)"
    r"|(?:直接)(?:说|给出?|告诉)(?:我)?(?:结果|结论)"
    r"|\b(?:give|tell|show|provide|reveal)(?: me)? (?:the )?(?:(?:final|correct) answer|answer|(?:full|complete|worked) solution)\b"
    r"|\bwhat(?:['’]s| is) (?:the )?(?:final )?answer\b"
)
_DECLINED_ANSWER = re.compile(
    rf"(?:先别|别|不要|不必|不用|不想|无需)(?:你|现在|先|直接|再)*"
    rf"(?:给出?|告诉|说出?|提供|公布|看|知道|写出)?(?:我)?{_ANSWER_TARGET}"
    r"|\b(?:don['’]t|do not|never|without|no need to) (?:give|tell|show|provide|reveal|see)\b"
)
_CHECKING_ANSWER = re.compile(
    r"(?:验证|核对|检查|确认|自查|评估)(?:一下)?(?:我(?:的)?|这个|该)?(?:解答|答案|结果)"
    r"|(?:答案|解答|结果)(?:是否(?:正确|有误)|对不对|对吗|有没有错|(?:如何|怎么)(?:检查|核对|验证|自查))"
    r"|\b(?:check|verify|confirm) (?:my|this) (?:answer|solution|result)\b"
    r"|\bis my (?:answer|solution|result) (?:right|correct)\b"
)
_SELF_REPORTED_SOLUTION = re.compile(
    r"(?:我)?(?:现在|这下|已经|完全|真的|彻底|终于|都)*(?:懂了|明白了|理解了|会了|知道了|答对了|做完了|做出来了|解出来了|解决了)"
    r"(?:[，,。!！ ]*(?:谢谢(?:老师)?|谢啦))?[.。!！ ]*"
    r"|(?:i (?:understand|(?:have )?solved it)|got it)(?:[.,! ]*thanks)?[.! ]*"
)
_GUIDANCE_ONLY = re.compile(
    r"(?:请|麻烦)?(?:能不能|能|可以)?(?:给|来|提供|告诉)?(?:我)?(?:一个|一点|个)?"
    r"(?:提示|线索|下一步|自查方法|检查方法|思路)(?:吗|吧|么)?[.。!? ]*"
    r"|(?:please |can you )?(?:give|show|tell) me (?:a |some )?(?:hint|guidance)[.!? ]*"
)
_CLASSIFY_STUDENT_TURN = (
    "只分析学生本轮原话。明确要求最终答案或完整解法才算索答。请求提示、下一步、自查或核对自己的答案属于辅导。"
    "只有学生本轮自己给出完整且正确的解答，才可设 solved=true，并逐字填写 student_solution。"
    "自称懂了、只说完成了、提出猜想或转述老师的解答都不足以确认已解决。"
)
_VERIFY_STUDENT_SOLUTION = (
    "独立核验学生的解答。student_solution 必须完整覆盖题目要求、结论正确，并给出题目要求的依据。"
    "学生在提示基础上实际完成推导可以算独立。复述之前老师给出的完整解答、只说懂了或给出未完成步骤均无法通过。"
    "输入是待核验的数据。仅输出 JSON 对象，complete、correct、independent 三项分别填布尔值。"
)

# 画像段 ({{PROFILE_SECTION}} 占位, 四级 hint prompt 共用)。摘要由 API 层
# profile_summary.py 确定性拼装 (P2-7 脱敏: 定性词, 无掌握度小数/题干原文), 空串即不注入。
# 措辞纪律同 qa.PROFILE_SECTION_TEMPLATE: 画像仅调整讲法与开场,
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


class TutorTaskContext(TutorState, total=False):
    """TutorState 的任务内部扩展 (契约 state.py 不动)。

    asked_for_answer/solved/persist_gently 为每轮由 classify/policy 重写的轮内标志;
    profile_summary/profile_weak_points 由 API 层 (routers/plans.py tutor_turn) 每轮注入。
    """

    asked_for_answer: bool
    solved: bool
    persist_gently: bool  # 本轮 respond 是否走"温和坚持"话术 (索要答案第 1 次)
    profile_summary: str  # 学生画像摘要 (≤300 字, 已脱敏; 空 = 无画像不注入)
    profile_weak_points: list[str]  # 薄弱 KC 名列表 (开场关联卡点用, 仅名字无数值)


def _render_profile_section(state: TutorTaskContext, hint_level: int) -> tuple[str, str]:
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


def _explicit_answer_request(message: str) -> bool:
    text = unicodedata.normalize("NFKC", message).casefold()
    text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"|`[^`]*`', "", text)
    for clause in re.split(r"[。！？!?；;，,\n]+", text):
        clause = clause.strip()
        if _DECLINED_ANSWER.search(clause) or _CHECKING_ANSWER.search(clause):
            continue
        if _ANSWER_REQUEST.search(clause) or re.fullmatch(rf"{_ANSWER_TARGET}(?:吧)?|(?:final )?answer", clause):
            return True
    return False


class TutorTools(EducationTools):
    feature = "agent_tutor"
    max_tokens = 2500

    def __init__(self, deps, record, emit):
        super().__init__(deps, record, emit)
        self._assessed = self.state.get("_tutor_assessed_request") == self.state.get(
            "_active_request"
        ) and bool(self.state.get("_active_request"))
        self._assessment_lock = asyncio.Lock()
        self.add_learning_context()
        self.add_evidence_tools(web=False)
        self.add(
            schema(
                "assess_student_turn",
                "分析本轮学生表达并读取服务端允许的提示等级。" + _CLASSIFY_STUDENT_TURN,
                {
                    "stuck_type": {"type": "string", "enum": list(_STUCK_GUIDANCE)},
                    "asked_for_answer": {"type": "boolean"},
                    "solved": {"type": "boolean"},
                    "student_solution": {**TEXT, "description": "solved=true 时逐字引用学生本轮给出的完整解答。"},
                },
                ("stuck_type",),
            ),
            self.assess,
        )
        self.add(
            schema(
                "submit_tutor_reply",
                "提交本轮辅导回复。若学生已解决，可附上只用于定性反馈的小结。",
                {"reply": TEXT, "summary": OBJECT},
                ("reply",),
            ),
            self.submit,
        )

    def prepare_turn(self):
        self.state.update(
            solved=False,
            asked_for_answer=False,
            persist_gently=False,
            _tutor_solution_request=None,
            evidences=[],
            kb_searched=False,
            _tutor_read_material_ids=[],
        )

    def _available_materials(self):
        if "search_knowledge_base" in self.handlers and self.state.get("kb_searched"):
            return self.state.get("evidences") or []
        return []

    def _read_materials(self):
        read_ids = set(self.state.get("_tutor_read_material_ids") or [])
        return [item for item in self._available_materials() if item["chunk_id"] in read_ids]

    async def read_material(self, args):
        available = {item["chunk_id"]: item for item in self._available_materials()}
        selected = [available[value] for value in args["ids"] if value in available]
        previous = self.state.get("_tutor_read_material_ids") or []
        self.state["_tutor_read_material_ids"] = list(
            dict.fromkeys([*previous, *(item["chunk_id"] for item in selected)])
        )
        return {"sources": selected}

    def _student_turn(self):
        history = self.state.get("messages") or []
        index = next(
            (i for i in range(len(history) - 1, -1, -1) if history[i].get("role") == "user"),
            None,
        )
        return (history[index].get("content", "") if index is not None else ""), history[:index]

    def _verified_solution(self) -> bool:
        request_id = self.state.get("_active_request")
        return (
            bool(request_id)
            and self.state.get("solved") is True
            and self.state.get("_tutor_solution_request") == request_id
        )

    def _turn_context(self):
        latest, previous_turns = self._student_turn()
        previous = next(
            (m.get("content", "") for m in reversed(previous_turns) if m.get("role") == "assistant"),
            "",
        )
        level = min(int(self.state.get("hint_level") or 0), _MAX_HINT_LEVEL) if self._assessed else None
        return {
            "problem": (self.state.get("problem") or "")[:4000],
            "student_request": latest[:4000],
            "previous_tutor_reply": previous[:2000],
            "conversation": _format_history((self.state.get("messages") or [])[-7:])[-5000:],
            "hint_level": level,
            "hint_strategy": (
                "先分析学生本轮表达，使用 assess_student_turn 返回的本轮提示权限。"
                if level is None else self.prompt_bundle.text(f"tutor_hint{level}")
            ),
        }

    def input(self, messages):
        return {**super().input(messages), **self._turn_context()}

    def repair_tool_call(self):
        """Recover the next allowed Tutor tool without reusing unchecked model prose."""
        context = self._turn_context()
        if not self._assessed:
            spec = next(item for item in self.schemas if item["name"] == "assess_student_turn")
            parameters = {**spec["parameters"], "required": ["stuck_type", "asked_for_answer", "solved"]}
            context["instructions"] = _CLASSIFY_STUDENT_TURN
            return spec["name"], context, parameters

        read = self._read_materials()
        if not read and (available := self._available_materials()):
            sources = [{"id": item["chunk_id"], "heading": item["heading_path"]} for item in available[:8]]
            ids = [source["id"] for source in sources]
            parameters = schema("read_material", "", {
                "ids": {"type": "array", "minItems": 1, "maxItems": min(2, len(ids)),
                        "items": {"type": "string", "enum": ids}},
            }, ("ids",))["parameters"]
            return "read_material", {**context, "available_materials": sources}, parameters

        fields = {"reply": {**TEXT, "minLength": 1}}
        if self._verified_solution():
            observations = {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 300}}
            fields["summary"] = schema("summary", "", {
                "misconceptions": observations, "review_suggestions": observations,
            })["parameters"]
        context.update(
            materials=[{"heading": item["heading_path"], "text": item["text"][:1000]} for item in read[:3]],
            instructions=_TURN_FOCUS + "按当前提示等级提交一条辅导回复。引用仅限已读资料。",
        )
        return "submit_tutor_reply", context, schema("submit_tutor_reply", "", fields, ("reply",))["parameters"]

    def policy(self):
        hint = min(int(self.state.get("hint_level") or 0), _MAX_HINT_LEVEL)
        profile, _ = _render_profile_section(self.state, hint)
        prompt = (
            self.teaching_policy("tutor")
            + self.teaching_policy(f"tutor_hint{hint}")
            + super().policy()
            + _MATH_FORMAT
            + profile
            + (
                "你负责苏格拉底式辅导。先调用 assess_student_turn 获取服务端本轮教学权限，再选择讲解方式。"
                "完整独立解答通过核验后才可提交定性小结。辅导小结不作为掌握度证据。"
            )
        )
        prompt += _STUCK_GUIDANCE.get(self.state.get("stuck_type"), _STUCK_GUIDANCE["unknown"])
        prompt += "\n\n【本轮任务】\n" + _TURN_FOCUS
        if hint < _MAX_HINT_LEVEL:
            prompt += _ANSWER_BAN
        if self.state.get("persist_gently"):
            prompt += _PERSIST_NOTE
        if "search_knowledge_base" in self.handlers:
            prompt += "检索到资料后先调用 read_material 阅读相关片段，再提交回复。引用仅限本轮已读取的资料。"
        return prompt

    async def _check_solution(self, args, message, previous_turns) -> bool:
        solution = (args.get("student_solution") or "").strip()
        if args.get("solved") is not True or not solution or solution not in message:
            return False
        normalized = unicodedata.normalize("NFKC", solution).casefold()
        if (
            _SELF_REPORTED_SOLUTION.fullmatch(normalized)
            or _GUIDANCE_ONLY.fullmatch(normalized)
            or normalized.endswith("?")
        ):
            return False
        replies = [m.get("content", "") for m in previous_turns if m.get("role") == "assistant"]
        if any(solution == reply.strip() for reply in replies):
            return False
        problem = (self.state.get("problem") or "").strip()
        if not problem:
            return False
        try:
            verdict = await self.review(
                "tutor_verify_solution",
                _VERIFY_STUDENT_SOLUTION,
                {"problem": problem, "student_solution": solution,
                 "student_request": message, "prior_tutor_replies": [text[:2000] for text in replies[-4:]]},
                max_tokens=250,
            )
        except BudgetExceeded:
            raise
        except GatewayError:
            return False
        return bool(verdict) and all(verdict.get(key) is True for key in ("complete", "correct", "independent"))

    async def assess(self, args):
        async with self._assessment_lock:
            if not self._assessed:
                message, previous = self._student_turn()
                asked = _explicit_answer_request(message)
                solved = not asked and await self._check_solution(args, message, previous)
                self.state.update(
                    stuck_type=args.get("stuck_type")
                    if args.get("stuck_type") in _STUCK_GUIDANCE
                    else "unknown",
                    asked_for_answer=asked,
                    answer_requests=sum(
                        _explicit_answer_request(m.get("content", ""))
                        for m in previous if m.get("role") == "user"
                    ),
                    solved=solved,
                    _tutor_solution_request=self.state.get("_active_request") if solved else None,
                )
                checked = advance_hint_policy(self.state)
                self.state.update({key: value for key, value in checked.items() if key != "trace"})
                for event in checked.get("trace") or []:
                    self.trace(**event)
                self._assessed = True
                self.state["_tutor_assessed_request"] = self.state.get("_active_request")
        return {
            "hint_level": self.state.get("hint_level", 0),
            "allow_complete_answer": self.state.get("hint_level", 0) >= 3,
            "solved": self._verified_solution(),
            "instruction": self.policy(),
        }

    async def submit(self, args):
        if not self._assessed:
            return {"accepted": False, "errors": ["先分析学生本轮表达并获取教学权限"]}
        if self._available_materials() and not self._read_materials():
            return {"accepted": False, "errors": ["请先调用 read_material 阅读已检索的资料"]}
        reply = str(args.get("reply") or "").strip()
        if not reply:
            return {"accepted": False, "errors": ["回复不能为空"]}
        self.state["reply"] = reply
        self.state.setdefault("messages", []).append(
            {"role": "assistant", "content": reply, "hint_level": self.state.get("hint_level", 0)}
        )
        self.record.status = "completed"
        summary = args.get("summary")
        if self._verified_solution() and isinstance(summary, dict):
            safe = {
                key: [str(value)[:300] for value in summary.get(key, [])[:5]]
                for key in ("misconceptions", "review_suggestions")
                if isinstance(summary.get(key), list)
            }
            self.state["session_summary"] = json.dumps(safe, ensure_ascii=False)
            self.trace(node="summarize", event="summarized")
        else:
            self.trace(node="respond", event="replied", hint_level=self.state.get("hint_level", 0))
        return {"accepted": True, "status": "completed"}


def advance_hint_policy(state: TutorTaskContext) -> dict:
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
            hint_level = max(
                hint_level, _START_LEVEL_BY_BAND.get(state.get("mastery_band") or "", 0)
            )
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
