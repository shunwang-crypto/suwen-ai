"""quiz 子图 (F5): blueprint → interrupt 人工确认 → 按蓝图生成 → schema 校验重试环。

- 蓝图 cheap 档、题目 standard 档, 全部 json_mode 经 ModelGateway;
- 蓝图数量/难度分布与需求不符时程序性修正 (截断/补齐), 保证 items 数 == num_questions;
- 确认经 langgraph interrupt(), resume 值 {"approved": bool, "blueprint": 可选修改后蓝图};
  interrupt 负载附带 difficulty_reasons (定向出题的逐 KC 分档理由, P1-1 可解释);
- generate 注入学生画像段 (P1-2): 干扰项贴合近期错因分布, 摘要为空即不注入;
- 逐题 Pydantic 严格校验 (QUESTION_SCHEMA), 失败带错误清单回 generate 修复, 最多重试 2 轮;
- schema 通过后由 cheap 档独立 critic 检查蓝图一致性与教学质量, major 最多返工 1 轮;
- 判分不进图: 客观题纯函数 grade_objective 由 API 层调用, 简答题走 LLM 评分 (§5.6)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ..gateway.base import BudgetExceeded, ChatMessage, ChatRequest, Tier
from ..kc.types import normalize_kc_name
from ..logging import get_logger
from .state import GraphDeps, QuizState

# 本文件位于 packages/core/src/eduagents_core/graphs/ -> parents[5] 为仓库根
_PROMPTS_DIR = Path(__file__).resolve().parents[5] / "prompts"
MAX_RETRIES = 2  # 校验失败最多打回 generate 修复 2 轮
MAX_CRITIC_RETRIES = 1  # 独立评审最多返工 1 轮，避免无限增加成本
MAX_CRITIC_ISSUES = 5
MAX_CRITIC_REASON_CHARS = 120
MAX_CRITIC_MATERIAL_CHARS = 20_000

_TYPES = ("single", "multi", "blank", "short")
_DIFFICULTIES = ("easy", "mid", "hard")
_LETTERS = ("A", "B", "C", "D")

# 画像注入 (P1-2, 同 qa_graph 的注入法): 摘要由 API 层 profile_summary.py 确定性
# 拼装并已按 P2-7 脱敏 (定性词, 无题干原文/PII), 空串即完全不改 prompt。
PROFILE_SECTION_TEMPLATE = (
    "## 学生画像（来自该学生的历史作答与掌握度记录，仅用于调整出题）\n\n"
    "{{PROFILE}}\n\n"
    "【个性化指令】干扰项优先贴合该学生近期的错因分布：错因偏向概念混淆时，"
    "干扰项优先设计成其易混概念；偏向公式用错时优先易记错的公式变形；偏向计算失误时"
    "优先典型计算陷阱。薄弱知识点的题目解析写得更细（从前置概念讲起）。"
    "画像仅用于调整出题与解析，不得写进题干、选项或答案文本，不得罗列画像原文。"
    "画像含兴趣标签时，例题的应用情境可自然贴合其兴趣（用其熟悉的场景包装题目背景），"
    "不得生硬堆砌兴趣词，不得改变蓝图规定的知识点与考察目标。"
)
log = get_logger("quiz_graph")


class _QuizGraphState(QuizState, total=False):
    """构图私有扩展通道 (同 qa 的 _QaGraphState): 不进 QuizState 公共契约。"""

    profile_summary: str  # 学生画像摘要 (API 层 profile_summary.py 产出; 空=无画像)
    difficulty_reasons: list[dict]  # 逐薄弱 KC 难度分档理由 (随 blueprint interrupt 下发)
    critic_issues: list[str]  # 有界修复指令；不保存素材或 critic 原始响应
    critic_retry_count: int  # 独立评审已触发的返工轮数


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


def _enforce_difficulty_mix(
    items: list[dict], difficulty_mix: dict, difficulty_by_kc: dict[str, str]
) -> int:
    """难度分布程序性强制 (ZPD 自适应不是对 LLM 的建议, 是约束)。

    两步, 返回被改写的题数:
    1. 逐 KC 钉住: 题目的 KC 命中定向分档表 (difficulty_by_kc, 归一化名) 时,
       难度以掌握度匹配档为准 —— 前端"为什么这套题"展示的分档必须与实际出题一致;
    2. 配比强制: mix 各档目标数之和恰等于题数时, 把盈余档的未钉住题稳定改标到缺口档。
       钉住题不参与改标 (定向路径的 mix 本就由同一分档表轮转推得, 两者天然一致)。
    """
    changed = 0
    for item in items:
        pinned = difficulty_by_kc.get(normalize_kc_name(item["kc_name"]))
        if pinned in _DIFFICULTIES and item["difficulty"] != pinned:
            item["difficulty"] = pinned
            item["_pinned"] = True
            changed += 1
        elif pinned in _DIFFICULTIES:
            item["_pinned"] = True
    targets = {d: difficulty_mix.get(d) for d in _DIFFICULTIES}
    if all(isinstance(t, int) and t >= 0 for t in targets.values()) and sum(
        targets.values()  # type: ignore[arg-type]
    ) == len(items):
        counts = {d: sum(1 for it in items if it["difficulty"] == d) for d in _DIFFICULTIES}
        deficit = [d for d in _DIFFICULTIES for _ in range(targets[d] - counts[d]) if targets[d] > counts[d]]  # type: ignore[operator]
        for item in items:
            if not deficit:
                break
            if item.get("_pinned") or counts[item["difficulty"]] <= targets[item["difficulty"]]:  # type: ignore[operator]
                continue
            counts[item["difficulty"]] -= 1
            item["difficulty"] = deficit.pop(0)
            counts[item["difficulty"]] += 1
            changed += 1
    for item in items:
        item.pop("_pinned", None)
    return changed


def _fix_blueprint(
    raw: dict,
    num_questions: int,
    difficulty_mix: dict,
    kc_names: list[str],
    topic: str,
    difficulty_by_kc: dict[str, str] | None = None,
    enforce_mix: bool = True,
) -> tuple[dict, bool, int]:
    """清洗 LLM 蓝图并程序性修正数量: 超出截断、不足按 mix 缺口补齐。

    返回 (blueprint, corrected, difficulty_enforced_count)。保证 len(items) == num_questions。
    enforce_mix=False 用于用户改过的蓝图 (学习者控制权: 用户显式改的难度不被系统改回)。
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
    enforced = 0
    if enforce_mix:
        enforced = _enforce_difficulty_mix(items, difficulty_mix, difficulty_by_kc or {})
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        kcs = "、".join(dict.fromkeys(it["kc_name"] for it in items))
        summary = f"共 {num_questions} 题, 覆盖: {kcs}"
    return {"items": items, "summary": summary}, corrected, enforced


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
        # 定向出题的逐 KC 分档表: 蓝图 LLM 给错难度时程序性钉回 (mix 不是建议, 是约束)
        difficulty_by_kc = {
            normalize_kc_name(str(r.get("kc_name", ""))): r.get("difficulty")
            for r in (state.get("difficulty_reasons") or [])
            if isinstance(r, dict) and r.get("difficulty") in _DIFFICULTIES
        }
        blueprint, corrected, enforced = _fix_blueprint(
            raw, num_questions, difficulty_mix, kc_names, topic, difficulty_by_kc
        )
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
                    "difficulty_enforced": enforced,
                }
            ],
        }

    def confirm_blueprint(state: _QuizGraphState) -> dict:
        # difficulty_reasons: 定向出题时路由层产出的逐 KC 分档理由 (P1-1 可解释),
        # 随 interrupt 平铺进 SSE, 前端蓝图确认卡渲染"为什么这套题"
        decision = interrupt(
            {
                "kind": "blueprint_confirm",
                "blueprint": state.get("blueprint", {}),
                "difficulty_reasons": state.get("difficulty_reasons") or [],
            }
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
            # 用户改过蓝图: 清洗后以其条数为准 (num_questions 跟随, 供整卷数量校验)。
            # enforce_mix=False: 学习者控制权 —— 用户显式改的难度不被系统改回
            blueprint, _, _ = _fix_blueprint(
                modified,
                len(modified["items"]),
                state.get("difficulty_mix") or {},
                state.get("kc_names") or [],
                state.get("topic", ""),
                enforce_mix=False,
            )
            update["blueprint"] = blueprint
            update["num_questions"] = len(blueprint["items"])
        update["trace"] = [
            {"node": "confirm_blueprint", "event": "approved", "edited": edited}
        ]
        return update

    def route_after_confirm(state: QuizState) -> str:
        return "generate" if state.get("blueprint_confirmed") else END

    async def generate(state: _QuizGraphState) -> dict:
        blueprint = state.get("blueprint") or {}
        kb_id = state.get("kb_id", "")
        chunk_ids: list[str] = []
        materials_block = "(无素材)"
        if kb_id and deps.retrieval is not None:
            # RetrievalService.retrieve(tenant_id, kb_ids, query) — 位置参数口径同 plan_graph;
            # 曾因误用 kb_id= 关键字在真检索路径 TypeError (单测的 None/fake 检索盖不住,
            # 由 record 冒烟抓出), 回归测试用真实签名的 fake 锁死
            retrieval = await deps.retrieval.retrieve(
                state.get("tenant_id", ""), kb_id, state.get("topic", "")
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
        critic_issues = state.get("critic_issues") or []
        if critic_issues:
            critic_lines = "\n".join(f"- {issue}" for issue in critic_issues)
            critic_block = (
                "## 独立质量评审要求返工\n\n"
                f"重新输出全部题目, 并修复以下问题:\n{critic_lines}"
            )
            fix_block = f"{fix_block}\n\n{critic_block}" if fix_block else critic_block
        # 画像注入 (P1-2): 空摘要即整段置空, prompt 与无画像时完全一致
        profile_summary = (state.get("profile_summary") or "").strip()
        profile_block = (
            PROFILE_SECTION_TEMPLATE.replace("{{PROFILE}}", profile_summary)
            if profile_summary
            else ""
        )
        prompt = (
            _load_prompt("quiz_generate.md")
            .replace("{{TOPIC}}", state.get("topic", ""))
            .replace("{{BLUEPRINT_JSON}}", json.dumps(blueprint, ensure_ascii=False))
            .replace("{{MATERIALS}}", materials_block)
            .replace("{{PROFILE_SECTION}}", profile_block)
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
                    # 画像是否注入可观测 (前端时间线可核对, 同 qa 的 profile_injected)
                    "profile_injected": bool(profile_summary),
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

    def route_after_validate(state: _QuizGraphState) -> str:
        if state.get("validation_errors"):
            if int(state.get("retry_count") or 0) <= MAX_RETRIES:
                return "generate"
            return END
        return "critic_review"

    async def critic_review(state: _QuizGraphState) -> dict:
        """独立质量评审；增强项失败可降级，但预算熔断必须继续向上抛出。"""
        questions = state.get("questions") or []
        if not questions:
            return {
                "critic_issues": [],
                "trace": [
                    {"node": "critic_review", "event": "skipped", "reason": "no_questions"}
                ],
            }

        materials: list[dict[str, str | int | None]] = []
        retrieval_degraded = False
        kb_id = state.get("kb_id", "")
        if kb_id and deps.retrieval is not None:
            try:
                # 不把素材正文塞入 Graph state/checkpoint；独立评审按相同查询重新检索。
                # 检索层可复用现有缓存，失败则只跳过事实锚定维度，不阻断 schema 合格题目。
                retrieval = await deps.retrieval.retrieve(
                    state.get("tenant_id", ""), kb_id, state.get("topic", "")
                )
                remaining = MAX_CRITIC_MATERIAL_CHARS
                for index, chunk in enumerate(list(retrieval.chunks)[:5], start=1):
                    if remaining <= 0:
                        break
                    text = str(chunk.text)[:remaining]
                    remaining -= len(text)
                    materials.append(
                        {
                            "index": index,
                            "heading": str(chunk.heading_path or "")[:500],
                            "text": text,
                            "page": chunk.page_start,
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - critic fact check can run without retrieval
                retrieval_degraded = True
                log.warning(
                    "quiz_critic_retrieval_degraded",
                    error_type=type(exc).__name__,
                )

        critic_input = json.dumps(
            {
                "blueprint": state.get("blueprint") or {},
                "materials": materials,
                "questions": questions,
            },
            ensure_ascii=False,
        )
        try:
            result = await deps.gateway.chat(
                ChatRequest(
                    messages=(
                        ChatMessage(role="system", content=_load_prompt("quiz_verify.md")),
                        ChatMessage(role="user", content=critic_input),
                    ),
                    tier=Tier.cheap,
                    temperature=0.0,
                    json_mode=True,
                    feature="quiz_verify",
                    tenant_id=state.get("tenant_id", ""),
                    user_id=state.get("user_id", ""),
                    request_id=state.get("request_id", ""),
                    max_tokens=700,
                )
            )
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - optional critic must not block valid questions
            log.warning("quiz_critic_degraded", error_type=type(exc).__name__)
            return {
                "critic_issues": [],
                "trace": [
                    {
                        "node": "critic_review",
                        "event": "degraded",
                        "reason": "gateway_error",
                        "retrieval_degraded": retrieval_degraded,
                    }
                ],
            }

        data = _parse_json_object(result.text)
        if data is None or not isinstance(data.get("issues"), list):
            return {
                "critic_issues": [],
                "trace": [
                    {
                        "node": "critic_review",
                        "event": "degraded",
                        "reason": "invalid_response",
                        "retrieval_degraded": retrieval_degraded,
                    }
                ],
            }

        issues: list[dict[str, Any]] = []
        issue_counts: dict[int, int] = {}
        for item in data["issues"]:
            if len(issues) >= MAX_CRITIC_ISSUES:
                break
            if not isinstance(item, dict):
                continue
            question_idx = item.get("question_idx")
            severity = item.get("severity")
            reason = item.get("reason")
            if (
                not isinstance(question_idx, int)
                or isinstance(question_idx, bool)
                or not 1 <= question_idx <= len(questions)
                or severity not in ("major", "minor")
                or not isinstance(reason, str)
                or not reason.strip()
                or issue_counts.get(question_idx, 0) >= 2
            ):
                continue
            issue_counts[question_idx] = issue_counts.get(question_idx, 0) + 1
            issues.append(
                {
                    "question_idx": question_idx,
                    "severity": severity,
                    "reason": reason.strip()[:MAX_CRITIC_REASON_CHARS],
                }
            )

        major = [issue for issue in issues if issue["severity"] == "major"]
        retry_count = int(state.get("critic_retry_count") or 0)
        if major and retry_count < MAX_CRITIC_RETRIES:
            fix_lines = [
                f"第 {issue['question_idx']} 题 ({issue['severity']}): {issue['reason']}"
                for issue in issues
            ]
            return {
                "critic_issues": fix_lines,
                "critic_retry_count": retry_count + 1,
                "trace": [
                    {
                        "node": "critic_review",
                        "event": "failed",
                        "major": len(major),
                        "issues": len(issues),
                        "will_retry": True,
                        "round": retry_count,
                        "retrieval_degraded": retrieval_degraded,
                    }
                ],
            }

        event = "exhausted" if major else "passed"
        return {
            "critic_issues": [],
            "trace": [
                {
                    "node": "critic_review",
                    "event": event,
                    "major": len(major),
                    "issues": len(issues),
                    "will_retry": False,
                    "round": retry_count,
                    "retrieval_degraded": retrieval_degraded,
                }
            ],
        }

    def route_after_critic(state: _QuizGraphState) -> str:
        return "generate" if state.get("critic_issues") else END

    graph = StateGraph(_QuizGraphState)
    graph.add_node("make_blueprint", make_blueprint)
    graph.add_node("confirm_blueprint", confirm_blueprint)
    graph.add_node("generate", generate)
    graph.add_node("validate", validate)
    graph.add_node("critic_review", critic_review)
    graph.add_edge(START, "make_blueprint")
    graph.add_edge("make_blueprint", "confirm_blueprint")
    graph.add_conditional_edges(
        "confirm_blueprint", route_after_confirm, {"generate": "generate", END: END}
    )
    graph.add_edge("generate", "validate")
    graph.add_conditional_edges(
        "validate",
        route_after_validate,
        {"generate": "generate", "critic_review": "critic_review", END: END},
    )
    graph.add_conditional_edges(
        "critic_review", route_after_critic, {"generate": "generate", END: END}
    )
    return graph
