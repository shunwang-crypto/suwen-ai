from __future__ import annotations

import uuid
from datetime import UTC, datetime

from eduagents_core.gateway import ChatMessage as GwMessage
from eduagents_core.gateway import ChatRequest as GwRequest
from eduagents_core.gateway import Tier
from eduagents_core.logging import get_logger
from eduagents_core.mastery.bkt import BktParams, bkt_update
from sqlalchemy import select

from .db import tenant_session
from .deps import AuthContext
from .models import (
    Attempt,
    ErrorItem,
    KnowledgeComponent,
    MasteryLog,
    MasteryRecord,
    Plan,
    Question,
    Quiz,
)

log = get_logger("services")

_SHORT_ANSWER_WEIGHT = 0.5  # §5.6: 主观题证据 0.5 权重进 BKT (写死)
_SHORT_PASS_SCORE = 0.6
_TUTOR_EVIDENCE_WEIGHT = 0.5  # 辅导小结证据权重 (与主观题同档: 间接证据减半)
_MIN_KC_MATCH_LEN = 2  # 归一化后过短的 KC 名不参与子串匹配 (防单字误伤)


async def _resolve_kc_ids(session, kb_id: str | None, kc_names: list[str]) -> list[str]:
    """kc_name -> 归一化 kc_id (F5-A2: 禁止裸字符串)。无词表/未命中 -> 丢弃该名。"""
    if not kb_id or not kc_names:
        return []
    from eduagents_core.kc import KcEntry, KcVocabulary

    rows = (
        await session.scalars(
            select(KnowledgeComponent).where(
                KnowledgeComponent.kb_id == uuid.UUID(kb_id)
            )
        )
    ).all()
    vocab = KcVocabulary(
        entries=[
            KcEntry(
                kc_id=str(r.id),
                canonical_name=r.canonical_name,
                aliases=list(r.aliases),
                description=r.description,
                embedding=None,
            )
            for r in rows
        ]
    )
    resolved: list[str] = []
    for name in kc_names:
        entry = vocab.by_name(name)
        if entry is not None and entry.kc_id not in resolved:
            resolved.append(entry.kc_id)
    return resolved


async def finalize_quiz(
    ctx: AuthContext, local_id: str, state: dict
) -> dict:
    """quiz 图完成 -> Quiz + Questions 落库 (kc_names 归一为 kc_ids)。"""
    questions = state.get("questions") or []
    kb_id = state.get("kb_id") or None
    async with tenant_session(ctx.tenant_id) as session:
        quiz = Quiz(
            id=uuid.UUID(local_id),
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            kb_id=uuid.UUID(kb_id) if kb_id else None,
            title=state.get("topic", "")[:255],
            blueprint_json=state.get("blueprint") or {},
            status="ready",
        )
        session.add(quiz)
        # 先 flush 父行: UoW 跨表排序不看数据库外键 (同 enrichment.py 的教训)
        await session.flush()
        out = []
        for q in questions:
            kc_ids = await _resolve_kc_ids(session, kb_id, q.get("kc_names", []))
            source_chunk = q.get("source_chunk_id")
            row = Question(
                tenant_id=ctx.tenant_id,
                quiz_id=quiz.id,
                type=q["type"],
                stem=q["stem"],
                options_json=q.get("options", []),
                answer=q["answer"],
                explanation=q.get("explanation", ""),
                kc_ids=kc_ids,
                source_chunk_id=uuid.UUID(source_chunk) if source_chunk else None,
                difficulty=q.get("difficulty", "mid"),
            )
            session.add(row)
            await session.flush()
            out.append(
                {
                    "id": str(row.id),
                    "type": row.type,
                    "stem": row.stem,
                    "options": row.options_json,
                    "difficulty": row.difficulty,
                    "kc_ids": kc_ids,
                }
            )
    return {"quiz_id": local_id, "questions": out}


async def finalize_plan(ctx: AuthContext, local_id: str, state: dict) -> dict:
    async with tenant_session(ctx.tenant_id) as session:
        session.add(
            Plan(
                id=uuid.UUID(local_id),
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                goal=state.get("goal", ""),
                budget_hours=float(state.get("budget_hours_per_day") or 0),
                structure_json=state.get("plan") or {},
            )
        )
    return {"plan_id": local_id, "plan": state.get("plan") or {}}


