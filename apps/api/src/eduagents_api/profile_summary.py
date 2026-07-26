"""画像读侧 (F8-A2 "新会话主动关联历史卡点" 的数据源)。

两个消费方共用本模块, 都只读不写、不经 LLM:
- 问答: build_profile_context -> ≤300 字中文摘要注入 qa 子图 system prompt;
  无数据时摘要为空串 (调用方据此判定"该用户尚无画像", 并据此保留答案缓存)。
- 出题: weak_point_kcs -> 定向薄弱点 KC 名 (p<0.6, 近 7 天有错题者优先, p 升序 top5)。

脱敏约定 (P2-7, 个保法口径): 摘要会随每次问答发往第三方 LLM, 且经 trace 事件
进前端 —— 掌握度概率是"可推断学习能力"的敏感个人信息, 错题原文可能含隐私。
因此对外出参只允许**抽象标签级**:
- 薄弱点只给 KC 名 + 两档定性词 ("较弱"/"待巩固"), 不给精确掌握度小数;
- 错因只给五分类类别名, 不给任何题干/作答原文;
- 结构化 weak_points/mastered 只传 KC 名列表 (trace 同源, 无数值)。
精确数值仅供站内页面走 /api/profile 等鉴权接口获取, 不随摘要出境。

摘要走 Redis 60s 缓存 (key: profile_summary:{user_id}[:{kb 指纹}])。缓存故障一律
回源, 绝不阻塞主链路。错题"近期"口径按真实时间 (created_at 是 server_default now(),
与 §9 虚拟时钟无关; FSRS 的 due 才走 timectl)。
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import orjson
from eduagents_core.logging import get_logger

# 阈值唯一来源 (个性化波次 G6): 此处仅 import 再导出 (routers/profile.py 等既有
# 消费方沿用本模块入口), 禁止再落地本地硬编码副本。
from eduagents_core.mastery.thresholds import (
    MASTERED_THRESHOLD,
    QUIZ_WEAK_THRESHOLD,
    WEAK_THRESHOLD,
    difficulty_for_mastery,
    zone_label,
)
from sqlalchemy import select

from .db import tenant_session
from .models import ErrorItem, KnowledgeComponent, MasteryRecord

log = get_logger("profile_summary")

CACHE_TTL_SECONDS = 60
SUMMARY_MAX_CHARS = 300  # 摘要硬上限 (进 system prompt, 不能挤占资料位)

# 问答摘要口径 (定性两档边界见 thresholds.zone_label: p<0.3 "较弱", 其余薄弱段 "待巩固")
_WEAK_TOP = 5
_MASTERED_TOP = 3
_RECENT_ERROR_KCS = 3  # 摘要里点名的最近错题 KC 数
_ERROR_SCAN = 10  # 错因分布的近期错题扫描窗口

# 定向出题口径 (M4-D2: "上午答错的概念下午自动出现在练习卷")
QUIZ_WEAK_TOP = 5
_RECENT_ERROR_DAYS = 7

# 错因五分类中文名 (与 apps/web profile 页 CAUSE_LABELS 同源)
CAUSE_LABELS = {
    "concept_confusion": "概念混淆",
    "formula_error": "公式用错",
    "calc_slip": "计算失误",
    "misread": "审题偏差",
    "knowledge_gap": "知识缺口",
}


@dataclass(frozen=True)
class ProfileContext:
    """画像摘要 + 结构化薄弱点 (摘要进 prompt, 薄弱点进 trace 供前端可见)。"""

    summary: str = ""
    weak_points: list[str] = field(default_factory=list)
    mastered: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.summary


def _cache_key(user_id: str, kb_ids: list[str]) -> str:
    base = f"profile_summary:{user_id}"
    if not kb_ids:
        return base
    # 绑定知识库不同 -> 画像取材范围不同, 缓存必须分桶 (否则跨库串味)
    digest = hashlib.sha1("|".join(sorted(kb_ids)).encode()).hexdigest()[:8]
    return f"{base}:{digest}"


def _norm_ids(kb_ids) -> list[str]:
    return [str(k) for k in (kb_ids or []) if str(k)]


async def _load_snapshot(
    tenant_id: uuid.UUID | str, user_id: uuid.UUID | str, kb_ids: list[str]
) -> tuple[list[tuple[str, str, float]], list[str], Counter]:
    """一次事务读齐: (掌握度行, 最近错题 KC 名, 近期错因计数)。

    掌握度行形如 (kc_id, kc_name, p_mastery), 已按 p 升序。kb_ids 非空时按所属知识库
    过滤; 过滤后为空则回退全量 (跨库学习者不该因绑库而丢画像)。
    """
    uid = uuid.UUID(str(user_id))
    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                select(
                    MasteryRecord.kc_id,
                    MasteryRecord.p_mastery,
                    KnowledgeComponent.canonical_name,
                    KnowledgeComponent.kb_id,
                )
                .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
                .where(MasteryRecord.user_id == uid)
                .order_by(MasteryRecord.p_mastery.asc())
            )
        ).all()
        errors = (
            await session.scalars(
                select(ErrorItem)
                .where(ErrorItem.user_id == uid)
                .order_by(ErrorItem.created_at.desc())
                .limit(_ERROR_SCAN)
            )
        ).all()
        error_kc_ids: list[str] = []
        for item in errors[:_RECENT_ERROR_KCS]:
            for kc_id in item.kc_ids or []:
                if str(kc_id) not in error_kc_ids:
                    error_kc_ids.append(str(kc_id))
        name_by_id = {str(kc_id): name for kc_id, _p, name, _kb in rows}
        missing = [k for k in error_kc_ids if k not in name_by_id]
        if missing:
            for kc in (
                await session.scalars(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.id.in_([uuid.UUID(m) for m in missing])
                    )
                )
            ).all():
                name_by_id[str(kc.id)] = kc.canonical_name

    kb_set = set(kb_ids)
    scoped = [
        (str(kc_id), name, float(p))
        for kc_id, p, name, kb_id in rows
        if not kb_set or str(kb_id) in kb_set
    ]
    if not scoped:
        scoped = [(str(kc_id), name, float(p)) for kc_id, p, name, _kb in rows]
    error_names = [name_by_id[k] for k in error_kc_ids if k in name_by_id]
    causes = Counter(item.cause for item in errors if item.cause)
    return scoped, error_names, causes


def _compose_summary(
    mastery: list[tuple[str, str, float]], error_names: list[str], causes: Counter
) -> ProfileContext:
    """确定性拼装中文摘要 (不经 LLM): 掌握点 / 薄弱点(两档定性) / 近期错题KC / 错因类别。

    P2-7 脱敏: 本摘要出境 (第三方 LLM) 且进前端 trace, 薄弱点只带"较弱/待巩固"
    定性词; 错因只给类别; 全程无掌握度小数、无题干/作答原文 (见模块 docstring)。
    """
    weak = [(name, p) for _kc, name, p in mastery if p < WEAK_THRESHOLD][:_WEAK_TOP]
    mastered = [name for _kc, name, p in reversed(mastery) if p > MASTERED_THRESHOLD][
        :_MASTERED_TOP
    ]
    if not weak and not mastered and not error_names and not causes:
        return ProfileContext()
    parts: list[str] = []
    if mastered:
        parts.append("该学生已掌握: " + "、".join(mastered))
    if weak:
        # p < WEAK_THRESHOLD 的薄弱段里 zone_label 只会给出 "较弱"/"待巩固" 两档定性词
        parts.append(
            "薄弱点: " + "、".join(f"{name}({zone_label(p)})" for name, p in weak)
        )
    if error_names:
        parts.append("近期错题涉及: " + "、".join(error_names[:_RECENT_ERROR_KCS]))
    if causes:
        top_cause = causes.most_common(1)[0][0]
        parts.append("近期错因偏向: " + CAUSE_LABELS.get(top_cause, top_cause))
    summary = "; ".join(parts) + "。"
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[: SUMMARY_MAX_CHARS - 1] + "…"
    return ProfileContext(
        summary=summary, weak_points=[name for name, _p in weak], mastered=mastered
    )


async def build_profile_context(
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    kb_ids=(),
    redis=None,
) -> ProfileContext:
    """画像摘要 + 薄弱点名单; 无任何数据时返回空 ProfileContext (summary 为空串)。"""
    ids = _norm_ids(kb_ids)
    key = _cache_key(str(user_id), ids)
    if redis is not None:
        try:
            cached = await redis.get(key)
            if cached is not None:
                data = orjson.loads(cached)
                return ProfileContext(
                    summary=str(data.get("summary", "")),
                    weak_points=[str(x) for x in data.get("weak_points") or []],
                    mastered=[str(x) for x in data.get("mastered") or []],
                )
        except Exception:  # noqa: BLE001 — 缓存故障一律回源, 不阻塞问答
            log.warning("profile_summary_cache_get_failed", user_id=str(user_id))
    mastery, error_names, causes = await _load_snapshot(tenant_id, user_id, ids)
    ctx = _compose_summary(mastery, error_names, causes)
    if redis is not None:
        try:
            await redis.setex(
                key,
                CACHE_TTL_SECONDS,
                orjson.dumps(
                    {
                        "summary": ctx.summary,
                        "weak_points": ctx.weak_points,
                        "mastered": ctx.mastered,
                    }
                ),
            )
        except Exception:  # noqa: BLE001
            log.warning("profile_summary_cache_put_failed", user_id=str(user_id))
    return ctx


async def build_profile_summary(
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    kb_ids=(),
    redis=None,
) -> str:
    """画像摘要文本 (≤300 字, 已脱敏为抽象标签级 — 理由见模块 docstring, P2-7);
    无数据返回空串。结构化字段见 build_profile_context (weak_points 仅 KC 名列表)。"""
    return (await build_profile_context(tenant_id, user_id, kb_ids, redis=redis)).summary


# 难度档中文名 (与 apps/web 蓝图卡 DIFFICULTY_OPTIONS 同源)
_DIFFICULTY_LABELS = {"easy": "简单", "mid": "中等", "hard": "困难"}

# 定性区间 -> 派此难度档的解释话术 (P1-1 ZPD 自适应可解释化; 档位见
# thresholds.difficulty_for_mastery, 语义锚定仿真自适应臂的脚手架原则)
_ZONE_REASONS = {
    "较弱": "掌握度较弱，低于最近发展区，先用{difficulty}题打稳地基",
    "待巩固": "掌握度待巩固，处于最近发展区，用{difficulty}题练最见效",
    "接近掌握": "接近掌握，用{difficulty}题冲刺达标线",
    "已掌握": "已掌握，用{difficulty}题保持并挑战",
}


def difficulty_rationale(name: str, p_mastery: float, recent_error: bool) -> dict:
    """单个 KC 的难度分档 + 可解释理由 (P1-1: "为什么这套题" 逐条理由)。

    理由随蓝图确认卡与薄弱点预览出境展示, 沿用 P2-7 脱敏口径: 只带定性区间词
    (zone_label), 不带掌握度小数、不带题干/作答原文。
    """
    zone = zone_label(p_mastery)
    difficulty = difficulty_for_mastery(p_mastery)
    reason = _ZONE_REASONS[zone].format(difficulty=_DIFFICULTY_LABELS[difficulty])
    if recent_error:
        reason += "；近 7 天答错过，优先安排"
    return {"kc_name": name, "zone": zone, "difficulty": difficulty, "reason": reason}


async def weak_point_kcs(
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    kb_ids=(),
    threshold: float = QUIZ_WEAK_THRESHOLD,
    limit: int = QUIZ_WEAK_TOP,
) -> list[dict]:
    """定向出题的薄弱 KC: p<threshold, 近 7 天有错题者优先, 其余按 p 升序, 取 top limit。

    返回 [{kc_id, name, p_mastery, recent_error, zone, difficulty, reason}]; zone/difficulty/
    reason 是逐 KC 的难度分档与定性理由 (difficulty_rationale, P1-1 可解释出题)。
    无薄弱点返回空列表 (调用方须显式提示, 不得静默降级成普通出题)。
    """
    uid = uuid.UUID(str(user_id))
    ids = _norm_ids(kb_ids)
    since = datetime.now(UTC) - timedelta(days=_RECENT_ERROR_DAYS)
    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                select(
                    MasteryRecord.kc_id,
                    MasteryRecord.p_mastery,
                    KnowledgeComponent.canonical_name,
                    KnowledgeComponent.kb_id,
                )
                .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
                .where(MasteryRecord.user_id == uid, MasteryRecord.p_mastery < threshold)
                .order_by(MasteryRecord.p_mastery.asc())
            )
        ).all()
        recent = (
            await session.scalars(
                select(ErrorItem).where(
                    ErrorItem.user_id == uid, ErrorItem.created_at >= since
                )
            )
        ).all()
    recent_kc_ids = {str(k) for item in recent for k in (item.kc_ids or [])}
    kb_set = set(ids)
    scoped = [r for r in rows if not kb_set or str(r[3]) in kb_set] or list(rows)
    items = []
    for kc_id, p, name, _kb in scoped:
        recent = str(kc_id) in recent_kc_ids
        rationale = difficulty_rationale(name, float(p), recent)
        items.append(
            {
                "kc_id": str(kc_id),
                "name": name,
                # 精确数值仅供站内鉴权接口展示 (P2-7); 出境理由只用下面的定性字段
                "p_mastery": round(float(p), 4),
                "recent_error": recent,
                "zone": rationale["zone"],
                "difficulty": rationale["difficulty"],
                "reason": rationale["reason"],
            }
        )
    # 近 7 天真的错过的排前面 ("上午答错的概念下午就出现在练习卷"), 其余按掌握度升序
    items.sort(key=lambda x: (not x["recent_error"], x["p_mastery"]))
    return items[:limit]
