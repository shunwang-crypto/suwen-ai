"""quiz 子图 (F5): blueprint → interrupt 人工确认 → 按蓝图生成 → schema 校验重试环。

- 蓝图 cheap 档、题目 standard 档, 全部 json_mode 经 ModelGateway;
- 蓝图数量/难度分布与需求不符时程序性修正 (截断/补齐), 保证 items 数 == num_questions;
- 确认经 langgraph interrupt(), resume 值 {"approved": bool, "blueprint": 可选修改后蓝图};
- 逐题 Pydantic 严格校验 (QUESTION_SCHEMA), 失败带错误清单回 generate 修复, 最多重试 2 轮;
- 判分不进图: 客观题纯函数 grade_objective 由 API 层调用, 简答题走 LLM 评分 (§5.6)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ..gateway.base import ChatMessage, ChatRequest, Tier
from .state import GraphDeps, QuizState

# 本文件位于 packages/core/src/eduagents_core/graphs/ -> parents[5] 为仓库根
_PROMPTS_DIR = Path(__file__).resolve().parents[5] / "prompts"
MAX_RETRIES = 2  # 校验失败最多打回 generate 修复 2 轮

_TYPES = ("single", "multi", "blank", "short")
_DIFFICULTIES = ("easy", "mid", "hard")
_LETTERS = ("A", "B", "C", "D")


class QuestionModel(BaseModel):
    """单题严格 schema (§F5): 违反任何一条即校验失败进入修复重试。"""

    type: Literal["single", "multi", "blank", "short"]
    stem: str
    options: list[str] = Field(default_factory=list)
    answer: str
    explanation: str
    kc_names: list[str]
    difficulty: Literal["easy", "mid", "hard"]
    source_hint: int | None = None
    source_chunk_id: str | None = None

    @field_validator("stem", "answer", "explanation")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空")
        return value

    @field_validator("kc_names")
    @classmethod
    def _kc_names_1_to_3(cls, value: list[str]) -> list[str]:
        if not 1 <= len(value) <= 3:
            raise ValueError("必须为 1-3 个知识点名称")
        if any(not isinstance(name, str) or not name.strip() for name in value):
            raise ValueError("知识点名称不能为空")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> QuestionModel:
        if self.type in ("single", "multi"):
            if len(self.options) != 4:
                raise ValueError(f"{self.type} 题 options 必须恰好 4 项")
            if any(not opt.strip() for opt in self.options):
                raise ValueError("options 不能含空白项")
        elif self.options:
            raise ValueError(f"{self.type} 题 options 必须为空数组")
        answer = self.answer.strip().upper()
        if self.type == "single" and answer not in _LETTERS:
            raise ValueError('single 题 answer 必须是 "A"-"D" 之一')
        if self.type == "multi":
            letters = [t.strip() for t in answer.split(",") if t.strip()]
            if (
                not letters
                or len(set(letters)) != len(letters)
                or any(letter not in _LETTERS for letter in letters)
            ):
                raise ValueError('multi 题 answer 必须是 "A,C" 式不重复字母组合')
        return self


QUESTION_SCHEMA: dict[str, Any] = QuestionModel.model_json_schema()


def grade_objective(question: dict, response: str) -> bool:
    """客观题纯函数判分 (API 层调用, 不进图)。

    single: 忽略大小写与首尾空白; multi: 字母集合相等;
    blank: 去首尾空白后精确匹配, answer 支持 "|" 分隔多个可接受答案。
    short 题需 LLM 评分, 抛 ValueError。
    """
    qtype = question.get("type")
    answer = str(question.get("answer", ""))
    if qtype == "single":
        return response.strip().upper() == answer.strip().upper()
    if qtype == "multi":

        def to_letter_set(text: str) -> set[str]:
            return {t.strip().upper() for t in text.split(",") if t.strip()}

        expected = to_letter_set(answer)
        return bool(expected) and to_letter_set(response) == expected
    if qtype == "blank":
        acceptable = {alt.strip() for alt in answer.split("|") if alt.strip()}
        return response.strip() in acceptable
    raise ValueError(f"非客观题无法程序判分: {qtype!r}")


# --- 内部工具 ---


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """尽力解析单个 JSON 对象: 先整体, 再截取首 { 到末 } (容忍代码块围栏); 失败返回 None。"""
    candidates = [text.strip()]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _mix_deficit(items: list[dict], num_questions: int, difficulty_mix: dict) -> list[str]:
    """依据难度分布计算补齐时应追加的难度序列 (easy→mid→hard, 各补到目标数)。"""
    deficit: list[str] = []
    counts = {d: sum(1 for it in items if it["difficulty"] == d) for d in _DIFFICULTIES}
    for difficulty in _DIFFICULTIES:
        target = difficulty_mix.get(difficulty, 0)
        if isinstance(target, int) and target > counts[difficulty]:
            deficit.extend([difficulty] * (target - counts[difficulty]))
    return deficit


def _fix_blueprint(
    raw: dict,
    num_questions: int,
    difficulty_mix: dict,
    kc_names: list[str],
    topic: str,
) -> tuple[dict, bool]:
    """清洗 LLM 蓝图并程序性修正数量: 超出截断、不足按 mix 缺口补齐。

    返回 (blueprint, corrected)。保证 len(items) == num_questions。
    """
    fallback_kc = (kc_names[0] if kc_names else topic).strip() or "综合"
    items: list[dict] = []
    raw_items = raw.get("items")
    if isinstance(raw_items, list):
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            kc = entry.get("kc_name")
            kc = kc.strip() if isinstance(kc, str) and kc.strip() else fallback_kc
            qtype = entry.get("type") if entry.get("type") in _TYPES else "single"
            difficulty = (
                entry.get("difficulty") if entry.get("difficulty") in _DIFFICULTIES else "mid"
            )
            items.append({"kc_name": kc, "type": qtype, "difficulty": difficulty})
    corrected = len(items) != num_questions
    if len(items) > num_questions:
        items = items[:num_questions]
    if len(items) < num_questions:
        deficit = _mix_deficit(items, num_questions, difficulty_mix)
        pool = [n for n in kc_names if n.strip()] or [fallback_kc]
        index = 0
        while len(items) < num_questions:
            items.append(
                {
                    "kc_name": pool[index % len(pool)],
                    "type": "single",
                    "difficulty": deficit.pop(0) if deficit else "mid",
                }
            )
            index += 1
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        kcs = "、".join(dict.fromkeys(it["kc_name"] for it in items))
        summary = f"共 {num_questions} 题, 覆盖: {kcs}"
    return {"items": items, "summary": summary}, corrected


def build_quiz_graph(deps: GraphDeps) -> StateGraph:
    """构建 quiz 子图 (未编译)。deps 闭包捕获, 不进 state (state 须可进 checkpointer)。"""

    async def make_blueprint(state: QuizState) -> dict:
        topic = state.get("topic", "")
        num_questions = int(state.get("num_questions") or 5)
        difficulty_mix = state.get("difficulty_mix") or {}
        kc_names = state.get("kc_names") or []
        kc_block = "\n".join(f"- {name}" for name in kc_names) or "(无)"
        prompt = (
            _load_prompt("quiz_blueprint.md")
            .replace("{{TOPIC}}", topic)
            .replace("{{NUM_QUESTIONS}}", str(num_questions))
            .replace("{{DIFFICULTY_MIX}}", json.dumps(difficulty_mix, ensure_ascii=False))
            .replace("{{KC_NAMES}}", kc_block)
        )
        result = await deps.gateway.chat(
            ChatRequest(
                messages=(ChatMessage(role="user", content=prompt),),
                tier=Tier.cheap,
                temperature=0.3,
                json_mode=True,
                feature="quiz_blueprint",
                tenant_id=state.get("tenant_id", ""),
                user_id=state.get("user_id", ""),
                request_id=state.get("request_id", ""),
            )
        )
        raw = _parse_json_object(result.text) or {}
        blueprint, corrected = _fix_blueprint(raw, num_questions, difficulty_mix, kc_names, topic)
        return {
            "blueprint": blueprint,
            "num_questions": num_questions,
            "trace": [
                {
                    "node": "make_blueprint",
                    "event": "blueprint_ready",
                    "items": len(blueprint["items"]),
                    "parsed": bool(raw),
                    "corrected": corrected,
                }
            ],
        }

    def confirm_blueprint(state: QuizState) -> dict:
        decision = interrupt(
            {"kind": "blueprint_confirm", "blueprint": state.get("blueprint", {})}
        )
        approved = isinstance(decision, dict) and bool(decision.get("approved"))
        if not approved:
            return {
                "blueprint_confirmed": False,
                "trace": [{"node": "confirm_blueprint", "event": "rejected"}],
            }
        update: dict[str, Any] = {"blueprint_confirmed": True}
        modified = decision.get("blueprint")
        edited = (
            isinstance(modified, dict)
            and isinstance(modified.get("items"), list)
            and bool(modified["items"])
        )
        if edited:
            # 用户改过蓝图: 清洗后以其条数为准 (num_questions 跟随, 供整卷数量校验)
            blueprint, _ = _fix_blueprint(
                modified,
                len(modified["items"]),
                state.get("difficulty_mix") or {},
                state.get("kc_names") or [],
                state.get("topic", ""),
            )
            update["blueprint"] = blueprint
            update["num_questions"] = len(blueprint["items"])
        update["trace"] = [
            {"node": "confirm_blueprint", "event": "approved", "edited": edited}
        ]
        return update

    def route_after_confirm(state: QuizState) -> str:
        return "generate" if state.get("blueprint_confirmed") else END

    async def generate(state: QuizState) -> dict:
        blueprint = state.get("blueprint") or {}
        kb_id = state.get("kb_id", "")
        chunk_ids: list[str] = []
        materials_block = "(无素材)"
        if kb_id and deps.retrieval is not None:
            retrieval = await deps.retrieval.retrieve(
                tenant_id=state.get("tenant_id", ""),
                kb_id=kb_id,
                query=state.get("topic", ""),
            )
            chunks = list(retrieval.chunks)[:5]
            if chunks:
                chunk_ids = [chunk.chunk_id for chunk in chunks]
                materials_block = "\n\n".join(
                    f"[素材 {i}] ({chunk.heading_path})\n{chunk.text}"
                    for i, chunk in enumerate(chunks, start=1)
                )
        errors = state.get("validation_errors") or []
        fix_block = ""
        if errors:
            error_lines = "\n".join(f"- {e}" for e in errors)
            fix_block = (
                "## 上一轮输出未通过校验, 本轮必须修复\n\n"
                f"重新输出全部题目, 并修复以下问题:\n{error_lines}"
            )
        prompt = (
            _load_prompt("quiz_generate.md")
            .replace("{{TOPIC}}", state.get("topic", ""))
            .replace("{{BLUEPRINT_JSON}}", json.dumps(blueprint, ensure_ascii=False))
            .replace("{{MATERIALS}}", materials_block)
            .replace("{{FIX_INSTRUCTIONS}}", fix_block)
        )
        result = await deps.gateway.chat(
            ChatRequest(
                messages=(ChatMessage(role="user", content=prompt),),
                tier=Tier.standard,
                temperature=0.7,
                json_mode=True,
                feature="quiz_generate",
                tenant_id=state.get("tenant_id", ""),
                user_id=state.get("user_id", ""),
                request_id=state.get("request_id", ""),
                # P1-15: 输出上限按题数动态 (单题含解析约 300-400 token)
                max_tokens=400 * int(state.get("num_questions", 5)) + 300,
            )
        )
        raw = _parse_json_object(result.text)
        raw_questions = raw.get("questions") if raw else None
        questions: list[dict] = []
        if isinstance(raw_questions, list):
            for item in raw_questions:
                if not isinstance(item, dict):
                    continue
                hint = item.get("source_hint")
                if isinstance(hint, int) and 1 <= hint <= len(chunk_ids):
                    item["source_chunk_id"] = chunk_ids[hint - 1]
                else:
                    item.setdefault("source_chunk_id", None)
                questions.append(item)
        return {
            "questions": questions,
            "trace": [
                {
                    "node": "generate",
                    "event": "generated",
                    "count": len(questions),
                    "materials": len(chunk_ids),
                    "round": int(state.get("retry_count") or 0),
                }
            ],
        }

    def validate(state: QuizState) -> dict:
        raw_questions = state.get("questions") or []
        expected = int(state.get("num_questions") or 0)
        errors: list[str] = []
        validated: list[dict] = []
        if len(raw_questions) != expected:
            errors.append(f"整卷数量不符: 需要 {expected} 题, 实得 {len(raw_questions)} 题")
        for i, question in enumerate(raw_questions, start=1):
            try:
                validated.append(QuestionModel.model_validate(question).model_dump())
            except ValidationError as exc:
                for err in exc.errors():
                    loc = ".".join(str(part) for part in err["loc"]) or "整题"
                    errors.append(f"第 {i} 题 {loc}: {err['msg']}")
        retry_count = int(state.get("retry_count") or 0)
        if errors:
            return {
                "validation_errors": errors,
                "retry_count": retry_count + 1,
                "trace": [
                    {
                        "node": "validate",
                        "event": "failed",
                        "round": retry_count,
                        "error_count": len(errors),
                        "will_retry": retry_count < MAX_RETRIES,
                    }
                ],
            }
        return {
            "questions": validated,
            "validation_errors": [],
            "trace": [
                {
                    "node": "validate",
                    "event": "passed",
                    "round": retry_count,
                    "error_count": 0,
                    "count": len(validated),
                }
            ],
        }

    def route_after_validate(state: QuizState) -> str:
        if state.get("validation_errors") and int(state.get("retry_count") or 0) <= MAX_RETRIES:
            return "generate"
        return END

    graph = StateGraph(QuizState)
    graph.add_node("make_blueprint", make_blueprint)
    graph.add_node("confirm_blueprint", confirm_blueprint)
    graph.add_node("generate", generate)
    graph.add_node("validate", validate)
    graph.add_edge(START, "make_blueprint")
    graph.add_edge("make_blueprint", "confirm_blueprint")
    graph.add_conditional_edges(
        "confirm_blueprint", route_after_confirm, {"generate": "generate", END: END}
    )
    graph.add_edge("generate", "validate")
    graph.add_conditional_edges(
        "validate", route_after_validate, {"generate": "generate", END: END}
    )
    return graph