async def _update_mastery(
    session,
    ctx: AuthContext,
    kc_ids: list[str],
    correct: bool,
    weight: float,
    evidence_id,
    evidence_type: str = "quiz_attempt",
) -> list[dict]:
    """BKT 直写 (确定性, 不经 LLM) + mastery_log 审计 (F8-A3)。

    evidence_type 区分证据来源 (quiz_attempt | tutor_summary | ...), 默认值保持向后兼容。

    幂等 (P1-7): 同一 (user_id, kc_id, evidence_type, evidence_id) 已有 mastery_log
    即跳过 — worker 重试/重复入队 (如 reflect_session) 不会重复推 BKT 污染画像。
    evidence_id=None (tutor_summary) 无去重键, 保持原样。
    """
    changes = []
    params = BktParams()
    applied: set[str] = set()
    if evidence_id is not None and kc_ids:
        existing_logs = (
            await session.scalars(
                select(MasteryLog.kc_id).where(
                    MasteryLog.user_id == ctx.user_id,
                    MasteryLog.evidence_type == evidence_type,
                    MasteryLog.evidence_id == evidence_id,
                )
            )
        ).all()
        applied = {str(k) for k in existing_logs}
    for kc_id in kc_ids:
        if str(kc_id) in applied:
            continue
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
                last_seen=datetime.now(UTC),
            )
            session.add(record)
        else:
            record.p_mastery = new_p
            record.confidence = min(1.0, record.confidence + 0.1)
            record.last_seen = datetime.now(UTC)
        session.add(
            MasteryLog(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                kc_id=uuid.UUID(kc_id),
                old_p=old_p,
                new_p=new_p,
                evidence_type=evidence_type,
                evidence_id=evidence_id,
                weight=weight,
            )
        )
        changes.append({"kc_id": kc_id, "old_p": round(old_p, 4), "new_p": round(new_p, 4)})
    return changes


async def apply_tutor_summary(ctx: AuthContext, summary: dict) -> list[dict]:
    """辅导小结落画像 (F4→F8 联动): misconceptions 命中已有 KC 词表 → BKT 负向证据。

    匹配规则: KC canonical/alias 归一化后作为子串出现在误解文本中即命中;
    词表外的误解如实丢弃, 不新建 KC (画像外键只认归一化 kc_id, F5-A2)。
    命中 KC 以 correct=False、0.5 权重、evidence_type="tutor_summary" 进 BKT,
    返回变化列表 (附 KC 名, 供前端复盘卡展示)。
    """
    from eduagents_core.kc.types import normalize_kc_name

    texts = [
        normalize_kc_name(m)
        for m in (summary.get("misconceptions") or [])
        if isinstance(m, str) and m.strip()
    ]
    if not texts:
        return []
    async with tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.scalars(
                select(KnowledgeComponent).order_by(KnowledgeComponent.created_at)
            )
        ).all()
        matched: list[str] = []
        name_by_id: dict[str, str] = {}
        for row in rows:
            keys = [normalize_kc_name(row.canonical_name)] + [
                normalize_kc_name(a) for a in (row.aliases or []) if isinstance(a, str)
            ]
            keys = [k for k in keys if len(k) >= _MIN_KC_MATCH_LEN]
            if any(k in t for t in texts for k in keys):
                kc_id = str(row.id)
                if kc_id not in matched:
                    matched.append(kc_id)
                    name_by_id[kc_id] = row.canonical_name
        if not matched:
            return []
        changes = await _update_mastery(
            session,
            ctx,
            matched,
            correct=False,
            weight=_TUTOR_EVIDENCE_WEIGHT,
            evidence_id=None,
            evidence_type="tutor_summary",
        )
    return [{**c, "name": name_by_id.get(c["kc_id"], "")} for c in changes]


