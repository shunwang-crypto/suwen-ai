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

import json
import uuid
from datetime import datetime

import orjson
from eduagents_core.gateway import ChatMessage as GwMessage
from eduagents_core.gateway import ChatRequest as GwRequest
from eduagents_core.gateway import Tier
from eduagents_core.graphs.quiz_graph import QuestionModel, grade_objective
from eduagents_core.logging import get_logger
from eduagents_core.mastery import scheduler
from eduagents_core.mastery.bkt import BktParams, bkt_update
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import func, select

from .. import timectl
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..error_analysis import CAUSES, classify_error_cause
from ..models import (
    Attempt,
    ErrorItem,
    KnowledgeComponent,
    MasteryLog,
    MasteryRecord,
    Question,
)

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


class AnswerIn(BaseModel):
    response: str = Field(default="", max_length=8000)


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


@router.get("/today")
async def today_queue(request: Request, ctx: AuthContext = Depends(api_rate_limited)):
    """到期错题队列 (F6-A4): state ∈ {new, reviewing} 且 FSRS 到期 (空卡视为到期)。"""
    now_dt = await timectl.now(request.app.state.redis)
    async with tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.scalars(
                select(ErrorItem).where(
                    ErrorItem.user_id == ctx.user_id,
                    ErrorItem.state.in_(_ACTIVE_STATES),
                )
            )
        ).all()
        due_rows = [r for r in rows if scheduler.is_due(r.fsrs_card_json, now_dt)]
        due_rows.sort(key=lambda r: scheduler.due_at(r.fsrs_card_json) or r.created_at)
        items = []
        for row in due_rows[:_TODAY_LIMIT]:
            question = await session.get(Question, row.question_id)
            if question is None:
                continue
            due = scheduler.due_at(row.fsrs_card_json)
            items.append(
                {
                    "error_item_id": str(row.id),
                    "state": row.state,
                    "cause": row.cause,
                    "due": due.isoformat() if due else None,
                    "question": _question_public(question),
                }
            )
    return {"items": items, "now": now_dt.isoformat()}


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
                        '只输出 JSON: {"score": 0到1的小数, "reason": "评分理由"}'
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
    """复习证据的 BKT 直写 + mastery_log 审计 (evidence_type="review", F8-A3)。"""
    changes = []
    params = BktParams()
    for kc_id in kc_ids:
        record = await session.scalar(
            select(MasteryRecord).where(
                MasteryRecord.user_id == ctx.user_id,
                MasteryRecord.kc_id == uuid.UUID(kc_id),
            )
        )
        old_p = record.p_mastery if record else params.p_l0
        new_p = bkt_update(old_p, correct, params, weight=weight)
        if record is None:
            record = MasteryRecord(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                kc_id=uuid.UUID(kc_id),
                p_mastery=new_p,
                confidence=weight,
                last_seen=now_dt,
            )
            session.add(record)
        else:
            record.p_mastery = new_p
            record.confidence = min(1.0, record.confidence + 0.1)
            record.last_seen = now_dt
        session.add(
            MasteryLog(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                kc_id=uuid.UUID(kc_id),
                old_p=old_p,
                new_p=new_p,
                evidence_type="review",
                evidence_id=evidence_id,
                weight=weight,
            )
        )
        changes.append({"kc_id": kc_id, "old_p": round(old_p, 4), "new_p": round(new_p, 4)})
    return changes


