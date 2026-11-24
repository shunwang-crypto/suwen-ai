"""Educational domain policies and tools for the native agent runtime."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ..gateway.base import BudgetExceeded, ChatMessage, ChatRequest, Tier
from ..json_utils import parse_json_object
from ..kc.types import normalize_kc_name
from ..logging import get_logger
from .context import QuizState
from .toolkit import OBJECT, EducationTools, schema

# 本文件位于 packages/core/src/eduagents_core/education/ -> parents[5] 为仓库根
_PROMPTS_DIR = Path(__file__).resolve().parents[5] / "prompts"
MAX_RETRIES = 2  # 校验失败最多打回 generate 修复 2 轮
MAX_CRITIC_RETRIES = 1  # 独立评审最多返工 1 轮，避免无限增加成本
MAX_CRITIC_ISSUES = 5
MAX_CRITIC_REASON_CHARS = 120
MAX_CRITIC_MATERIAL_CHARS = 20_000
_DIAGNOSTIC_TOP_LEVEL_KEYS = frozenset(
    {"questions", "items", "summary", "schema_version", "data", "error"}
)
MAX_DIAGNOSTIC_TOP_LEVEL_KEYS = 8
QUIZ_GENERATION_BASE_TOKENS = 6_000
QUIZ_GENERATION_TOKENS_PER_QUESTION = 1_200
QUIZ_GENERATION_RETRY_TOKENS = 2_000
QUIZ_GENERATION_MAX_TOKENS = 16_000

_TYPES = ("single", "multi", "blank", "short")
_DIFFICULTIES = ("easy", "mid", "hard")
_LETTERS = ("A", "B", "C", "D")

# 画像注入 (P1-2, 同 qa 任务的注入法): 摘要由 API 层 profile_summary.py 确定性
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
log = get_logger("quiz_tools")


class _QuizTaskContext(QuizState, total=False):
    """任务私有扩展 (同 qa 的 _QaTaskContext): 不进 QuizState 公共契约。"""

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


def quiz_generation_max_tokens(num_questions: int, retry_count: int) -> int:
    """Reserve room for provider reasoning plus the structured question body.

    Some OpenAI-compatible reasoning models count hidden reasoning against
    ``max_tokens``. The previous ``400 * questions + 300`` budget could end
    with ``finish_reason=length`` before any visible JSON was emitted. Repair
    rounds receive additional room, while the hard cap keeps large quizzes
    bounded.
    """
    questions = min(20, max(1, int(num_questions)))
    retries = max(0, int(retry_count))
    return min(
        QUIZ_GENERATION_MAX_TOKENS,
        QUIZ_GENERATION_BASE_TOKENS
        + QUIZ_GENERATION_TOKENS_PER_QUESTION * questions
        + QUIZ_GENERATION_RETRY_TOKENS * retries,
    )


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


def _diagnostic_top_level_keys(raw: dict[str, Any] | None) -> list[str]:
    """Return a bounded, content-safe summary of a model JSON object's keys."""
    if raw is None:
        return []
    keys: list[str] = []
    for key in raw:
        safe_key = key if key in _DIAGNOSTIC_TOP_LEVEL_KEYS else "<other>"
        if safe_key not in keys:
            keys.append(safe_key)
        if len(keys) >= MAX_DIAGNOSTIC_TOP_LEVEL_KEYS:
            break
    return sorted(keys)


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
        deficit = [
            d
            for d in _DIFFICULTIES
            for _ in range(targets[d] - counts[d])
            if targets[d] > counts[d]
        ]  # type: ignore[operator]
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