async def record_attempts(
    ctx: AuthContext,
    gateway,
    quiz_id: uuid.UUID,
    answers: list[dict],
    now_dt: datetime | None = None,
) -> dict:
    """作答判分: 客观题本地判 / 简答 LLM 评分附理由; 错题自动入本; BKT 直写 (F5-A3)。

    now_dt: 业务时间 (路由层从 timectl 虚拟时钟取, §9); 缺省回退系统时间保持向后兼容。
    """
    from eduagents_core.graphs.quiz_graph import grade_objective
    from eduagents_core.mastery.scheduler import new_card_json

    business_now = now_dt or datetime.now(UTC)
    results = []
    async with tenant_session(ctx.tenant_id) as session:
        questions = {
            str(q.id): q
            for q in (
                await session.scalars(
                    select(Question).where(Question.quiz_id == quiz_id)
                )
            ).all()
        }
        for item in answers:
            q = questions.get(str(item["question_id"]))
            if q is None:
                continue
            response = str(item.get("response", ""))
            llm_score = None
            scored_reason = None
            if q.type in ("single", "multi", "blank"):
                correct = grade_objective(
                    {"type": q.type, "answer": q.answer}, response
                )
                weight = 1.0
            else:  # short: LLM 评分, ≥0.6 记 correct, 0.5 权重 (§5.6)
                result = await gateway.chat(
                    GwRequest(
                        messages=(
                            GwMessage(
                                role="user",
                                content=(
                                    "你是评分助手。对照参考答案给学生作答打分。\n"
                                    f"题目: {q.stem}\n参考答案: {q.answer}\n"
                                    f"学生作答: {response}\n"
                                    '只输出 JSON: {"score": 0到1的小数, "reason": "评分理由"}'
                                ),
                            ),
                        ),
                        tier=Tier.standard,
                        json_mode=True,
                        feature="quiz_grade",
                        tenant_id=str(ctx.tenant_id),
                        user_id=str(ctx.user_id),
                    )
                )
                import orjson

                try:
                    parsed = orjson.loads(result.text)
                    llm_score = max(0.0, min(1.0, float(parsed.get("score", 0))))
                    scored_reason = str(parsed.get("reason", ""))[:2000]
                except Exception:  # noqa: BLE001 — 评分解析失败按 0 分并如实记录
                    llm_score = 0.0
                    scored_reason = "评分解析失败, 已按 0 分处理, 请人工复核"
                correct = llm_score >= _SHORT_PASS_SCORE
                weight = _SHORT_ANSWER_WEIGHT
            attempt = Attempt(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                question_id=q.id,
                response=response,
                is_correct=correct,
                llm_score=llm_score,
                scored_reason=scored_reason,
            )
            session.add(attempt)
            await session.flush()
            kc_ids = [str(k) for k in (q.kc_ids or [])]
            changes = await _update_mastery(
                session, ctx, kc_ids, correct, weight, attempt.id
            )
            if not correct:
                # F6-A1: 错题 100% 自动入本 (错因初值 knowledge_gap, M4 错因分类细化)
                # F6-A4: 入本即挂 FSRS 卡 (due=业务时间, 当天进复习队列)
                session.add(
                    ErrorItem(
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        question_id=q.id,
                        kc_ids=kc_ids,
                        fsrs_card_json=new_card_json(business_now),
                    )
                )
            results.append(
                {
                    "question_id": str(q.id),
                    "correct": correct,
                    "llm_score": llm_score,
                    "scored_reason": scored_reason,
                    "explanation": q.explanation,
                    "answer": q.answer,
                    "mastery_changes": changes,
                }
            )
    total = len(results)
    n_correct = sum(1 for r in results if r["correct"])
    # F3-A3 联动 (尾部追加, 判分主流程一行未改): 本次正确率 <0.6 且存在活跃计划时,
    # 自动按当前学情插入补强任务并写 plan_adjustments; 失败只记日志, 不影响判分返回。
    from .plan_adjust import LOW_SCORE_THRESHOLD, adjust_on_low_score

    plan_adjustment = None
    accuracy = (n_correct / total) if total else 1.0
    if total and accuracy < LOW_SCORE_THRESHOLD:
        plan_adjustment = await adjust_on_low_score(ctx, accuracy)
    return {
        "total": total,
        "correct": n_correct,
        "results": results,
        "plan_adjustment": plan_adjustment,  # 新增键, 老调用方忽略即可
    }
