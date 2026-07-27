"""F7 学情分析报告数据面。

- radar: 按活动量 top12 KC 的掌握度 (F7-A1: 与作答记录同源可核对)。
- trend: 近 30 天每日答题数与正确率 (attempts 聚合, 无数据日补零)。
- weak_points: p<0.4 的 KC + 相关错题数 + 归因下钻 (沿 kc_edges 找未掌握前置)。
- actions: 规则生成的可执行建议, 必须引用具体 KC 名/数量 (F7-A2/A3, 禁止空话)。
- narrative: standard 档 LLM 三段式诊断; stub 网关下如实返回 null (§14 禁止桩文本冒充)。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from eduagents_core.config import Settings
from eduagents_core.gateway import ChatMessage as GwMessage
from eduagents_core.gateway import ChatRequest as GwRequest
from eduagents_core.gateway import GatewayError, Tier
from eduagents_core.mastery.thresholds import QUIZ_WEAK_THRESHOLD, REINFORCE_THRESHOLD
from fastapi import APIRouter, Depends, Request
from sqlalchemy import Date, cast, func, select

from ..budget import CN_TZ
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings, get_gateway
from ..kc_hierarchy import FALLBACK_CHAPTER, chapter_sort_key, resolve_kc_meta
from ..models import Attempt, ErrorItem, KcEdge, KnowledgeComponent, MasteryLog, MasteryRecord

router = APIRouter(prefix="/api/reports", tags=["reports"])

_TREND_DAYS = 30
_RADAR_TOP_N = 12
# 报告薄弱红线与计划补强同源 (thresholds 唯一来源, G6): 两处判定不许漂移
_WEAK_THRESHOLD = REINFORCE_THRESHOLD
# C4: "待巩固"档 = 红线以上但未达定向出题线 (与 quiz 薄弱点圈选同源)
_CONSOLIDATE_THRESHOLD = QUIZ_WEAK_THRESHOLD


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

    # radar: 按活动量 (mastery_log 条数 = 证据事件数) top 12
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
    buckets: dict[str, dict] = {}
    for r, name in scoped:
        info = meta.get(str(r.kc_id)) or {}
        if kb_id:
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
    axes_kind = "chapter" if kb_id else "kb"
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
    radar.sort(key=lambda a: chapter_sort_key(a["name"]) if kb_id else (0, 0, a["name"]))

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
                .where(Attempt.user_id == ctx.user_id, Attempt.at >= since)
                .group_by(day_col)
            )
        ).all()
    }
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
    entries = []
    for kc in sorted(weak_ids + consolidate_ids, key=lambda k: p_by_kc[k]):
        error_count = int(
            await session.scalar(
                select(func.count()).where(
                    ErrorItem.user_id == ctx.user_id, ErrorItem.kc_ids.contains([kc])
                )
            )
            or 0
        )
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
                ErrorItem.user_id == ctx.user_id, ErrorItem.state.in_(["new", "reviewing"])
            )
        )
        or 0
    )
    return {
        "radar": radar,
        "radar_kcs": radar_kcs,
        "axes_kind": axes_kind,
        "trend": trend,
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
    kb_id: str | None = None,
    ctx: AuthContext = Depends(api_rate_limited),
    settings: Settings = Depends(get_app_settings),
    gateway=Depends(get_gateway),
):
    """学情报告 (F7)。kb_id 非空 = 单库视图: 雷达轴为该库各章, 薄弱点限该库。"""
    async with tenant_session(ctx.tenant_id) as session:
        agg = await _build_aggregates(
            session, ctx, kb_id=kb_id, redis=getattr(request.app.state, "redis", None)
        )
    actions = _build_actions(agg)
    narrative, note = await _narrative(
        gateway, settings, ctx, getattr(request.state, "request_id", ""), agg, actions
    )
    return {
        # A2: 聚合轴 (章/库), 每轴带 kc_count/weak_count 与下钻 kcs
        "radar": agg["radar"],
        "axes_kind": agg["axes_kind"],
        # 旧 top12 KC 轴保留一版, 供未升级的消费方降级
        "radar_kcs": agg["radar_kcs"],
        "trend": agg["trend"],
        "weak_points": agg["weak_points"],
        # C4: 两档阈值分开命名下发, 消除"薄弱"同词双口径
        "weak_threshold": _WEAK_THRESHOLD,
        "consolidate_threshold": _CONSOLIDATE_THRESHOLD,
        "consolidate_points": agg["consolidate_points"],
        "actions": actions,
        "narrative": narrative,
        "narrative_note": note,
    }