@router.post("/{error_item_id}/answer")
async def answer_review(
    error_item_id: uuid.UUID,
    body: AnswerIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """复习作答: 判分 → FSRS review → 状态机推进 → BKT 同步。"""
    now_dt = await timectl.now(request.app.state.redis)
    request_id = getattr(request.state, "request_id", "")
    async with tenant_session(ctx.tenant_id) as session:
        item = await session.get(ErrorItem, error_item_id)
        if item is None or item.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        question = await session.get(Question, item.question_id)
        if question is None:
            raise HTTPException(status_code=404, detail="QUESTION_NOT_FOUND")
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
        streak = int(old_card.get("streak", 0))
        new_card, next_due = scheduler.review(old_card, correct, now_dt)
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
        changes = await _update_mastery_review(
            session, ctx, kc_ids, correct, weight, item.id, now_dt
        )
    return {
        "correct": correct,
        "state": state,
        "cause": cause,
        "next_due": next_due.isoformat(),
        "mastery_changes": changes,
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
            "kc_names (1-3 个), difficulty (easy/mid/hard)。"
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
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx: AuthContext = Depends(api_rate_limited),
):
    """错题列表 (F6): 按状态/错因/知识点筛选, 分页; 含我的作答与正确答案对照。

    state 除四个真实状态外还接受别名 "active" (= new|reviewing, 即仍在复习队列里的),
    对应前端"待复习"统计卡 —— 让卡片点击与列表筛选严格一致, 不做近似。
    error_count = 该题在 attempts 里的错误作答次数 (复习作答不写 attempts, 故只统计测评路径)。
    """
    if state is not None and state not in (*_ALL_STATES, "active"):
        raise HTTPException(status_code=422, detail="INVALID_STATE")
    if cause is not None and cause not in CAUSES:
        raise HTTPException(status_code=422, detail="INVALID_CAUSE")
    async with tenant_session(ctx.tenant_id) as session:
        conditions = [ErrorItem.user_id == ctx.user_id]
        if state == "active":
            conditions.append(ErrorItem.state.in_(_ACTIVE_STATES))
        elif state is not None:
            conditions.append(ErrorItem.state == state)
        if cause is not None:
            conditions.append(ErrorItem.cause == cause)
        if kc_id is not None:
            conditions.append(ErrorItem.kc_ids.contains([str(kc_id)]))  # JSONB @>
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
        questions: dict[str, Question] = {}
        if question_ids:
            questions = {
                str(q.id): q
                for q in (
                    await session.scalars(select(Question).where(Question.id.in_(question_ids)))
                ).all()
            }
        # 我的作答 + 错误次数: 一次性拉本页题目的全部作答, 内存聚合 (避免 N+1)
        wrong_count: dict[str, int] = {}
        last_wrong: dict[str, str] = {}
        last_any: dict[str, str] = {}
        if question_ids:
            attempts = (
                await session.execute(
                    select(Attempt.question_id, Attempt.response, Attempt.is_correct)
                    .where(Attempt.user_id == ctx.user_id, Attempt.question_id.in_(question_ids))
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
        items = []
        for row in rows:
            question = questions.get(str(row.question_id))
            due = scheduler.due_at(row.fsrs_card_json)
            qid = str(row.question_id)
            items.append(
                {
                    "id": str(row.id),
                    "state": row.state,
                    "cause": row.cause,
                    "kc_ids": [str(k) for k in (row.kc_ids or [])],
                    "kc_names": [
                        name_map[str(k)] for k in (row.kc_ids or []) if str(k) in name_map
                    ],
                    "next_due": due.isoformat() if due else None,
                    "error_count": wrong_count.get(qid, 0),
                    "my_answer": last_wrong.get(qid, last_any.get(qid, "")),
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


@errors_router.get("/stats")
async def error_stats(ctx: AuthContext = Depends(api_rate_limited)):
    """错题聚合 (F6): 总数 + 按错因五分类 / 按状态 / 按 KC top10 计数。"""
    async with tenant_session(ctx.tenant_id) as session:
        by_cause = {
            cause: int(n)
            for cause, n in (
                await session.execute(
                    select(ErrorItem.cause, func.count())
                    .where(ErrorItem.user_id == ctx.user_id)
                    .group_by(ErrorItem.cause)
                )
            ).all()
        }
        by_state = {
            state: int(n)
            for state, n in (
                await session.execute(
                    select(ErrorItem.state, func.count())
                    .where(ErrorItem.user_id == ctx.user_id)
                    .group_by(ErrorItem.state)
                )
            ).all()
        }
        # KC 计数: kc_ids 是 JSONB 数组, 逐行内存展开 (单用户错题量级极小, 免 unnest 方言依赖)
        kc_rows = (
            await session.scalars(select(ErrorItem.kc_ids).where(ErrorItem.user_id == ctx.user_id))
        ).all()
        counter: dict[str, int] = {}
        for kc_ids in kc_rows:
            for kc_id in kc_ids or []:
                counter[str(kc_id)] = counter.get(str(kc_id), 0) + 1
        top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:_KC_TOP_N]
        name_map = await _kc_names(session, [kc_id for kc_id, _ in top])
        by_kc = [
            {"kc_id": kc_id, "name": name_map.get(kc_id, ""), "count": n} for kc_id, n in top
        ]
    # 五分类/状态补零, 前端画分布图不必自己兜底
    return {
        "total": sum(by_state.values()),
        "by_cause": {c: by_cause.get(c, 0) for c in CAUSES},
        "by_state": {s: by_state.get(s, 0) for s in _ALL_STATES},
        "by_kc": by_kc,
    }


async def _load_error_item(session, error_item_id: uuid.UUID, ctx: AuthContext) -> ErrorItem:
    item = await session.get(ErrorItem, error_item_id)
    if item is None or item.user_id != ctx.user_id:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return item


@errors_router.post("/{error_item_id}/archive")
async def archive_error(
    error_item_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)
):
    """手动归档: state→archived, 退出复习队列 (today 只取 new/reviewing)。"""
    async with tenant_session(ctx.tenant_id) as session:
        item = await _load_error_item(session, error_item_id, ctx)
        item.state = "archived"
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
    if body.attempt_id is not None:
        attempt = await session.get(Attempt, body.attempt_id)
        if attempt is None or attempt.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        return attempt
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
    return attempt


@appeals_router.post("")
async def appeal_score(
    body: AppealIn, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    """主观题判分申诉 (P2-3): 重调一次 LLM 复核判分 (温度 0)。

    - 仅 llm_score 非空的作答可申诉 (客观题是规则匹配, 无复核空间 -> 422);
    - 复核结果非 live (stub/replay 桩) 或解析失败 -> 202 如实登记待人工复核, 不假复核;
    - |新分-原分| ≥ 0.3 -> 修正 attempt (llm_score/scored_reason 加复核修正标记);
      若跨越 0.6 及格线 -> 按 mastery_log 里该 evidence 的 old_p 回滚掌握度,
      再以新判定走 services._update_mastery 重写 (evidence_type="appeal_regrade");
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
                        '只输出 JSON: {"score": 0到1的小数, "reason": "评分理由"}'
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
            "appeal_kept", attempt_id=str(attempt_id),
            old_score=old_score, new_score=new_score,
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
    from ..services import _update_mastery  # 只调用不改 (P2-3 所有权约定)

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
            changes = await _update_mastery(
                session,
                ctx,
                kc_ids,
                new_correct,
                _SHORT_ANSWER_WEIGHT,
                attempt_id,
                evidence_type="appeal_regrade",
            )
    log.info(
        "appeal_adjusted", attempt_id=str(attempt_id),
        old_score=old_score, new_score=new_score, crossed=crossed,
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
