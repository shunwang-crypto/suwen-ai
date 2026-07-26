from __future__ import annotations

import uuid
from datetime import UTC, datetime

from eduagents_core.gateway import ChatMessage as GwMessage
from eduagents_core.gateway import ChatRequest as GwRequest
from eduagents_core.gateway import Tier
from eduagents_core.logging import get_logger
from eduagents_core.mastery.bkt import BktParams, bkt_update
from sqlalchemy import delete, select

from .db import tenant_session
from .deps import AuthContext
from .models import (
    Attempt,
    ErrorItem,
    KnowledgeComponent,
    LearnerInsight,
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

INSIGHT_TEXT_MAX = 600  # learner_insights.text 截断上限 (学生原话/误解描述)
INSIGHTS_KEEP_PER_USER = 100  # 长期记忆每用户保留最近 100 条, 超出删最旧 (P2-1)


async def persist_learner_insights(
    session,
    ctx: AuthContext,
    rows: list[dict],
    *,
    source: str,
    session_id: str | None = None,
) -> int:
    """洞察条目入长期记忆表 (P2-1 唯一写入口, G10: 补上"他在什么语境下困惑")。

    rows: [{"kc_id": str|None, "kind": "confused"|"understood"|"misconception",
    "text": 学生原话或误解描述}]; text 统一截断 ≤ INSIGHT_TEXT_MAX, 空文本如实丢弃。
    写入后按 (created_at, id) 倒序裁剪, 每用户只保留最近 INSIGHTS_KEEP_PER_USER 条;
    返回裁掉的旧行数 (供日志)。
    幂等: reflection 侧由调用方复用 mastery_log 去重; 此外本函数自带内容级去重 ——
    同批次与保留窗口内已有相同 (kind, kc_id, text) 的条目跳过不插。记忆条目直接
    面向用户可见可删, summarize 轮被重试/重放时不得出现一字不差的重复行。
    """
    existing = {
        (kind, str(kc) if kc else None, text)
        for kind, kc, text in (
            await session.execute(
                select(LearnerInsight.kind, LearnerInsight.kc_id, LearnerInsight.text)
                .where(
                    LearnerInsight.user_id == ctx.user_id,
                    LearnerInsight.source == source,
                )
                .order_by(LearnerInsight.created_at.desc(), LearnerInsight.id.desc())
                .limit(INSIGHTS_KEEP_PER_USER)
            )
        ).all()
    }
    inserted = 0
    for row in rows:
        text_value = str(row.get("text") or "").strip()[:INSIGHT_TEXT_MAX]
        if not text_value:
            continue  # 无原话支撑的条目不入库 (与 match_insights 的丢弃口径一致)
        kc_id = row.get("kc_id")
        key = (row["kind"], str(kc_id) if kc_id else None, text_value)
        if key in existing:
            continue
        existing.add(key)
        session.add(
            LearnerInsight(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                kc_id=uuid.UUID(str(kc_id)) if kc_id else None,
                source=source,
                kind=row["kind"],
                text=text_value,
                session_id=uuid.UUID(str(session_id)) if session_id else None,
            )
        )
        inserted += 1
    if not inserted:
        return 0
    await session.flush()
    # 超额裁剪: 第 101 条起 (倒序 offset) 删除。同事务批次共享 now(), 以 id 兜底定序。
    stale_ids = (
        await session.scalars(
            select(LearnerInsight.id)
            .where(LearnerInsight.user_id == ctx.user_id)
            .order_by(LearnerInsight.created_at.desc(), LearnerInsight.id.desc())
            .offset(INSIGHTS_KEEP_PER_USER)
        )
    ).all()
    if stale_ids:
        await session.execute(
            delete(LearnerInsight).where(LearnerInsight.id.in_(stale_ids))
        )
    return len(stale_ids)


async def _resolve_kc_ids(
    session, kb_id: str | None, kc_names: list[str], user_id: uuid.UUID | None = None
) -> list[str]:
    """kc_name -> 归一化 kc_id (F5-A2: 禁止裸字符串)。无词表/未命中 -> 丢弃该名。

    词表口径: 绑库 = 该库全部 KC; 未绑库但有 user_id = 该用户有掌握度记录的 KC ——
    画像页「验证一下」与薄弱点出题都可以不绑库发起, 名称本就来自其掌握度记录,
    没有这条回退, 验证题会以 kc_ids=[] 落库, 判分永远写不动 BKT (重估空转)。
    """
    if not kc_names or (not kb_id and user_id is None):
        return []
    from eduagents_core.kc import KcEntry, KcVocabulary

    if kb_id:
        query = select(KnowledgeComponent).where(
            KnowledgeComponent.kb_id == uuid.UUID(kb_id)
        )
    else:
        query = (
            select(KnowledgeComponent)
            .join(MasteryRecord, MasteryRecord.kc_id == KnowledgeComponent.id)
            .where(MasteryRecord.user_id == user_id)
        )
    rows = (await session.scalars(query)).all()
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
            kc_ids = await _resolve_kc_ids(
                session, kb_id, q.get("kc_names", []), user_id=ctx.user_id
            )
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


def match_misconception_insights(
    misconceptions: list[str], vocab: list[tuple[str, str, list[str]]]
) -> tuple[list[str], dict[str, str], list[str | None]]:
    """误解文本批量匹配 KC 词表 (纯函数, 便于单测)。

    vocab: [(kc_id, canonical, aliases)] 按 created_at 序。返回三元组:
    - matched: 命中的 kc_id (词表序去重) — BKT 口径与旧内联实现逐位相同
      (KC canonical/alias 归一化后作为子串出现在任一误解文本中即命中);
    - name_by_id: kc_id -> canonical 名;
    - kc_by_text: 每条文本的首个命中 kc_id (词表序), 未命中为 None —
      供 learner_insights 落库时"能归一匹配 KC 就带 kc_id" (P2-1)。
    """
    from eduagents_core.kc.types import normalize_kc_name

    norm_texts = [normalize_kc_name(t) for t in misconceptions]
    matched: list[str] = []
    name_by_id: dict[str, str] = {}
    kc_by_text: list[str | None] = [None] * len(misconceptions)
    for kc_id, canonical, aliases in vocab:
        keys = [normalize_kc_name(canonical)] + [
            normalize_kc_name(a) for a in aliases if isinstance(a, str)
        ]
        keys = [k for k in keys if len(k) >= _MIN_KC_MATCH_LEN]
        hit = False
        for i, text_norm in enumerate(norm_texts):
            if any(k in text_norm for k in keys):
                hit = True
                if kc_by_text[i] is None:
                    kc_by_text[i] = kc_id
        if hit and kc_id not in matched:
            matched.append(kc_id)
            name_by_id[kc_id] = canonical
    return matched, name_by_id, kc_by_text


async def apply_tutor_summary(ctx: AuthContext, summary: dict) -> list[dict]:
    """辅导小结落画像 (F4→F8 联动): misconceptions 命中已有 KC 词表 → BKT 负向证据。

    匹配规则: KC canonical/alias 归一化后作为子串出现在误解文本中即命中;
    词表外的误解不进 BKT、不新建 KC (画像外键只认归一化 kc_id, F5-A2)。
    命中 KC 以 correct=False、0.5 权重、evidence_type="tutor_summary" 进 BKT,
    返回变化列表 (附 KC 名, 供前端复盘卡展示)。

    P2-1 追加: 误解文本本身写入 learner_insights (source=tutor_summary,
    kind=misconception; 能归一匹配 KC 就带 kc_id, 匹配不上也如实保留原文) —
    掌握度数字之外留住"他到底错在哪"的语境 (G10)。
    """
    raw_texts = [
        m.strip()
        for m in (summary.get("misconceptions") or [])
        if isinstance(m, str) and m.strip()
    ]
    if not raw_texts:
        return []
    async with tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.scalars(
                select(KnowledgeComponent).order_by(KnowledgeComponent.created_at)
            )
        ).all()
        vocab = [(str(r.id), r.canonical_name, list(r.aliases or [])) for r in rows]
        matched, name_by_id, kc_by_text = match_misconception_insights(raw_texts, vocab)
        await persist_learner_insights(
            session,
            ctx,
            [
                {"kc_id": kc_by_text[i], "kind": "misconception", "text": text}
                for i, text in enumerate(raw_texts)
            ],
            source="tutor_summary",
        )
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
