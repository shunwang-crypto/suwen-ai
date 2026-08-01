"""F7 学情分析报告数据面。

- radar: 全局按知识库聚合；传 kb_id 时按章节聚合，并附 KC 下钻明细。
  单库用户的全局视图自动下钻为章节轴 (一根库轴的"光杆图"无信息量), axes_kind 如实标注。
- radar_kcs: 按活动量 top12 KC 的兼容字段 (F7-A1: 与作答记录同源可核对)。
- trend: 近 30 天每日答题数与正确率 (attempts 聚合, 无数据日补零)。
- weak_points: 低于补强红线 (REINFORCE_THRESHOLD) 的 KC + 相关错题数 + 归因下钻
  (沿 kc_edges 找未掌握前置); consolidate_points 为"待巩固"档 (红线以上、未达定向
  出题线 QUIZ_WEAK_THRESHOLD), 两档同结构分开下发, 消除"薄弱"同词双口径 (C4)。
- actions: 规则生成的可执行建议, 必须引用具体 KC 名/数量 (F7-A2/A3, 禁止空话)。
- narrative: standard 档 LLM 三段式诊断; stub 网关下如实返回 null (§14 禁止桩文本冒充)。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timedelta

from eduagents_core.config import Settings
from eduagents_core.gateway import ChatMessage as GwMessage
from eduagents_core.gateway import ChatRequest as GwRequest
from eduagents_core.gateway import GatewayError, Tier
from eduagents_core.logging import get_logger
from eduagents_core.mastery.thresholds import QUIZ_WEAK_THRESHOLD, REINFORCE_THRESHOLD
from fastapi import APIRouter, Depends, Request
from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..budget import CN_TZ
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..kc_hierarchy import FALLBACK_CHAPTER, chapter_sort_key, resolve_kc_meta
from ..models import (
    ERROR_IMPORT_QUIZ_KIND,
    AssessmentAnswer,
    AssessmentAttempt,
    Attempt,
    ErrorItem,
    KcEdge,
    KnowledgeComponent,
    LearningReportSnapshot,
    MasteryLog,
    MasteryRecord,
    Question,
    Quiz,
)

router = APIRouter(prefix="/api/reports", tags=["reports"])
log = get_logger("reports")

_TREND_DAYS = 30
_RADAR_TOP_N = 12
# 报告薄弱红线与计划补强同源 (thresholds 唯一来源, G6): 两处判定不许漂移
_WEAK_THRESHOLD = REINFORCE_THRESHOLD
# C4: "待巩固"档 = 红线以上但未达定向出题线 (与 quiz 薄弱点圈选同源)
_CONSOLIDATE_THRESHOLD = QUIZ_WEAK_THRESHOLD


def _ordinary_attempt_filter():
    kind = Quiz.blueprint_json["kind"].astext
    return or_(kind.is_(None), kind != ERROR_IMPORT_QUIZ_KIND)


async def _build_aggregates(
    session, ctx: AuthContext, kb_id: str | None = None, redis=None
) -> dict:
    # 掌握度全量 + KC 名 (同 profile 口径, F7-A1 可核对)
    mastery_rows = (
        await session.execute(
            select(MasteryRecord, KnowledgeComponent.canonical_name)
            .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
            .where(MasteryRecord.user_id == ctx.user_id)
        )
    ).all()
    p_by_kc = {str(r.kc_id): r.p_mastery for r, _ in mastery_rows}
    name_by_kc = {str(r.kc_id): name for r, name in mastery_rows}

    # 兼容轴: 按活动量 (mastery_log 条数 = 证据事件数) top 12
    activity = {
        str(kc_id): int(n)
        for kc_id, n in (
            await session.execute(
                select(MasteryLog.kc_id, func.count())
                .where(MasteryLog.user_id == ctx.user_id)
                .group_by(MasteryLog.kc_id)
            )
        ).all()
    }
    ranked = sorted(
        mastery_rows, key=lambda pair: activity.get(str(pair[0].kc_id), 0), reverse=True
    )[:_RADAR_TOP_N]
    radar_kcs = [
        {
            "kc_id": str(r.kc_id),
            "name": name,
            "p_mastery": round(r.p_mastery, 4),
            "activity": activity.get(str(r.kc_id), 0),
        }
        for r, name in ranked
    ]

    # A2 雷达语义修复: 单 KC 当轴在真实规模 (3 库 × 152 KC) 下语义错误 —— 异构 KC
    # 混轴、轴集合随掌握度漂移不可跨时间对比。改为聚合轴:
    #   传 kb_id -> 轴 = 该库各章 (章内 KC 按证据数加权平均, 无证据按等权);
    #   不传     -> 轴 = 各知识库 (库级聚合)。
    # 章序按章号排 (中文数字), 保证轴序稳定; 旧 top12 结构保留在 radar_kcs 兼容。
    meta = await resolve_kc_meta(
        ctx.tenant_id, [str(r.kc_id) for r, _ in mastery_rows], redis=redis
    )
    scoped = [
        (r, name)
        for r, name in mastery_rows
        if not kb_id or (meta.get(str(r.kc_id)) or {}).get("kb_id") == kb_id
    ]
    # 单库用户的全局视图: 库轴只有一根 = 一根光杆图, 没有可比性也没有信息量。
    # 此时自动下钻到那唯一一个库的章节轴 (数据同源, 只是换个聚合粒度)。
    effective_kb_id = kb_id
    if not kb_id:
        distinct_kbs = {
            (meta.get(str(r.kc_id)) or {}).get("kb_id")
            for r, _ in scoped
            if (meta.get(str(r.kc_id)) or {}).get("kb_id")
        }
        if len(distinct_kbs) == 1:
            effective_kb_id = next(iter(distinct_kbs))
    buckets: dict[str, dict] = {}
    for r, name in scoped:
        info = meta.get(str(r.kc_id)) or {}
        if effective_kb_id:
            axis_key = info.get("chapter") or FALLBACK_CHAPTER
        else:
            axis_key = info.get("kb_name") or "未归库"
        bucket = buckets.setdefault(
            axis_key,
            {"name": axis_key, "kb_id": info.get("kb_id"), "num": 0.0, "den": 0.0, "kcs": []},
        )
        # 加权平均: 证据越多的 KC 对章掌握度贡献越大 (无证据仍按 1 计, 不被抹掉)
        weight = float(activity.get(str(r.kc_id), 0)) + 1.0
        bucket["num"] += r.p_mastery * weight
        bucket["den"] += weight
        bucket["kcs"].append(
            {"kc_id": str(r.kc_id), "name": name, "p_mastery": round(r.p_mastery, 4)}
        )
    axes_kind = "chapter" if effective_kb_id else "kb"
    radar = [
        {
            "key": key,
            "name": b["name"],
            "kb_id": b["kb_id"],
            "p_mastery": round(b["num"] / b["den"], 4) if b["den"] else 0.0,
            "kc_count": len(b["kcs"]),
            "weak_count": sum(1 for k in b["kcs"] if k["p_mastery"] < _CONSOLIDATE_THRESHOLD),
            "kcs": sorted(b["kcs"], key=lambda k: k["p_mastery"]),
        }
        for key, b in buckets.items()
    ]
    radar.sort(key=lambda a: chapter_sort_key(a["name"]) if effective_kb_id else (0, 0, a["name"]))

    # trend: 近 30 天每日答题数与正确率, 无数据日补零 (前端折线连续)。
    # P2-11 唯一决定: 日切在 SQL 侧 AT TIME ZONE 'Asia/Shanghai' (timestamptz ->
    # 沪时本地时间再取日期), 与预算日界 (budget.py CN_TZ) 同一口径 —— 北京时间
    # 凌晨刷题计入"今天", 打卡/趋势不再因 UTC 日切断签。
    today = datetime.now(CN_TZ).date()
    since = datetime(today.year, today.month, today.day, tzinfo=CN_TZ) - timedelta(
        days=_TREND_DAYS - 1
    )
    day_col = cast(func.timezone("Asia/Shanghai", Attempt.at), Date)
    daily = {
        day: (int(total), int(correct))
        for day, total, correct in (
            await session.execute(
                select(
                    day_col.label("day"),
                    func.count(),
                    func.count().filter(Attempt.is_correct.is_(True)),
                )
                .select_from(Attempt)
                .join(Question, Question.id == Attempt.question_id)
                .join(Quiz, Quiz.id == Question.quiz_id)
                .where(
                    Attempt.user_id == ctx.user_id,
                    Attempt.at >= since,
                    _ordinary_attempt_filter(),
                )
                .group_by(day_col)
            )
        ).all()
    }
    assessment_day_col = cast(func.timezone("Asia/Shanghai", AssessmentAnswer.created_at), Date)
    assessment_daily = {
        day: (int(total), int(correct))
        for day, total, correct in (
            await session.execute(
                select(
                    assessment_day_col.label("day"),
                    func.count(),
                    func.count().filter(AssessmentAnswer.is_correct.is_(True)),
                )
                .select_from(AssessmentAnswer)
                .join(AssessmentAttempt, AssessmentAttempt.id == AssessmentAnswer.attempt_id)
                .where(
                    AssessmentAttempt.student_id == ctx.user_id,
                    AssessmentAttempt.status == "graded",
                    AssessmentAnswer.grading_status == "verified",
                    AssessmentAnswer.created_at >= since,
                )
                .group_by(assessment_day_col)
            )
        ).all()
    }
    for day, (total, correct) in assessment_daily.items():
        old_total, old_correct = daily.get(day, (0, 0))
        daily[day] = (old_total + total, old_correct + correct)
    trend = []
    for i in range(_TREND_DAYS):
        d = today - timedelta(days=_TREND_DAYS - 1 - i)
        total, correct = daily.get(d, (0, 0))
        trend.append(
            {
                "date": d.isoformat(),
                "attempts": total,
                "correct": correct,
                "accuracy": round(correct / total, 4) if total else None,
            }
        )

    # Figma 报告新增的可核对指标：只使用真实 Attempt / MasteryLog 证据。
    # 仓库当前没有可靠的前台停留时长，因此明确返回作答次数，不用次数推算“学习小时”。
    current_attempts = sum(item[0] for item in daily.values())
    current_correct = sum(item[1] for item in daily.values())
    active_days = sum(1 for total, _ in daily.values() if total > 0)
    previous_since = since - timedelta(days=_TREND_DAYS)
    previous_row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Attempt.is_correct.is_(True)),
                func.count(func.distinct(day_col)),
            )
            .select_from(Attempt)
            .join(Question, Question.id == Attempt.question_id)
            .join(Quiz, Quiz.id == Question.quiz_id)
            .where(
                Attempt.user_id == ctx.user_id,
                Attempt.at >= previous_since,
                Attempt.at < since,
                _ordinary_attempt_filter(),
            )
        )
    ).one()
    previous_attempts = int(previous_row[0] or 0)
    previous_correct = int(previous_row[1] or 0)
    previous_active_days = int(previous_row[2] or 0)
    previous_assessment = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(AssessmentAnswer.is_correct.is_(True)),
                func.count(func.distinct(assessment_day_col)),
            )
            .select_from(AssessmentAnswer)
            .join(AssessmentAttempt, AssessmentAttempt.id == AssessmentAnswer.attempt_id)
            .where(
                AssessmentAttempt.student_id == ctx.user_id,
                AssessmentAttempt.status == "graded",
                AssessmentAnswer.grading_status == "verified",
                AssessmentAnswer.created_at >= previous_since,
                AssessmentAnswer.created_at < since,
            )
        )
    ).one()
    previous_attempts += int(previous_assessment[0] or 0)
    previous_correct += int(previous_assessment[1] or 0)
    previous_active_days = max(previous_active_days, int(previous_assessment[2] or 0))

    scoped_kc_ids = [r.kc_id for r, _ in scoped]
    mastery_log_query = select(MasteryLog).where(
        MasteryLog.user_id == ctx.user_id,
        MasteryLog.at >= since,
    )
    if kb_id:
        mastery_log_query = mastery_log_query.where(MasteryLog.kc_id.in_(scoped_kc_ids))
    recent_mastery_logs = (await session.scalars(mastery_log_query)).all()
    newly_mastered = {
        str(item.kc_id)
        for item in recent_mastery_logs
        if item.old_p < _CONSOLIDATE_THRESHOLD <= item.new_p
    }
    slipped = {
        str(item.kc_id)
        for item in recent_mastery_logs
        if item.old_p >= _CONSOLIDATE_THRESHOLD > item.new_p
    }
    current_accuracy = current_correct / current_attempts if current_attempts else None
    previous_accuracy = previous_correct / previous_attempts if previous_attempts else None
    metrics = {
        "attempt_count": current_attempts,
        "active_days": active_days,
        "accuracy": round(current_accuracy, 4) if current_accuracy is not None else None,
        "newly_mastered_count": len(newly_mastered),
        "slipped_count": len(slipped),
        "attempt_count_delta": current_attempts - previous_attempts,
        "active_days_delta": active_days - previous_active_days,
        "accuracy_delta": (
            round(current_accuracy - previous_accuracy, 4)
            if current_accuracy is not None and previous_accuracy is not None
            else None
        ),
    }

    # weak_points: p<红线, 附相关错题数 + 归因下钻 (kc_edges: from_kc 是 to_kc 的前置)
    weak_ids = [
        kc
        for kc, p in p_by_kc.items()
        if p < _WEAK_THRESHOLD and (not kb_id or (meta.get(kc) or {}).get("kb_id") == kb_id)
    ]
    prereqs: dict[str, list[str]] = {}
    if weak_ids:
        edges = (
            await session.scalars(
                select(KcEdge).where(
                    KcEdge.to_kc.in_([uuid.UUID(k) for k in weak_ids]),
                    KcEdge.type == "prerequisite",
                )
            )
        ).all()
        for e in edges:
            prereqs.setdefault(str(e.to_kc), []).append(str(e.from_kc))
    # C4: "薄弱"曾同词双阈值 (报告 0.4 / 出题 0.6) 导致同一用户看到两个数字。
    # 分层命名: 红线 = 低于补强阈值; 待巩固 = 红线以上但未达定向出题线。两档同结构
    # 下发, weak_points 保持"红线"旧语义 (actions/narrative 消费方口径不变)。
    consolidate_ids = [
        kc
        for kc, p in p_by_kc.items()
        if _WEAK_THRESHOLD <= p < _CONSOLIDATE_THRESHOLD
        and (not kb_id or (meta.get(kc) or {}).get("kb_id") == kb_id)
    ]
    # 真实规模下逐 KC COUNT 会退化成 N+1（152 KC 就可能多出上百次往返）。错题本
    # 本来就是用户级小集合，一次取回 kc_ids 后在内存聚合，查询数恒定为 1。
    target_ids = set(weak_ids + consolidate_ids)
    error_counts: Counter[str] = Counter()
    if target_ids:
        error_kc_rows = (
            await session.scalars(
                select(ErrorItem.kc_ids).where(
                    ErrorItem.user_id == ctx.user_id,
                    ErrorItem.verification_status == "verified",
                )
            )
        ).all()
        for row_ids in error_kc_rows:
            for row_kc in {str(value) for value in (row_ids or [])} & target_ids:
                error_counts[row_kc] += 1

    entries = []
    for kc in sorted(weak_ids + consolidate_ids, key=lambda k: p_by_kc[k]):
        error_count = int(error_counts[kc])
        # 前置里同样未掌握 (p<0.4) 的列为可能根因; 无掌握记录的前置不妄断
        root_cause = [
            {"kc_id": pre, "name": name_by_kc.get(pre, ""), "p_mastery": round(p_by_kc[pre], 4)}
            for pre in prereqs.get(kc, [])
            if pre in p_by_kc and p_by_kc[pre] < _WEAK_THRESHOLD
        ]
        entries.append(
            {
                "tier": "red" if p_by_kc[kc] < _WEAK_THRESHOLD else "consolidate",
                "kc_id": kc,
                "name": name_by_kc.get(kc, ""),
                "p_mastery": round(p_by_kc[kc], 4),
                "error_count": error_count,
                "possible_root_cause": root_cause,
                # B5: 按科目分节展示 (真实规模下 17+ 张卡平铺不可读)
                "subject": (meta.get(kc) or {}).get("subject") or "",
                "kb_id": (meta.get(kc) or {}).get("kb_id"),
                "kb_name": (meta.get(kc) or {}).get("kb_name") or "",
                "chapter": (meta.get(kc) or {}).get("chapter") or FALLBACK_CHAPTER,
            }
        )

    weak_points = [e for e in entries if e["tier"] == "red"]
    consolidate_points = [e for e in entries if e["tier"] == "consolidate"]

    # 未归档错题总数 (actions 用)
    open_errors = int(
        await session.scalar(
            select(func.count()).where(
                ErrorItem.user_id == ctx.user_id,
                ErrorItem.state.in_(["new", "reviewing"]),
                ErrorItem.verification_status == "verified",
            )
        )
        or 0
    )
    return {
        "radar": radar,
        "radar_kcs": radar_kcs,
        "axes_kind": axes_kind,
        "trend": trend,
        "metrics": metrics,
        "weak_points": weak_points,
        "consolidate_points": consolidate_points,
        "open_errors": open_errors,
    }


def _build_actions(agg: dict) -> list[dict]:
    """规则生成 ≥2 条可执行建议, 每条带跳转; 禁止空话: 必须引用具体 KC 名或数量。"""
    actions: list[dict] = []
    if agg["open_errors"] > 0:
        actions.append(
            {
                "type": "review",
                "label": f"复习错题本 {agg['open_errors']} 题",
                "href": "/reviews",
            }
        )
    weak = agg["weak_points"]
    if weak:
        w = weak[0]  # 最薄弱
        actions.append(
            {
                "type": "quiz",
                "label": f"针对「{w['name']}」出一组练习 (当前掌握度 {w['p_mastery']:.0%})",
                "href": f"/quiz?kc={w['kc_id']}",
            }
        )
        for wp in weak:
            if wp["possible_root_cause"]:
                rc = wp["possible_root_cause"][0]
                actions.append(
                    {
                        "type": "quiz",
                        "label": (
                            f"「{wp['name']}」薄弱可能源于前置「{rc['name']}」"
                            f"未掌握 ({rc['p_mastery']:.0%}), 先补前置"
                        ),
                        "href": f"/quiz?kc={rc['kc_id']}",
                    }
                )
                break
    # 冷启动兜底: 无错题也无薄弱点时仍保证 ≥2 条, 且带具体数量
    if len(actions) < 2:
        fallbacks = [
            {"type": "quiz", "label": "完成 5-8 题摸底测评, 建立掌握度画像", "href": "/quiz"},
            {"type": "plan", "label": "制定 1 份学习计划 (含摸底诊断)", "href": "/plans"},
        ]
        for fb in fallbacks:
            if len(actions) >= 2:
                break
            actions.append(fb)
    return actions


_REPORT_PROMPT_VERSION = "v2"


def _report_payload(agg: dict, actions: list[dict]) -> dict:
    """可持久化的确定性报告。原始证据仍在业务表，快照只负责即时展示。"""
    return {
        "radar": agg["radar"],
        "axes_kind": agg["axes_kind"],
        "radar_kcs": agg["radar_kcs"],
        "trend": agg["trend"],
        "metrics": agg["metrics"],
        "weak_points": agg["weak_points"],
        "weak_threshold": _WEAK_THRESHOLD,
        "consolidate_threshold": _CONSOLIDATE_THRESHOLD,
        "consolidate_points": agg["consolidate_points"],
        "open_errors": agg["open_errors"],
        "actions": actions,
    }


def _payload_signature(payload: dict) -> str:
    stable = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _snapshot_response(snapshot: LearningReportSnapshot, *, cached: bool) -> dict:
    return {
        **(snapshot.payload_json or {}),
        "narrative": snapshot.narrative,
        "narrative_note": snapshot.narrative_note,
        "report_status": snapshot.status,
        "report_updated_at": snapshot.updated_at.isoformat(),
        "cached": cached,
    }


def _scope(kb_id: uuid.UUID | None) -> tuple[str, str | None]:
    value = str(kb_id) if kb_id is not None else None
    return value or "all", value


async def _narrative(
    gateway, settings: Settings, ctx: AuthContext, request_id: str, agg: dict, actions: list[dict]
) -> tuple[str | None, str | None]:
    """standard 档三段式诊断。stub/replay 命中桩时如实返回 null, 不冒充 (§14)。"""
    if settings.gateway_mode == "stub":
        return None, "需 live 网关生成"
    payload = {
        "radar": agg["radar"],
        "trend_recent_7d": agg["trend"][-7:],
        "weak_points": agg["weak_points"],
        "open_errors": agg["open_errors"],
        "actions": [a["label"] for a in actions],
    }
    prompt = (
        "你是学情分析师。仅基于下面的 JSON 聚合数据, 输出三段式诊断, 三段分别以"
        "『现状』『归因』『建议』开头。每段必须引用具体知识点名称与数字 (掌握度百分比/"
        "错题数/正确率), 禁止出现没有具体知识点的空话 (如泛泛的『多加练习』)。"
        "归因段优先引用 possible_root_cause 的前置知识点链条。\n"
        "写作要求: 直接以学情结论开口, 严禁出现『基于您提供的数据』『根据 JSON』等"
        "提示词口吻; 百分比一律取整 (写 82% 不写 81.64%); 末尾以 2-3 条可执行行动"
        "清单收尾, 每条给出具体知识点与动作。\n数据:\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )
    try:
        result = await gateway.chat(
            GwRequest(
                messages=(GwMessage(role="user", content=prompt),),
                tier=Tier.standard,
                temperature=0.3,
                max_tokens=800,
                feature="report_narrative",
                tenant_id=str(ctx.tenant_id),
                user_id=str(ctx.user_id),
                request_id=request_id,
            )
        )
    except GatewayError as e:
        return None, f"网关调用失败, 未生成诊断: {e.code}"
    if result.mode == "stub":  # replay 未命中回落 stub 时同样不冒充
        return None, "需 live 网关生成"
    return result.text, None


@router.get("/learning")
async def learning_report(
    request: Request,
    kb_id: uuid.UUID | None = None,
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    """立即返回最近快照；首次访问只做确定性聚合，不在页面首屏等待 LLM。"""
    scope_key, scoped_kb_id = _scope(kb_id)
    async with tenant_session(ctx.tenant_id) as session:
        snapshot = await session.scalar(
            select(LearningReportSnapshot).where(
                LearningReportSnapshot.user_id == ctx.user_id,
                LearningReportSnapshot.scope_key == scope_key,
            )
        )
        if snapshot is not None:
            return _snapshot_response(snapshot, cached=True)

        agg = await _build_aggregates(
            session,
            ctx,
            kb_id=scoped_kb_id,
            redis=getattr(request.app.state, "redis", None),
        )
        actions = _build_actions(agg)
        payload = _report_payload(agg, actions)
        unavailable = settings.gateway_mode == "stub"
        # React 开发模式会主动双跑 effect；数据库唯一约束 + ON CONFLICT 让首次
        # 快照创建也保持幂等，两个并发 GET 不会有一个变成 500。
        snapshot = await session.scalar(
            pg_insert(LearningReportSnapshot)
            .values(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                scope_key=scope_key,
                kb_id=kb_id,
                payload_json=payload,
                narrative=None,
                narrative_note=("需 live 网关生成" if unavailable else "正在生成最新 AI 诊断"),
                status="unavailable" if unavailable else "pending",
                data_signature=_payload_signature(payload),
                model=settings.standard_model,
                prompt_version=_REPORT_PROMPT_VERSION,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    LearningReportSnapshot.user_id,
                    LearningReportSnapshot.scope_key,
                ]
            )
            .returning(LearningReportSnapshot)
        )
        if snapshot is None:
            snapshot = await session.scalar(
                select(LearningReportSnapshot).where(
                    LearningReportSnapshot.user_id == ctx.user_id,
                    LearningReportSnapshot.scope_key == scope_key,
                )
            )
        if snapshot is None:  # pragma: no cover — DB 冲突行理论上必须可见
            raise RuntimeError("learning report snapshot upsert failed")
        return _snapshot_response(snapshot, cached=False)


@router.post("/learning/refresh")
async def refresh_learning_report(
    request: Request,
    kb_id: uuid.UUID | None = None,
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
):
    """刷新确定性快照并将 AI 诊断入队；页面离开不取消后台生成。"""
    scope_key, scoped_kb_id = _scope(kb_id)
    async with tenant_session(ctx.tenant_id) as session:
        agg = await _build_aggregates(
            session,
            ctx,
            kb_id=scoped_kb_id,
            redis=getattr(request.app.state, "redis", None),
        )
        actions = _build_actions(agg)
        payload = _report_payload(agg, actions)
        signature = _payload_signature(payload)
        snapshot = await session.scalar(
            select(LearningReportSnapshot).where(
                LearningReportSnapshot.user_id == ctx.user_id,
                LearningReportSnapshot.scope_key == scope_key,
            ).with_for_update()
        )
        if (
            snapshot is not None
            and snapshot.data_signature == signature
            and snapshot.prompt_version == _REPORT_PROMPT_VERSION
            and snapshot.status in {"completed", "unavailable"}
        ):
            return _snapshot_response(snapshot, cached=True)
        if (
            snapshot is not None
            and snapshot.data_signature == signature
            and snapshot.prompt_version == _REPORT_PROMPT_VERSION
            and snapshot.status == "generating"
        ):
            return _snapshot_response(snapshot, cached=True)
        unavailable = settings.gateway_mode == "stub"
        if snapshot is None:
            snapshot = LearningReportSnapshot(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                scope_key=scope_key,
                kb_id=kb_id,
                payload_json=payload,
                narrative_note=("需 live 网关生成" if unavailable else "正在生成最新 AI 诊断"),
                status="unavailable" if unavailable else "generating",
                data_signature=signature,
                model=settings.standard_model,
                prompt_version=_REPORT_PROMPT_VERSION,
            )
            session.add(snapshot)
        else:
            snapshot.payload_json = payload
            snapshot.data_signature = signature
            snapshot.status = "unavailable" if unavailable else "generating"
            snapshot.narrative_note = (
                "需 live 网关生成" if unavailable else "正在生成最新 AI 诊断"
            )
            snapshot.model = settings.standard_model
            snapshot.prompt_version = _REPORT_PROMPT_VERSION
        await session.flush()
        snapshot_id = snapshot.id
        await session.refresh(snapshot)
        response = _snapshot_response(snapshot, cached=False)

    if unavailable:
        return response

    job_id = f"learning-report:{snapshot_id}:{uuid.uuid4().hex}"
    try:
        job = await request.app.state.arq.enqueue_job(
            "refresh_learning_report",
            str(ctx.tenant_id),
            str(ctx.user_id),
            str(snapshot_id),
            signature,
            getattr(request.state, "request_id", ""),
            _job_id=job_id,
        )
        if job is None:
            raise RuntimeError("learning report job was not enqueued")
    except Exception:  # noqa: BLE001 — 确定性报告仍可展示，只将 AI 诊断收口为失败
        log.exception("report_refresh_enqueue_failed", user_id=str(ctx.user_id), scope=scope_key)
        async with tenant_session(ctx.tenant_id) as session:
            snapshot = await session.get(LearningReportSnapshot, snapshot_id)
            if snapshot is not None and snapshot.data_signature == signature:
                snapshot.status = "failed"
                snapshot.narrative_note = "后台更新暂不可用，当前报告数据仍可正常查看"
                await session.flush()
                await session.refresh(snapshot)
                response = _snapshot_response(snapshot, cached=False)
    return response