class QuizTools(EducationTools):
    feature = "agent_quiz"

    def __init__(self, deps, record, emit):
        super().__init__(deps, record, emit)
        self.max_tokens = quiz_generation_max_tokens(int(self.state.get("num_questions") or 5), 0)
        self.add_learning_context()
        self.add_evidence_tools(web=not bool(self.state.get("kb_id")))
        if not self.state.get("blueprint_confirmed"):
            self.add(
                schema(
                    "submit_blueprint",
                    "提交练习蓝图。required 模式必须等真实用户确认后才能生成题目。",
                    {"blueprint": OBJECT},
                    ("blueprint",),
                ),
                self.submit_blueprint,
            )
        if self.state.get("blueprint_confirmed") or self.state.get("confirmation_mode") == "auto":
            self.add(
                schema(
                    "submit_quiz",
                    "提交完整题卷；数量、知识点、难度和题目 schema 由服务端校验，错误返回后修正。",
                    {"questions": {"type": "array", "items": QUESTION_SCHEMA, "maxItems": 20}},
                    ("questions",),
                ),
                self.submit_quiz,
            )

    def policy(self):
        return (
            super().policy()
            + (
                "你负责生成教育练习。蓝图条目包含 kc_name、type(single/multi/blank/short)、difficulty(easy/mid/hard)。"
                "题目包含 type、stem、options、answer、explanation、kc_names、difficulty；选择题必须四个选项。"
                "所有生成内容通过提交工具交给服务器检查；已确认蓝图必须遵守。"
                "若提供知识库，应检索素材后出题；source_chunk_id 只能引用工具提供的 chunk_id。"
            )
            + (
                PROFILE_SECTION_TEMPLATE.replace(
                    "{{PROFILE}}", self.state.get("profile_summary") or ""
                )
                if self.state.get("profile_summary")
                else ""
            )
            + json.dumps(
                {
                    "blueprint": self.state.get("blueprint"),
                    "confirmed": bool(self.state.get("blueprint_confirmed")),
                    "confirmation_mode": self.state.get("confirmation_mode", "required"),
                },
                ensure_ascii=False,
            )
        )

    async def submit_blueprint(self, args):
        if self.state.get("blueprint_confirmed"):
            return {"accepted": False, "errors": ["蓝图已确认，不能由模型覆盖"]}
        raw = args.get("blueprint")
        if not isinstance(raw, dict):
            return {"accepted": False, "errors": ["蓝图必须是对象"]}
        pinned = {
            normalize_kc_name(str(item.get("kc_name", ""))): item.get("difficulty")
            for item in self.state.get("difficulty_reasons") or []
            if isinstance(item, dict)
        }
        blueprint, corrected, enforced = _fix_blueprint(
            raw,
            int(self.state.get("num_questions") or 5),
            self.state.get("difficulty_mix") or {},
            self.state.get("kc_names") or [],
            self.state.get("topic", ""),
            pinned,
        )
        self.state["blueprint"] = blueprint
        self.trace(
            node="make_blueprint",
            event="blueprint_ready",
            corrected=corrected,
            difficulty_enforced=enforced,
        )
        if self.state.get("confirmation_mode") == "auto":
            self.state["blueprint_confirmed"] = True
            return {
                "accepted": True,
                "continue": True,
                "blueprint": blueprint,
                "instruction": "继续检索素材并提交完整题卷",
            }
        self.record.pending = {
            "kind": "blueprint_confirm",
            "blueprint": blueprint,
            "difficulty_reasons": self.state.get("difficulty_reasons") or [],
        }
        self.record.status = "awaiting_confirmation"
        return {
            "accepted": True,
            "status": "awaiting_confirmation",
            "instruction": "停止，等待真实用户确认",
        }

    def confirm(self, decision):
        self.record.pending = None
        if not isinstance(decision, dict) or decision.get("approved") is not True:
            self.record.status = "cancelled"
            self.state.update(
                blueprint_confirmed=False,
                terminal_outcome="cancelled_by_user",
                failure_code="QUIZ_CANCELLED_BY_USER",
            )
            return
        modified = decision.get("blueprint")
        if (
            isinstance(modified, dict)
            and isinstance(modified.get("items"), list)
            and modified["items"]
        ):
            blueprint, _, _ = _fix_blueprint(
                modified,
                min(20, len(modified["items"])),
                self.state.get("difficulty_mix") or {},
                self.state.get("kc_names") or [],
                self.state.get("topic", ""),
                enforce_mix=False,
            )
            self.state.update(blueprint=blueprint, num_questions=len(blueprint["items"]))
        self.state["blueprint_confirmed"] = True
        # The registry is built again after confirmation by the task runtime.

    async def submit_quiz(self, args):
        if not self.state.get("blueprint_confirmed"):
            return {"accepted": False, "errors": ["尚未获得用户蓝图确认"]}
        raw = args.get("questions")
        if (
            self.state.get("kb_id")
            and "search_knowledge_base" in self.handlers
            and not self.state.get("kb_searched")
        ):
            return {"accepted": False, "errors": ["先检索授权知识库的出题素材"]}
        questions = (
            [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        )
        source_ids = [item["chunk_id"] for item in self.state.get("evidences") or []]
        for item in questions:
            hint = item.get("source_hint")
            if (
                isinstance(hint, int)
                and not isinstance(hint, bool)
                and 1 <= hint <= len(source_ids)
            ):
                item["source_chunk_id"] = source_ids[hint - 1]
            if item.get("source_chunk_id") not in source_ids:
                item["source_chunk_id"] = None
        self.state.update(
            questions=questions,
            ever_generated_nonempty=bool(questions)
            or self.state.get("ever_generated_nonempty", False),
        )
        checked = validate_questions(self.state)
        self.state.update({key: value for key, value in checked.items() if key != "trace"})
        for event in checked.get("trace") or []:
            self.trace(**event)
        if self.state.get("validation_errors"):
            if self.state.get("failure_code"):
                self.record.status = "failed"
            return {
                "accepted": False,
                "errors": self.state["validation_errors"],
                "retryable": self.record.status != "failed",
                "status": self.record.status,
            }
        reviewed = await review_questions(self.deps, self.state)
        self.state.update({key: value for key, value in reviewed.items() if key != "trace"})
        for event in reviewed.get("trace") or []:
            self.trace(**event)
        if self.state.get("critic_issues"):
            return {"accepted": False, "errors": self.state["critic_issues"]}
        self.state.update(terminal_outcome="succeeded", failure_code="")
        self.record.status = "completed"
        return {"accepted": True, "status": "completed", "count": len(self.state["questions"])}


def validate_questions(state: QuizState) -> dict:
    raw_questions = state.get("questions") or []
    expected = int(state.get("num_questions") or 0)
    target_kcs = {
        normalize_kc_name(str(name))
        for name in (state.get("kc_names") or [])
        if isinstance(name, str) and normalize_kc_name(name)
    }
    errors: list[str] = []
    validated: list[dict] = []
    if len(raw_questions) != expected:
        errors.append(f"整卷数量不符: 需要 {expected} 题, 实得 {len(raw_questions)} 题")
    for i, question in enumerate(raw_questions, start=1):
        try:
            parsed = QuestionModel.model_validate(question)
            if target_kcs:
                out_of_scope = [
                    name for name in parsed.kc_names if normalize_kc_name(name) not in target_kcs
                ]
                if out_of_scope:
                    errors.append(f"第 {i} 题 kc_names: 未命中目标 KC 白名单")
                    continue
            blueprint = (state.get("blueprint") or {}).get("items") or []
            if i <= len(blueprint):
                item = blueprint[i - 1]
                if parsed.type != item.get("type") or parsed.difficulty != item.get("difficulty"):
                    errors.append(f"第 {i} 题: 题型或难度不符合已确认蓝图")
                    continue
                kc = normalize_kc_name(str(item.get("kc_name", "")))
                if kc and kc not in {normalize_kc_name(name) for name in parsed.kc_names}:
                    errors.append(f"第 {i} 题 kc_names: 不符合已确认蓝图知识点")
                    continue
            validated.append(parsed.model_dump())
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(part) for part in err["loc"]) or "整题"
                errors.append(f"第 {i} 题 {loc}: {err['msg']}")
    retry_count = int(state.get("retry_count") or 0)
    if errors:
        retry_exhausted = retry_count >= MAX_RETRIES
        ever_generated_nonempty = bool(state.get("ever_generated_nonempty"))
        return {
            "validation_errors": errors,
            "retry_count": retry_count + 1,
            **(
                {
                    "terminal_outcome": "validation_failed"
                    if ever_generated_nonempty
                    else "generation_failed",
                    "failure_code": "QUIZ_VALIDATION_FAILED"
                    if ever_generated_nonempty
                    else "QUIZ_GENERATION_EMPTY",
                }
                if retry_exhausted
                else {}
            ),
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
        "terminal_outcome": "succeeded",
        "failure_code": "",
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


async def review_questions(deps, state: _QuizTaskContext) -> dict:
    """独立质量评审；增强项失败可降级，但预算熔断必须继续向上抛出。"""
    questions = state.get("questions") or []
    if not questions:
        return {
            "critic_issues": [],
            "trace": [{"node": "critic_review", "event": "skipped", "reason": "no_questions"}],
        }

    materials: list[dict[str, str | int | None]] = []
    retrieval_degraded = False
    from ..retrieval.sanitize import sanitize_retrieved_text

    remaining = MAX_CRITIC_MATERIAL_CHARS
    for index, item in enumerate((state.get("evidences") or [])[:5], 1):
        text, _ = sanitize_retrieved_text(str(item.get("text") or ""))
        heading, _ = sanitize_retrieved_text(str(item.get("heading_path") or ""))
        text = text[:remaining]
        remaining -= len(text)
        materials.append(
            {"index": index, "heading": heading[:500], "text": text, "page": item.get("page_start")}
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
                billing_reference=f"{state.get('request_id', '')}:quiz-review:{uuid.uuid4().hex}",
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

    data = parse_json_object(result.text)
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
