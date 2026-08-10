"""复习闭环 (F6): 到期错题队列 / FSRS 复习作答状态机 / 变式重练 / 错题本管理。

- 时间一律经 timectl 虚拟时钟 (§9), 禁止 datetime.now();
- 判分规则与 services.record_attempts 同款: 客观题 grade_objective, short 题 LLM 评分;
- 状态机: new→reviewing→mastered (连对 2 次), 答错 streak 清零回 reviewing, 可手动 archived;
- BKT 同步直写 (客观 1.0 / 主观 0.5 权重) + mastery_log 审计 (evidence_type="review");
- 错题本 (errors_router, 前缀 /api/errors): 列表筛选 / 聚合统计 / 归档 / 错因修正;
- 判分申诉 (appeals_router, 前缀 /api/appeals, P2-3): 主观题 LLM 复核, 分差 ≥0.3 修正
  并按 mastery_log 回滚重写掌握度; stub 网关下如实 202 登记待人工复核。
  主线接线 (main.py 一行): app.include_router(reviews.appeals_router)
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

import orjson
from eduagents_core.gateway import ChatMessage as GwMessage
from eduagents_core.gateway import ChatRequest as GwRequest
from eduagents_core.gateway import Tier
from eduagents_core.graphs.quiz_graph import QuestionModel, grade_objective
from eduagents_core.logging import get_logger
from eduagents_core.mastery import scheduler
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import array as pg_array

from .. import timectl
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..error_analysis import CAUSES, classify_error_cause
from ..fsrs_params import resolve_schedule_params
from ..kc_hierarchy import resolve_kc_meta
from ..models import (
    Attempt,
    ErrorItem,
    FsrsReviewEvent,
    KnowledgeComponent,
    MasteryLog,
    MasteryRecord,
    Question,
    ResourceQuestion,
)
from ..plan_adjust import PlanTaskAttributionError, complete_plan_task
from ..services import apply_mastery_evidence, write_learning_event

log = get_logger("reviews")

router = APIRouter(prefix="/api/reviews", tags=["reviews"])
errors_router = APIRouter(prefix="/api/errors", tags=["errors"])
appeals_router = APIRouter(prefix="/api/appeals", tags=["appeals"])

_TODAY_LIMIT = 5  # F6-A4: 每日复习队列 ≤5 条
_ACTIVE_STATES = ("new", "reviewing")
_ALL_STATES = ("new", "reviewing", "mastered", "archived")
_MASTERY_STREAK = 2  # 连对 2 次 → mastered
_SHORT_ANSWER_WEIGHT = 0.5  # §5.6: 主观题证据 0.5 权重
_SHORT_PASS_SCORE = 0.6
_KC_TOP_N = 10  # 统计里按 KC 计数的 top N
# 遗忘快照 (P2-7) 第二时刻点: 现在 + 3 天, 与 profile.py 同口径
_RETENTION_HORIZON = timedelta(days=3)


def _new_review_idempotency_key() -> str:
    """Give a legacy request a unique identity instead of guessing retries.

    The web client sends an explicit key and keeps it for the lifetime of one
    submission.  Older clients cannot provide that guarantee, so each request
    without a key gets a fresh identity; a time bucket must never turn a later
    legitimate review into a replay.
    """
    return f"request:{uuid.uuid4().hex}"


class AnswerIn(BaseModel):
    response: str = Field(default="", max_length=8000)
    idempotency_key: str | None = Field(default=None, max_length=128)
    review_duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    plan_id: uuid.UUID | None = None
    task_key: str | None = Field(default=None, max_length=96)

    @model_validator(mode="after")
    def _plan_attribution_pair(self) -> AnswerIn:
        if (self.plan_id is None) != (self.task_key is None):
            raise ValueError("plan_id 与 task_key 必须同时提供")
        if self.task_key is not None:
            self.task_key = self.task_key.strip()
            if not self.task_key:
                raise ValueError("task_key 不能为空")
        if self.idempotency_key is not None:
            self.idempotency_key = self.idempotency_key.strip() or None
        return self


class CauseIn(BaseModel):
    cause: str = Field(min_length=1, max_length=32)


def _question_public(q: Question) -> dict:
    """出给学生的题面 (不含 answer/explanation)。"""
    return {
        "id": str(q.id),
        "type": q.type,
        "stem": q.stem,
        "options": q.options_json,
        "difficulty": q.difficulty,
        "kc_ids": [str(k) for k in (q.kc_ids or [])],
    }


def _confidence_priority(row) -> float:
    value = getattr(row, "confidence_probability_at_error", None)
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0.0
    )


def filter_due(
    rows: list,
    now_dt: datetime,
    *,
    overcorrection_enabled: bool = False,
) -> list:
    """到期过滤 + 排序 (纯函数, F6 到期口径唯一出处): FSRS 到期 (空卡视为到期),
    按到期时间升序, 空卡按建卡时间。rows 需已限定活跃态 (new/reviewing)。"""
    due_rows = [r for r in rows if scheduler.is_due(r.fsrs_card_json, now_dt)]
    if not overcorrection_enabled:
        due_rows.sort(key=lambda r: scheduler.due_at(r.fsrs_card_json) or r.created_at)
        return due_rows

    # Due-ness is filtered first and remains the primary sort key.  The
    # confidence snapshot only orders rows within the same due layer; a
    # high-confidence error can never jump ahead of an earlier-due card.
    due_rows.sort(
        key=lambda r: (
            scheduler.due_at(r.fsrs_card_json) or r.created_at,
            -_confidence_priority(r),
        )
    )
    return due_rows


async def due_error_items(
    session,
    user_id,
    now_dt: datetime,
    *,
    overcorrection_enabled: bool = False,
    overcorrection_shadow: bool = False,
) -> list:
    """全量到期错题 (B3 同源共用函数): reviews 今日队列与 next_actions 开场卡都调这里。

    此前两处各写一份查询, 复习页"5"(截断后) 与开场卡"37"(全量) 数字打架;
    现在到期集合只有这一个出处, total_due = len(结果), 队列 = 结果[:limit]。
    """
    rows = (
        await session.scalars(
            select(ErrorItem).where(
                ErrorItem.user_id == user_id,
                ErrorItem.state.in_(_ACTIVE_STATES),
                ErrorItem.verification_status == "verified",
            )
        )
    ).all()
    default_rows = filter_due(rows, now_dt)
    if overcorrection_shadow and not overcorrection_enabled:
        shadow_rows = filter_due(rows, now_dt, overcorrection_enabled=True)
        if [row.id for row in shadow_rows] != [row.id for row in default_rows]:
            log.info(
                "overcorrection_queue_shadow",
                candidate_count=len(shadow_rows),
                reordered_count=sum(
                    left.id != right.id
                    for left, right in zip(default_rows, shadow_rows, strict=False)
                ),
            )
    return filter_due(rows, now_dt, overcorrection_enabled=overcorrection_enabled)


@router.get("/today")
async def today_queue(request: Request, ctx: AuthContext = Depends(api_rate_limited)):
    """到期错题队列 (F6-A4): state ∈ {new, reviewing} 且 FSRS 到期 (空卡视为到期)。

    B3: 响应带 total_due (全量到期数, 与 next_actions 同一函数取数), 前端"今日 5 题 /
    共 37 题到期"两个数字同源; 条目带 subject/kb_name/kb_id (kc_hierarchy 推导)。
    """
    now_dt = await timectl.now(request.app.state.redis)
    settings = getattr(request.app.state, "settings", None)
    overcorrection_enabled = bool(
        getattr(settings, "overcorrection_ranking_enabled", False)
    )
    overcorrection_shadow = bool(
        getattr(settings, "overcorrection_ranking_shadow_enabled", True)
    )
    async with tenant_session(ctx.tenant_id) as session:
        due_rows = await due_error_items(
            session,
            ctx.user_id,
            now_dt,
            overcorrection_enabled=overcorrection_enabled,
            overcorrection_shadow=overcorrection_shadow,
        )
        total_due = len(due_rows)
        items = []
        at_3d = now_dt + _RETENTION_HORIZON
        for row in due_rows[:_TODAY_LIMIT]:
            question = await session.get(Question, row.question_id)
            if question is None:
                continue
            due = scheduler.due_at(row.fsrs_card_json)
            # 遗忘快照 (P2-7): 该卡两点保持率; 未复习过的卡为 None (无预测依据)
            r_now = scheduler.predicted_retention(row.fsrs_card_json, now_dt)
            r_3d = scheduler.predicted_retention(row.fsrs_card_json, at_3d)
            items.append(
                {
                    "error_item_id": str(row.id),
                    "state": row.state,
                    "cause": row.cause,
                    "due": due.isoformat() if due else None,
                    "retention_now": round(r_now, 4) if r_now is not None else None,
                    "retention_in_3d": round(r_3d, 4) if r_3d is not None else None,
                    "question": _question_public(question),
                }
            )
    # 队列条目的库/科目归属 (B3): 按题目 KC 推导, 多 KC 题取首个可解析 KC 的归属
    meta = await resolve_kc_meta(
        ctx.tenant_id,
        [k for it in items for k in it["question"]["kc_ids"]],
        redis=request.app.state.redis,
    )
    for it in items:
        info = next((meta[k] for k in it["question"]["kc_ids"] if k in meta), None)
        it["kb_id"] = info["kb_id"] if info else None
        it["kb_name"] = info["kb_name"] if info else ""
        it["subject"] = info["subject"] if info else ""
    return {"items": items, "total_due": total_due, "now": now_dt.isoformat()}


async def _grade_short(
    gateway, ctx: AuthContext, question: Question, response: str, request_id: str
) -> bool:
    """short 题 LLM 评分 (与 services.record_attempts 同款规则: score ≥0.6 记对)。"""
    result = await gateway.chat(
        GwRequest(
            messages=(
                GwMessage(
                    role="user",
                    content=(
                        "你是评分助手。对照参考答案给学生作答打分。\n"
                        f"题目: {question.stem}\n参考答案: {question.answer}\n"
                        f"学生作答: {response}\n"
                        '只输出 JSON: {"score": 0到1的小数, "reason": "评分理由"}。'
                        "reason 中的数学表达式必须使用成对的 $...$, 不得把中文正文"
                        "放进公式定界符, 不得裸输出完整 LaTeX 公式。"
                    ),
                ),
            ),
            tier=Tier.standard,
            json_mode=True,
            feature="review_grade",
            tenant_id=str(ctx.tenant_id),
            user_id=str(ctx.user_id),
            request_id=request_id,
        )
    )
    try:
        parsed = orjson.loads(result.text)
        score = max(0.0, min(1.0, float(parsed.get("score", 0))))
    except Exception:  # noqa: BLE001 — 评分解析失败按 0 分处理
        score = 0.0
    return score >= _SHORT_PASS_SCORE


async def _update_mastery_review(
    session,
    ctx: AuthContext,
    kc_ids: list[str],
    correct: bool,
    weight: float,
    evidence_id: uuid.UUID,
    now_dt: datetime,
) -> list[dict]:
    """复习证据 → 统一 EvidenceUpdater (evidence_type="review", F8-A3)。

    §16: 掌握度只能有一条更新链路。这里曾是 services.apply_mastery_evidence 的
    逐行复制, 但漏了它的 (user_id, kc_id, evidence_type, evidence_id) 幂等去重 ——
    复习接口重试会把同一条证据重复推进 BKT。改为委托后该缺陷一并消失。
    """
    return await apply_mastery_evidence(
        session,
        ctx,
        kc_ids,
        correct,
        weight,
        evidence_id,
        evidence_type="review",
        now_dt=now_dt,
    )


@router.post("/{error_item_id}/answer")
async def answer_review(
    error_item_id: uuid.UUID,
    body: AnswerIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """复习作答: 判分 → FSRS review → 状态机推进 → BKT 同步。"""
    header_idempotency_key = request.headers.get("Idempotency-Key")
    if header_idempotency_key is not None:
        header_idempotency_key = header_idempotency_key.strip()
        if len(header_idempotency_key) > 128:
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key 长度不能超过 128 个字符",
            )
    now_dt = await timectl.now(request.app.state.redis)
    request_id = getattr(request.state, "request_id", "")
    settings = getattr(request.app.state, "settings", None)
    personalization_enabled = bool(
        getattr(settings, "fsrs_personalization_enabled", False)
    )
    plan_task_completion = None
    async with tenant_session(ctx.tenant_id) as session:
        item = await session.scalar(
            select(ErrorItem)
            .where(
                ErrorItem.id == error_item_id,
                ErrorItem.tenant_id == ctx.tenant_id,
                ErrorItem.user_id == ctx.user_id,
            )
            .with_for_update()
        )
        if item is None or item.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        if item.verification_status != "verified":
            raise HTTPException(status_code=409, detail="ANSWER_NOT_VERIFIED")
        question = await session.get(Question, item.question_id)
        if question is None:
            raise HTTPException(status_code=404, detail="QUESTION_NOT_FOUND")
        schedule = await resolve_schedule_params(
            session,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            enabled=personalization_enabled,
        )
        provided_idempotency_key = (body.idempotency_key or header_idempotency_key or "").strip()
        request_fingerprint = hashlib.sha256(
            (
                f"{question.id}\0{body.response}\0{body.review_duration_ms}\0"
                f"{body.plan_id}\0{body.task_key}"
            ).encode()
        ).hexdigest()
        idempotency_key = provided_idempotency_key or _new_review_idempotency_key()
        existing_event = await session.scalar(
            select(FsrsReviewEvent).where(
                FsrsReviewEvent.user_id == ctx.user_id,
                FsrsReviewEvent.idempotency_key == idempotency_key,
            )
        )
        if existing_event is not None:
            if (
                existing_event.error_item_id != item.id
                or (
                    existing_event.request_fingerprint is not None
                    and existing_event.request_fingerprint != request_fingerprint
                )
            ):
                raise HTTPException(status_code=409, detail="REVIEW_IDEMPOTENCY_CONFLICT")
            replay_card = dict(existing_event.card_after_json or {})
            replay_due = scheduler.due_at(replay_card)
            return {
                "correct": existing_event.correct,
                "state": item.state,
                "cause": item.cause,
                "next_due": replay_due.isoformat() if replay_due else None,
                "answer": question.answer,
                "explanation": question.explanation,
                "mastery_changes": [],
                "idempotent": True,
            }
        if question.type in ("single", "multi", "blank"):
            correct = grade_objective(
                {"type": question.type, "answer": question.answer}, body.response
            )
            weight = 1.0
        else:  # short
            correct = await _grade_short(
                request.app.state.gateway, ctx, question, body.response, request_id
            )
            weight = _SHORT_ANSWER_WEIGHT
        # FSRS: 对=Good 错=Again; streak 计数随卡走 (fsrs_card_json 扩展键)
        old_card = dict(item.fsrs_card_json or {})
        was_due = item.state in _ACTIVE_STATES and scheduler.is_due(old_card, now_dt)
        streak = int(old_card.get("streak", 0))
        card_before = scheduler.normalize_business_card_json(old_card, item.id)
        new_card, next_due, review_log = scheduler.review_with_log(
            card_before,
            correct,
            now_dt,
            business_card_id=item.id,
            review_duration_ms=body.review_duration_ms,
            params=schedule.params if personalization_enabled else None,
            params_version=schedule.version,
        )
        if correct:
            streak += 1
            state = "mastered" if streak >= _MASTERY_STREAK else "reviewing"
        else:
            streak = 0
            state = "reviewing"
        new_card["streak"] = streak
        item.fsrs_card_json = new_card
        item.state = state
        # F6-A2 错因自动分类: 只在答错时细化 (入本初值恒为 knowledge_gap, 见 services.py);
        # stub 网关不发请求、异常回落初值, 因此这里绝不会打断复习闭环。
        if not correct:
            item.cause = await classify_error_cause(
                request.app.state.gateway,
                question,
                body.response,
                question.answer,
                tenant_id=str(ctx.tenant_id),
                user_id=str(ctx.user_id),
                request_id=request_id,
            )
        cause = item.cause
        kc_ids = [str(k) for k in (question.kc_ids or [])]
        # 一次复习事件才是一条掌握度证据；ErrorItem.id 是长期复用的卡片身份，
        # 不能用作 evidence_id，否则同一卡片的后续合法复习会被误判为幂等重放。
        review_event_id = uuid.uuid4()
        changes = await _update_mastery_review(
            session, ctx, kc_ids, correct, weight, review_event_id, now_dt
        )
        session.add(
            FsrsReviewEvent(
                id=review_event_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                error_item_id=item.id,
                rating=int(review_log.rating),
                correct=correct,
                reviewed_at=review_log.review_datetime,
                review_duration_ms=review_log.review_duration,
                card_before_json=card_before,
                card_after_json=new_card,
                review_log_json=review_log.to_dict(),
                params_version=schedule.version,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        )
        await session.flush()
        await write_learning_event(
            session,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            source_type="review",
            source_id=item.id,
            question_id=question.id,
            item_key=str(item.id),
            kc_ids=kc_ids,
            correct=correct,
            score=1.0 if correct else 0.0,
            rating=int(review_log.rating),
            difficulty_label=question.difficulty,
            occurred_at=review_log.review_datetime,
            idempotency_key=idempotency_key,
        )
        if body.plan_id is not None and body.task_key is not None and was_due:
            try:
                plan_task_completion = await complete_plan_task(
                    session,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    plan_id=body.plan_id,
                    task_key=body.task_key,
                    source_type="review_answer",
                    source_id=str(item.id),
                    completed_at=now_dt,
                )
            except PlanTaskAttributionError as exc:
                log.warning(
                    "review_plan_task_completion_failed",
                    error_item_id=str(item.id),
                    code=exc.code,
                )
                plan_task_completion = {"completed": False, "error_code": exc.code}
    return {
        "correct": correct,
        "state": state,
        "cause": cause,
        "next_due": next_due.isoformat(),
        "answer": question.answer,
        "explanation": question.explanation,
        "mastery_changes": changes,
        "plan_task_completion": plan_task_completion,
    }


def _parse_json_object(text: str) -> dict | None:
    """尽力解析单个 JSON 对象 (容忍代码块围栏), 失败返回 None。"""
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


@router.post("/{error_item_id}/variant")
async def make_variant(
    error_item_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """变式重练 (F6-A3): 同考点换情境生成新题, 落库并接管该错题的后续复习。"""
    request_id = getattr(request.state, "request_id", "")
    gateway = request.app.state.gateway
    async with tenant_session(ctx.tenant_id) as session:
        item = await session.get(ErrorItem, error_item_id)
        if item is None or item.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        if item.verification_status != "verified":
            raise HTTPException(status_code=409, detail="ANSWER_NOT_VERIFIED")
        original = await session.get(Question, item.question_id)
        if original is None:
            raise HTTPException(status_code=404, detail="QUESTION_NOT_FOUND")
        kc_names: list[str] = []
        for kc_id in original.kc_ids or []:
            kc = await session.get(KnowledgeComponent, uuid.UUID(str(kc_id)))
            if kc is not None:
                kc_names.append(kc.canonical_name)
        base_prompt = (
            "你是出题专家。基于下面的原题, 生成一道**考点完全相同但情境/数字/表述不同**的变式题。\n"
            f"考点: {'、'.join(kc_names) or '与原题一致'}\n"
            f"原题类型: {original.type}\n原题题干: {original.stem}\n"
            f"原题选项: {json.dumps(original.options_json, ensure_ascii=False)}\n"
            f"原题答案: {original.answer}\n\n"
            "要求: 1) 考点一致, 情境不同 (换场景/换数据/换叙述角度); 2) 题型与原题一致;\n"
            "3) single/multi 题 options 恰好 4 项, single 答案为 A-D 单字母, "
            'multi 答案为 "A,C" 式组合; blank/short 题 options 为空数组;\n'
            "4) 只输出一个 JSON 对象, 字段: type, stem, options, answer, explanation, "
            "kc_names (1-3 个), difficulty (easy/mid/hard)。\n"
            "5) 数学表达式必须使用 LaTeX: 行内公式严格写成 $...$; 块公式的开始和结束 "
            "$$ 各自独占一行, JSON 字符串中的换行编码为 \\n。不得省略闭合定界符, "
            "不得把中文正文放进公式定界符, 不得裸输出完整公式。"
        )
        model: QuestionModel | None = None
        prompt = base_prompt
        for _round in range(2):  # 失败重试 1 次, 带错误清单
            result = await gateway.chat(
                GwRequest(
                    messages=(GwMessage(role="user", content=prompt),),
                    tier=Tier.standard,
                    temperature=0.7,
                    json_mode=True,
                    feature="review_variant",
                    tenant_id=str(ctx.tenant_id),
                    user_id=str(ctx.user_id),
                    request_id=request_id,
                )
            )
            raw = _parse_json_object(result.text) or {}
            if isinstance(raw.get("question"), dict):  # 容忍 {"question": {...}} 包装
                raw = raw["question"]
            try:
                model = QuestionModel.model_validate(raw)
                break
            except ValidationError as exc:
                errors = "; ".join(e["msg"] for e in exc.errors())
                log.warning("variant_validation_failed", errors=errors)
                prompt = base_prompt + f"\n\n上一轮输出未通过校验, 必须修复: {errors}"
        if model is None:
            raise HTTPException(status_code=502, detail="VARIANT_GENERATION_FAILED")
        new_q = Question(
            tenant_id=ctx.tenant_id,
            quiz_id=original.quiz_id,  # 沿用原题的 quiz
            type=model.type,
            stem=model.stem,
            options_json=model.options,
            answer=model.answer,
            explanation=model.explanation,
            kc_ids=list(original.kc_ids or []),  # 考点一致: 直接沿用原题归一化 kc_ids
            source_chunk_id=original.source_chunk_id,
            source_type="generated",
            source_meta_json={"variant_of": str(original.id)},
            difficulty=model.difficulty,
        )
        session.add(new_q)
        await session.flush()
        item.variant_of = original.id  # 指向原题, 后续复习答变式题
        item.question_id = new_q.id
        payload = _question_public(new_q)
    return {
        "error_item_id": str(error_item_id),
        "variant_of": str(original.id),
        "question": payload,
    }


# ---------------------------------------------------------------- 错题本 (F6: /api/errors)


async def _kc_names(session, kc_ids: list[str]) -> dict[str, str]:
    """kc_id -> canonical_name 批量映射 (非法 uuid 静默跳过)。"""
    valid: list[uuid.UUID] = []
    for kc_id in set(kc_ids):
        try:
            valid.append(uuid.UUID(str(kc_id)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not valid:
        return {}
    rows = (
        await session.execute(
            select(KnowledgeComponent.id, KnowledgeComponent.canonical_name).where(
                KnowledgeComponent.id.in_(valid)
            )
        )
    ).all()
    return {str(kc_id): name for kc_id, name in rows}


@errors_router.get("")
async def list_errors(
    state: str | None = Query(default=None),
    cause: str | None = Query(default=None),
    kc_id: uuid.UUID | None = Query(default=None),
    kb_id: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx: AuthContext = Depends(api_rate_limited),
):
    """错题列表 (F6): 按状态/错因/知识点/知识库及全文搜索筛选, 分页; 含答案对照。

    state 除四个真实状态外还接受别名 "active" (= new|reviewing, 即仍在复习队列里的),
    对应前端"待复习"统计卡 —— 让卡片点击与列表筛选严格一致, 不做近似。
    error_count = 该题在 attempts 里的错误作答次数 (复习作答不写 attempts, 故只统计测评路径)。
    B4: kb_id 过滤经 kc_ids ∩ 该库 KC 集合实现 (jsonb ?| text[]), 不加冗余列不动迁移。
    q 覆盖题干、解析和知识点名，搜索在分页前完成，结果数与翻页保持真实。
    """
    if state is not None and state not in (*_ALL_STATES, "active"):
        raise HTTPException(status_code=422, detail="INVALID_STATE")
    if cause is not None and cause not in CAUSES:
        raise HTTPException(status_code=422, detail="INVALID_CAUSE")
    async with tenant_session(ctx.tenant_id) as session:
        conditions = [ErrorItem.user_id == ctx.user_id]
        if state == "active":
            conditions.append(ErrorItem.state.in_(_ACTIVE_STATES))
            conditions.append(ErrorItem.verification_status == "verified")
        elif state is not None:
            conditions.append(ErrorItem.state == state)
        if cause is not None:
            conditions.append(ErrorItem.cause == cause)
        if kc_id is not None:
            conditions.append(ErrorItem.kc_ids.contains([str(kc_id)]))  # JSONB @>
        if kb_id is not None:
            # B4: 先取该库全量 KC id, 再用 jsonb ?| (任意元素命中) 圈定错题
            kb_kc_ids = [
                str(k)
                for k in (
                    await session.scalars(
                        select(KnowledgeComponent.id).where(KnowledgeComponent.kb_id == kb_id)
                    )
                ).all()
            ]
            if not kb_kc_ids:
                return {"items": [], "total": 0, "page": page, "page_size": page_size, "pages": 0}
            conditions.append(ErrorItem.kc_ids.op("?|")(pg_array(kb_kc_ids)))
        search = (q or "").strip()
        if search:
            pattern = f"%{search}%"
            matched_kc_ids = [
                str(value)
                for value in (
                    await session.scalars(
                        select(KnowledgeComponent.id).where(
                            KnowledgeComponent.canonical_name.ilike(pattern)
                        )
                    )
                ).all()
            ]
            question_matches = select(Question.id).where(
                or_(
                    Question.stem.ilike(pattern),
                    Question.explanation.ilike(pattern),
                )
            )
            search_conditions = [ErrorItem.question_id.in_(question_matches)]
            if matched_kc_ids:
                search_conditions.append(ErrorItem.kc_ids.op("?|")(pg_array(matched_kc_ids)))
            conditions.append(or_(*search_conditions))
        total = int(
            await session.scalar(select(func.count()).select_from(ErrorItem).where(*conditions))
            or 0
        )
        rows = (
            await session.scalars(
                select(ErrorItem)
                .where(*conditions)
                .order_by(ErrorItem.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()

        question_ids = [r.question_id for r in rows]
        resource_question_ids = [r.resource_question_id for r in rows if r.resource_question_id is not None]
        questions: dict[str, Question] = {}
        resource_ids: dict[str, str] = {}
        if question_ids:
            questions = {
                str(q.id): q
                for q in (
                    await session.scalars(select(Question).where(Question.id.in_(question_ids)))
                ).all()
            }
        if resource_question_ids:
            resource_ids = {
                str(resource_question_id): str(resource_id)
                for resource_question_id, resource_id in (
                    await session.execute(
                        select(ResourceQuestion.id, ResourceQuestion.resource_id).where(
                            ResourceQuestion.id.in_(resource_question_ids)
                        )
                    )
                ).all()
            }
        # 我的作答 + 错误次数: 一次性拉本页题目的全部作答, 内存聚合 (避免 N+1)
        wrong_count: dict[str, int] = {}
        last_wrong: dict[str, str] = {}
        last_any: dict[str, str] = {}
        attempt_question_ids = [r.question_id for r in rows if r.source_import_id is None]
        if attempt_question_ids:
            attempts = (
                await session.execute(
                    select(Attempt.question_id, Attempt.response, Attempt.is_correct)
                    .where(
                        Attempt.user_id == ctx.user_id,
                        Attempt.question_id.in_(attempt_question_ids),
                    )
                    .order_by(Attempt.at.asc())
                )
            ).all()
            for qid, response, is_correct in attempts:
                key = str(qid)
                last_any[key] = response
                if is_correct is False:
                    wrong_count[key] = wrong_count.get(key, 0) + 1
                    last_wrong[key] = response

        name_map = await _kc_names(session, [k for r in rows for k in (r.kc_ids or [])])
        evidence_ids = set()
        if rows:
            evidence_ids = {
                evidence_id
                for evidence_id in (
                    await session.scalars(
                        select(MasteryLog.evidence_id).where(
                            MasteryLog.user_id == ctx.user_id,
                            MasteryLog.evidence_id.in_([r.id for r in rows]),
                        )
                    )
                ).all()
                if evidence_id is not None
            }
        items = []
        for row in rows:
            question = questions.get(str(row.question_id))
            due = (
                scheduler.due_at(row.fsrs_card_json)
                if row.verification_status == "verified"
                else None
            )
            qid = str(row.question_id)
            items.append(
                {
                    "id": str(row.id),
                    "state": row.state,
                    "cause": row.cause,
                    "verification_status": row.verification_status,
                    "kc_ids": [str(k) for k in (row.kc_ids or [])],
                    "kc_names": [
                        name_map[str(k)] for k in (row.kc_ids or []) if str(k) in name_map
                    ],
                    "next_due": due.isoformat() if due else None,
                    "error_count": 0 if row.source_import_id is not None else wrong_count.get(qid, 0),
                    "my_answer": (
                        row.original_response or ""
                        if row.source_import_id is not None
                        else last_wrong.get(qid, last_any.get(qid, ""))
                    ),
                    "source_import_id": (
                        str(row.source_import_id) if row.source_import_id else None
                    ),
                    "resource_id": resource_ids.get(str(row.resource_question_id))
                    if row.resource_question_id
                    else None,
                    "resource_question_id": str(row.resource_question_id)
                    if row.resource_question_id
                    else None,
                    "content_editable": not bool((row.fsrs_card_json or {}).get("last_review"))
                    and row.id not in evidence_ids,
                    "is_variant": row.variant_of is not None,
                    "created_at": row.created_at,
                    "question": (
                        {
                            "id": qid,
                            "type": question.type,
                            "stem": question.stem,
                            "options": question.options_json,
                            "answer": question.answer,
                            "explanation": question.explanation,
                            "difficulty": question.difficulty,
                            "source_type": question.source_type,
                            "source_meta": question.source_meta_json or {},
                        }
                        if question is not None
                        else None
                    ),
                }
            )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@errors_router.get("/{error_item_id}/tutor-context")
async def error_tutor_context(
    error_item_id: uuid.UUID,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """Resolve an owned wrong answer for Tutor without exposing its text in a URL."""
    async with tenant_session(ctx.tenant_id) as session:
        row = await session.scalar(
            select(ErrorItem).where(
                ErrorItem.id == error_item_id,
                ErrorItem.user_id == ctx.user_id,
            )
        )
        if row is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        question = await session.get(Question, row.question_id)
        if question is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        return {
            "problem": question.stem[:4000],
            "error_item_id": str(row.id),
            "kc_ids": [str(kc_id) for kc_id in (row.kc_ids or [])],
        }


@errors_router.get("/stats")
async def error_stats(ctx: AuthContext = Depends(api_rate_limited)):
    """错题聚合 (F6): 总数 + 按错因五分类 / 按状态 / 按 KC top10 计数。"""
    async with tenant_session(ctx.tenant_id) as session:
        by_cause = {
            cause: int(n)
            for cause, n in (
                await session.execute(
                    select(ErrorItem.cause, func.count())
                    .where(
                        ErrorItem.user_id == ctx.user_id,
                        ErrorItem.verification_status == "verified",
                    )
                    .group_by(ErrorItem.cause)
                )
            ).all()
        }
        by_state = {
            state: int(n)
            for state, n in (
                await session.execute(
                    select(ErrorItem.state, func.count())
                    .where(
                        ErrorItem.user_id == ctx.user_id,
                        ErrorItem.verification_status == "verified",
                    )
                    .group_by(ErrorItem.state)
                )
            ).all()
        }
        pending_verification = int(
            await session.scalar(
                select(func.count())
                .select_from(ErrorItem)
                .where(
                    ErrorItem.user_id == ctx.user_id,
                    ErrorItem.state.in_(_ACTIVE_STATES),
                    ErrorItem.verification_status == "pending",
                )
            )
            or 0
        )
        # KC 计数: kc_ids 是 JSONB 数组, 逐行内存展开 (单用户错题量级极小, 免 unnest 方言依赖)
        kc_rows = (
            await session.scalars(
                select(ErrorItem.kc_ids).where(
                    ErrorItem.user_id == ctx.user_id,
                    ErrorItem.verification_status == "verified",
                )
            )
        ).all()
        counter: dict[str, int] = {}
        for kc_ids in kc_rows:
            for kc_id in kc_ids or []:
                counter[str(kc_id)] = counter.get(str(kc_id), 0) + 1
        top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:_KC_TOP_N]
        name_map = await _kc_names(session, [kc_id for kc_id, _ in top])
        by_kc = [{"kc_id": kc_id, "name": name_map.get(kc_id, ""), "count": n} for kc_id, n in top]
    # 五分类/状态补零, 前端画分布图不必自己兜底
    return {
        "total": sum(by_state.values()),
        "by_cause": {c: by_cause.get(c, 0) for c in CAUSES},
        "by_state": {s: by_state.get(s, 0) for s in _ALL_STATES},
        "pending_verification": pending_verification,
        "by_kc": by_kc,
    }


async def _load_error_item(session, error_item_id: uuid.UUID, ctx: AuthContext) -> ErrorItem:
    item = await session.get(ErrorItem, error_item_id)
    if item is None or item.user_id != ctx.user_id:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return item


@errors_router.post("/{error_item_id}/archive")
async def archive_error(error_item_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    """手动归档: state→archived, 退出复习队列 (today 只取 new/reviewing)。"""
    async with tenant_session(ctx.tenant_id) as session:
        item = await _load_error_item(session, error_item_id, ctx)
        item.state = "archived"
        return {"id": str(item.id), "state": item.state}


@errors_router.post("/{error_item_id}/restore")
async def restore_error(error_item_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    """将已归档错题恢复到复习队列；保留原 FSRS 卡片与历史证据。"""
    async with tenant_session(ctx.tenant_id) as session:
        item = await _load_error_item(session, error_item_id, ctx)
        if item.state == "archived":
            item.state = "reviewing"
        return {"id": str(item.id), "state": item.state}


@errors_router.post("/{error_item_id}/cause")
async def update_cause(
    error_item_id: uuid.UUID,
    body: CauseIn,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """错因修正 (用户/系统重新标注五分类)。"""
    if body.cause not in CAUSES:
        raise HTTPException(status_code=422, detail="INVALID_CAUSE")
    async with tenant_session(ctx.tenant_id) as session:
        item = await _load_error_item(session, error_item_id, ctx)
        item.cause = body.cause
        return {"id": str(item.id), "cause": item.cause}


# ---------------------------------------------------------------- 判分申诉 (P2-3: /api/appeals)

_APPEAL_ADJUST_DELTA = 0.3  # 复核分与原分差 ≥0.3 才修正 (LLM 评分天然抖动, 小差维持原判)
_APPEAL_REASON_MAX = 2000


class AppealIn(BaseModel):
    """attempt_id 与 question_id 二选一:

    quiz 判分结果与错题本都不下发 attempt_id (历史契约), 故支持传 question_id,
    由后端解析该用户在此题上最近一次 LLM 判分的作答 —— 这是唯一可申诉对象。
    """

    attempt_id: uuid.UUID | None = None
    question_id: uuid.UUID | None = None
    reason: str = Field(min_length=1, max_length=_APPEAL_REASON_MAX)

    @model_validator(mode="after")
    def _exactly_one_target(self) -> AppealIn:
        if (self.attempt_id is None) == (self.question_id is None):
            raise ValueError("attempt_id 与 question_id 必须恰好传一个")
        return self


async def _load_appeal_attempt(session, body: AppealIn, ctx: AuthContext) -> Attempt:
    """定位可申诉的 attempt: 必须属于本人; question_id 路径取最近一次 LLM 判分作答。"""

    async def _ordinary(attempt: Attempt) -> Attempt:
        question = await session.get(Question, attempt.question_id)
        if question is not None and (question.source_meta_json or {}).get("import_id"):
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        return attempt

    if body.attempt_id is not None:
        attempt = await session.get(Attempt, body.attempt_id)
        if attempt is None or attempt.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        return await _ordinary(attempt)
    attempt = await session.scalar(
        select(Attempt)
        .where(
            Attempt.user_id == ctx.user_id,
            Attempt.question_id == body.question_id,
            Attempt.llm_score.is_not(None),
        )
        .order_by(Attempt.at.desc())
        .limit(1)
    )
    if attempt is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return await _ordinary(attempt)


@appeals_router.post("")
async def appeal_score(
    body: AppealIn, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    """主观题判分申诉 (P2-3): 重调一次 LLM 复核判分 (温度 0)。

    - 仅 llm_score 非空的作答可申诉 (客观题是规则匹配, 无复核空间 -> 422);
    - 复核结果非 live (stub/replay 桩) 或解析失败 -> 202 如实登记待人工复核, 不假复核;
    - |新分-原分| ≥ 0.3 -> 修正 attempt (llm_score/scored_reason 加复核修正标记);
      若跨越 0.6 及格线 -> 按 mastery_log 里该 evidence 的 old_p 回滚掌握度,
      再以新判定走 services.apply_mastery_evidence 重写 (evidence_type="appeal_regrade");
    - 分差 <0.3 -> 维持原判, scored_reason 追加复核说明。响应始终含前后分数。
    """
    request_id = getattr(request.state, "request_id", "")
    async with tenant_session(ctx.tenant_id) as session:
        attempt = await _load_appeal_attempt(session, body, ctx)
        if attempt.llm_score is None:
            # 客观题规则判分, 不存在"评分主观误差", 申诉无对象
            raise HTTPException(status_code=422, detail="APPEAL_NOT_ALLOWED")
        question = await session.get(Question, attempt.question_id)
        if question is None:
            raise HTTPException(status_code=404, detail="QUESTION_NOT_FOUND")
        attempt_id = attempt.id
        old_score = float(attempt.llm_score)
        old_correct = attempt.is_correct is True
        stem, answer, response = question.stem, question.answer, attempt.response
        kc_ids = [str(k) for k in (question.kc_ids or [])]

    # LLM 复核在事务外进行 (不给慢调用挂长事务); 温度 0 + 明示"仔细复核"
    result = await request.app.state.gateway.chat(
        GwRequest(
            messages=(
                GwMessage(
                    role="user",
                    content=(
                        "你是评分复核助手。学生对之前的评分提出申诉, 请仔细复核: "
                        "逐点对照参考答案独立重新打分, 不要参考或迁就任何已有分数。\n"
                        f"题目: {stem}\n参考答案: {answer}\n"
                        f"学生作答: {response}\n"
                        f"学生申诉理由: {body.reason}\n"
                        '只输出 JSON: {"score": 0到1的小数, "reason": "评分理由"}。'
                        "reason 中的数学表达式必须使用成对的 $...$, 不得把中文正文"
                        "放进公式定界符, 不得裸输出完整 LaTeX 公式。"
                    ),
                ),
            ),
            tier=Tier.standard,
            temperature=0.0,
            json_mode=True,
            feature="appeal_regrade",
            tenant_id=str(ctx.tenant_id),
            user_id=str(ctx.user_id),
            request_id=request_id,
        )
    )
    new_score: float | None = None
    new_reason = ""
    if result.mode == "live":
        parsed = _parse_json_object(result.text)
        if parsed is not None and "score" in parsed:
            try:
                new_score = max(0.0, min(1.0, float(parsed.get("score", 0))))
                new_reason = str(parsed.get("reason", ""))[:2000]
            except (TypeError, ValueError):
                new_score = None

    if new_score is None:
        # stub/replay 桩 或 复核输出不可解析: 不假装复核过, 如实登记待人工 (§14)
        async with tenant_session(ctx.tenant_id) as session:
            attempt = await session.get(Attempt, attempt_id)
            note = f"[申诉已登记待人工复核: {body.reason[:200]}]"
            if attempt is not None and note not in (attempt.scored_reason or ""):
                attempt.scored_reason = f"{attempt.scored_reason or ''}\n{note}".strip()
        log.info("appeal_pending_manual", attempt_id=str(attempt_id), mode=result.mode)
        return ORJSONResponse(
            status_code=202,
            content={
                "status": "pending_manual_review",
                "message": "当前无法自动复核，申诉已登记，将由人工复核处理",
                "attempt_id": str(attempt_id),
                "old_score": old_score,
                "new_score": None,
                "adjusted": False,
                "crossed": False,
                "mastery_changes": [],
            },
        )

    if abs(new_score - old_score) < _APPEAL_ADJUST_DELTA:
        async with tenant_session(ctx.tenant_id) as session:
            attempt = await session.get(Attempt, attempt_id)
            if attempt is not None:
                attempt.scored_reason = (
                    f"{attempt.scored_reason or ''}\n"
                    f"[申诉复核维持原判: 复核分 {new_score:.2f}, 原分 {old_score:.2f}]"
                ).strip()
        log.info(
            "appeal_kept",
            attempt_id=str(attempt_id),
            old_score=old_score,
            new_score=new_score,
        )
        return {
            "status": "kept",
            "message": f"复核分 {new_score:.2f} 与原分差异小于 {_APPEAL_ADJUST_DELTA}，维持原判",
            "attempt_id": str(attempt_id),
            "old_score": old_score,
            "new_score": new_score,
            "adjusted": False,
            "crossed": False,
            "mastery_changes": [],
        }

    # 分差 ≥0.3: 修正判分; 跨越及格线时回滚并重写掌握度
    new_correct = new_score >= _SHORT_PASS_SCORE
    crossed = new_correct != old_correct
    changes: list[dict] = []
    async with tenant_session(ctx.tenant_id) as session:
        attempt = await session.get(Attempt, attempt_id)
        if attempt is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        attempt.llm_score = new_score
        attempt.scored_reason = (
            f"{new_reason}\n[申诉复核修正: 原分 {old_score:.2f} → {new_score:.2f}]"
        ).strip()
        if crossed:
            attempt.is_correct = new_correct
            # 回滚: 该 attempt 写入的 mastery_log 里最早一条的 old_p = 判分前状态
            entries = (
                await session.scalars(
                    select(MasteryLog)
                    .where(
                        MasteryLog.evidence_id == attempt_id,
                        MasteryLog.evidence_type == "quiz_attempt",
                        MasteryLog.user_id == ctx.user_id,
                    )
                    .order_by(MasteryLog.at.asc())
                )
            ).all()
            pre_p: dict[str, float] = {}
            for entry in entries:
                pre_p.setdefault(str(entry.kc_id), entry.old_p)
            for kc_id, old_p in pre_p.items():
                record = await session.scalar(
                    select(MasteryRecord).where(
                        MasteryRecord.user_id == ctx.user_id,
                        MasteryRecord.kc_id == uuid.UUID(kc_id),
                    )
                )
                if record is not None:
                    record.p_mastery = old_p  # confidence 无法精确还原, 如实保留
            # 重写: 以复核后的判定作为新证据 (审计链保留原始行 + 新增 appeal_regrade 行)
            changes = await apply_mastery_evidence(
                session,
                ctx,
                kc_ids,
                new_correct,
                _SHORT_ANSWER_WEIGHT,
                attempt_id,
                evidence_type="appeal_regrade",
            )
    log.info(
        "appeal_adjusted",
        attempt_id=str(attempt_id),
        old_score=old_score,
        new_score=new_score,
        crossed=crossed,
    )
    return {
        "status": "adjusted",
        "message": (
            f"复核修正: {old_score:.2f} → {new_score:.2f}"
            + ("，判定结果已更新并同步掌握度" if crossed else "")
        ),
        "attempt_id": str(attempt_id),
        "old_score": old_score,
        "new_score": new_score,
        "adjusted": True,
        "crossed": crossed,
        "mastery_changes": changes,
    }
