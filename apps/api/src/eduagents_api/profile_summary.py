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

偏好段 (P2-3, G8): 摘要末尾追加 user_preferences 的定性描述, 形如
「讲解偏好: 类比优先、简洁; 兴趣: 篮球、动漫; 目标: 高数期末冲 90」。
兴趣/目标是用户**自报**数据且只进其本人的 prompt (非平台推断、无掌握度数值),
符合 P2-7 口径。偏好段优先级低于薄弱点段: 总长超 300 字时先砍兴趣,
再不够则整段让位。只有偏好没有掌握度数据的新用户 (Onboarding 冷启动, G12)
摘要同样非空 —— 个性化答案不得跨用户复用, 缓存互斥由 empty 判定自然生效。

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
from eduagents_core.kc.types import normalize_kc_name
from eduagents_core.logging import get_logger

# 阈值唯一来源 (个性化波次 G6): 此处仅 import 再导出 (routers/profile.py 等既有
# 消费方沿用本模块入口), 禁止再落地本地硬编码副本。
from eduagents_core.mastery.thresholds import (
    EVIDENCE_OBSERVED_MIN_EVENTS,
    MASTERED_THRESHOLD,
    QUIZ_WEAK_THRESHOLD,
    VALID_MASTERY_EVIDENCE_TYPES,
    WEAK_THRESHOLD,
    ZPD_LOWER,
    difficulty_for_mastery,
    evidence_state,
    mastery_band,
    zone_label,
)
from sqlalchemy import and_, func, select

from .db import tenant_session
from .models import ErrorItem, KnowledgeComponent, MasteryLog, MasteryRecord, UserPreference

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
# 题面 ↔ KC 名子串匹配的最短归一化长度 (同 services._MIN_KC_MATCH_LEN 口径):
# 单字 KC 名 (如"角""集") 会命中几乎任意题面, 放行会把起始层级判飞。
_MIN_BAND_MATCH_LEN = 2

# 错因五分类中文名 (与 apps/web profile 页 CAUSE_LABELS 同源)
CAUSE_LABELS = {
    "concept_confusion": "概念混淆",
    "formula_error": "公式用错",
    "calc_slip": "计算失误",
    "misread": "审题偏差",
    "knowledge_gap": "知识缺口",
}

# 偏好枚举 -> 中文定性词 (P2-3)。键序即枚举白名单唯一出处: routers/preferences.py
# 校验与 0005 迁移 CHECK 约束同口径。风格枚举是**内容组织维度** (讲解怎么组织),
# 不涉及任何感官类型分类 (该类说法已被证伪, 全仓禁用)。
STYLE_LABELS = {"rigorous": "严谨", "colloquial": "口语", "analogy": "类比优先"}
VERBOSITY_LABELS = {"concise": "简洁", "detailed": "详尽"}
EXPLANATION_STYLES = tuple(STYLE_LABELS)
VERBOSITY_LEVELS = tuple(VERBOSITY_LABELS)

# Different model features need different, minimal profile slices.  Keeping the
# allow-list here makes prompt data flow auditable and prevents a new consumer
# from silently receiving every profile field.
ROLE_SECTIONS: dict[str, tuple[str, ...]] = {
    "qa": ("mastered", "weak", "errors", "causes", "prefs"),
    "quiz": ("weak", "errors", "causes", "prefs"),
    "tutor": ("weak", "errors", "prefs"),
    # plan 必须拿到 mastered: 规划的核心决策之一是"哪些可以跳过/略讲"。只给薄弱点
    # 会让 LLM 对已掌握的知识点也照常展开, 挤占有限的每日时长。quiz 相反 —— 出题
    # 不需要已掌握 (不出会做的题), 故仍不给。
    "plan": ("mastered", "weak", "errors", "prefs"),
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


def _cache_key(user_id: str, kb_ids: list[str], role: str = "qa") -> str:
    base = f"profile_summary:{role}:{user_id}"
    if not kb_ids:
        return base
    # 绑定知识库不同 -> 画像取材范围不同, 缓存必须分桶 (否则跨库串味)
    digest = hashlib.sha1("|".join(sorted(kb_ids)).encode()).hexdigest()[:8]
    return f"{base}:{digest}"


def _norm_ids(kb_ids) -> list[str]:
    return [str(k) for k in (kb_ids or []) if str(k)]


async def invalidate_profile_summary_cache(redis, user_id: uuid.UUID | str) -> int:
    """删掉该用户 profile_summary 的全部缓存桶 (裸 key + 各 kb 指纹桶), 返回删除数。

    PUT /api/preferences 保存后调用: 60s TTL 对"改完偏好立刻问答"的演示即时性
    太慢, 必须立即失效。user_id 是定长 UUID, 前缀匹配不会误伤他人 key。
    缓存故障只记日志 (最坏退化为 60s 后自然过期), 不阻塞偏好保存主链路。
    """
    if redis is None:
        return 0
    try:
        keys = [
            key
            async for key in redis.scan_iter(match=f"profile_summary:*:{user_id}*")
        ]
        if keys:
            await redis.delete(*keys)
        return len(keys)
    except Exception:  # noqa: BLE001 — 缓存故障不阻塞主链路 (与读侧同口径)
        log.warning("profile_summary_cache_invalidate_failed", user_id=str(user_id))
        return 0


async def _load_snapshot(
    tenant_id: uuid.UUID | str, user_id: uuid.UUID | str, kb_ids: list[str]
) -> tuple[list[tuple[str, str, float, str]], list[str], Counter, dict | None]:
    """一次事务读齐: (掌握度行含 evidence_state, 最近错题 KC 名, 近期错因计数, 偏好)。

    掌握度行形如 (kc_id, kc_name, p_mastery), 已按 p 升序。kb_ids 非空时按所属知识库
    过滤; 过滤后为空则回退全量 (跨库学习者不该因绑库而丢画像)。偏好 (P2-3) 与
    知识库无关, 不参与 kb 过滤。
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
                    func.count(func.distinct(MasteryLog.evidence_id)),
                )
                .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
                .outerjoin(
                    MasteryLog,
                    (MasteryLog.user_id == MasteryRecord.user_id)
                    & (MasteryLog.kc_id == MasteryRecord.kc_id)
                    & MasteryLog.evidence_type.in_(VALID_MASTERY_EVIDENCE_TYPES),
                )
                .where(MasteryRecord.user_id == uid)
                .group_by(
                    MasteryRecord.kc_id,
                    MasteryRecord.p_mastery,
                    KnowledgeComponent.canonical_name,
                    KnowledgeComponent.kb_id,
                )
                .order_by(MasteryRecord.p_mastery.asc())
            )
        ).all()
        errors = (
            await session.scalars(
                select(ErrorItem)
                .where(
                    ErrorItem.user_id == uid,
                    ErrorItem.verification_status == "verified",
                )
                .order_by(ErrorItem.created_at.desc())
                .limit(_ERROR_SCAN)
            )
        ).all()
        error_kc_ids: list[str] = []
        for item in errors[:_RECENT_ERROR_KCS]:
            for kc_id in item.kc_ids or []:
                if str(kc_id) not in error_kc_ids:
                    error_kc_ids.append(str(kc_id))
        name_by_id = {str(kc_id): name for kc_id, _p, name, _kb, _count in rows}
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
        pref_row = await session.get(UserPreference, uid)
        prefs = (
            None
            if pref_row is None
            else {
                "explanation_style": pref_row.explanation_style,
                "verbosity": pref_row.verbosity,
                "interests": [str(x) for x in pref_row.interests or []],
                "daily_minutes": pref_row.daily_minutes,
                "goal": pref_row.goal,
            }
        )

    kb_set = set(kb_ids)
    scoped = [
        (str(kc_id), name, float(p), evidence_state(int(count)))
        for kc_id, p, name, kb_id, count in rows
        if not kb_set or str(kb_id) in kb_set
    ]
    if not scoped:
        scoped = [
            (str(kc_id), name, float(p), evidence_state(int(count)))
            for kc_id, p, name, _kb, count in rows
        ]
    error_names = [name_by_id[k] for k in error_kc_ids if k in name_by_id]
    causes = Counter(item.cause for item in errors if item.cause)
    return scoped, error_names, causes, prefs


def _pref_parts(prefs: dict | None, include_interests: bool = True) -> list[str]:
    """偏好 dict -> 定性描述片段 (P2-3)。全部字段可缺; 空 dict/None 返回空列表。

    兴趣/目标为用户自报原话, 只进本人 prompt (P2-7 口径允许); 时长供计划链路
    参考。include_interests=False 是超长降级档 (先砍兴趣, 见 _compose_summary)。
    """
    if not prefs:
        return []
    parts: list[str] = []
    style_bits = [
        label
        for label in (
            STYLE_LABELS.get(prefs.get("explanation_style") or ""),
            VERBOSITY_LABELS.get(prefs.get("verbosity") or ""),
        )
        if label
    ]
    if style_bits:
        parts.append("讲解偏好: " + "、".join(style_bits))
    interests = [str(x).strip() for x in prefs.get("interests") or [] if str(x).strip()]
    if include_interests and interests:
        parts.append("兴趣: " + "、".join(interests))
    goal = str(prefs.get("goal") or "").strip()
    if goal:
        parts.append("目标: " + goal)
    minutes = prefs.get("daily_minutes")
    if minutes:
        parts.append(f"每日可学约{int(minutes)}分钟")
    return parts


def _compose_summary(
    mastery: list[tuple],
    error_names: list[str],
    causes: Counter,
    prefs: dict | None = None,
    role: str = "qa",
) -> ProfileContext:
    """确定性拼装中文摘要 (不经 LLM): 掌握点 / 薄弱点(两档定性) / 近期错题KC /
    错因类别 / 偏好段 (P2-3)。

    P2-7 脱敏: 本摘要出境 (第三方 LLM) 且进前端 trace, 薄弱点只带"较弱/待巩固"
    定性词; 错因只给类别; 全程无掌握度小数、无题干/作答原文 (见模块 docstring)。
    偏好段排最后且优先级最低: 超 300 字先砍兴趣, 再不够整段让位给薄弱点段。
    """
    # Pure callers/tests may still pass a compact three-tuple.  Treating that
    # as observed is a function-input convenience only; database consumers
    # always supply the explicitly derived fourth field above.
    normalized = [
        (*row, "observed") if len(row) == 3 else row
        for row in mastery
    ]
    sections = ROLE_SECTIONS.get(role, ROLE_SECTIONS["qa"])
    weak_all = [
        (name, p)
        for _kc, name, p, state in normalized
        if state == "observed" and p < WEAK_THRESHOLD
    ][:_WEAK_TOP]
    mastered_all = [name for _kc, name, p, state in reversed(normalized) if state == "observed" and p > MASTERED_THRESHOLD][
        :_MASTERED_TOP
    ]
    pref_parts = _pref_parts(prefs)
    weak = weak_all if "weak" in sections else []
    mastered = mastered_all if "mastered" in sections else []
    selected_errors = error_names if "errors" in sections else []
    selected_causes = causes if "causes" in sections else Counter()
    if "prefs" not in sections:
        pref_parts = []
    if not weak and not mastered and not selected_errors and not selected_causes and not pref_parts:
        return ProfileContext()
    parts: list[str] = []
    if mastered:
        parts.append("该学生已掌握: " + "、".join(mastered))
    if weak:
        # p < WEAK_THRESHOLD 的薄弱段里 zone_label 只会给出 "较弱"/"待巩固" 两档定性词
        parts.append(
            "薄弱点: " + "、".join(f"{name}({zone_label(p)})" for name, p in weak)
        )
    if selected_errors:
        parts.append("近期错题涉及: " + "、".join(selected_errors[:_RECENT_ERROR_KCS]))
    if selected_causes:
        top_cause = selected_causes.most_common(1)[0][0]
        parts.append("近期错因偏向: " + CAUSE_LABELS.get(top_cause, top_cause))
    summary = "; ".join(parts + pref_parts) + "。"
    if len(summary) > SUMMARY_MAX_CHARS and pref_parts:
        # 偏好段低优先: 先砍兴趣, 仍超长且薄弱点段非空时整段让位 (P2-3)
        pref_parts = _pref_parts(prefs, include_interests=False)
        summary = "; ".join(parts + pref_parts) + "。"
        if len(summary) > SUMMARY_MAX_CHARS and parts:
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
    role: str = "qa",
) -> ProfileContext:
    """Build the minimal profile slice for a model feature.

    Unknown roles deliberately fall back to ``qa`` for backwards
    compatibility.  The normalized role is part of the Redis key, so cached
    summaries from one feature can never be reused by another feature.
    """
    if role not in ROLE_SECTIONS:
        role = "qa"
    ids = _norm_ids(kb_ids)
    key = _cache_key(str(user_id), ids, role)
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
    mastery, error_names, causes, prefs = await _load_snapshot(tenant_id, user_id, ids)
    ctx = _compose_summary(mastery, error_names, causes, prefs, role=role)
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


# 难度档中文名 (与 apps/web 蓝图卡 DIFFICULTY_OPTIONS 同源)
_DIFFICULTY_LABELS = {"easy": "简单", "mid": "中等", "hard": "困难"}

# 定性区间 -> 派此难度档的解释话术，档位见 thresholds.difficulty_for_mastery。
# C3 差异化: 每个区间给 (区间前段, 区间后段) 两套措辞骨架, 再叠 recent_error
# 两款衔接语 —— 4 区间 × 2 段位 × 2 错题态 = 16 种组合, 且理由点名 KC,
# 同卷 5 个 KC 不再出现整段一字不差的复读。措辞纪律不变 (P2-7): 只有定性词,
# 零掌握度小数; 区间词 (zone_label) 必须原样出现在理由里 (前端/测试同源断言)。
_ZONE_REASONS = {
    "较弱": (
        "掌握度较弱，尚在起步，低于最近发展区，先用{difficulty}题把基础概念立起来",
        "掌握度较弱，有过接触但还没入门，先用{difficulty}题打稳地基",
    ),
    "待巩固": (
        "掌握度待巩固，刚迈进最近发展区，用{difficulty}题小步推进最见效",
        "掌握度待巩固，已到最近发展区后段，用{difficulty}题练熟就能上台阶",
    ),
    "接近掌握": (
        "接近掌握，还差一段火候，用{difficulty}题继续逼近达标线",
        "接近掌握，离达标线只差一步，用{difficulty}题冲刺达标",
    ),
    "已掌握": (
        "已掌握，用{difficulty}题保温，防止生疏回退",
        "已掌握且相当扎实，用{difficulty}题挑战边界、保持手感",
    ),
}
# recent_error 衔接语同样分两款 (按段位错开), 断言锚点"近 7 天答错过"两款皆含
_RECENT_ERROR_SUFFIXES = ("；近 7 天答错过，优先安排重练", "；近 7 天答错过，趁热订正防再错")
# 各定性区间的 p 值边界 (与 thresholds.zone_label 同源), 用于判定区间内段位
_ZONE_BANDS = {
    "较弱": (0.0, ZPD_LOWER),
    "待巩固": (ZPD_LOWER, QUIZ_WEAK_THRESHOLD),
    "接近掌握": (QUIZ_WEAK_THRESHOLD, MASTERED_THRESHOLD),
    "已掌握": (MASTERED_THRESHOLD, 1.0),
}


def _zone_position(zone: str, p_mastery: float) -> int:
    """区间内段位: 0 = 前段 (低半区), 1 = 后段 (高半区)。纯确定性。"""
    lo, hi = _ZONE_BANDS[zone]
    return 0 if p_mastery < (lo + hi) / 2 else 1


def difficulty_rationale(name: str, p_mastery: float, recent_error: bool) -> dict:
    """单个 KC 的难度分档 + 可解释理由 (P1-1: "为什么这套题" 逐条理由)。

    理由随蓝图确认卡与薄弱点预览出境展示, 沿用 P2-7 脱敏口径: 只带定性区间词
    (zone_label), 不带掌握度小数、不带题干/作答原文。C3: 措辞按
    zone × 区间段位 × recent_error 差异化, 并以「KC 名」开头 (同卷理由不复读)。
    """
    zone = zone_label(p_mastery)
    difficulty = difficulty_for_mastery(p_mastery)
    pos = _zone_position(zone, p_mastery)
    reason = f"「{name}」" + _ZONE_REASONS[zone][pos].format(
        difficulty=_DIFFICULTY_LABELS[difficulty]
    )
    if recent_error:
        reason += _RECENT_ERROR_SUFFIXES[pos]
    return {"kc_name": name, "zone": zone, "difficulty": difficulty, "reason": reason}


async def problem_mastery_band(
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    problem: str,
    kb_ids=(),
) -> str:
    """题目文本命中的 KC 的掌握度档 (weak/reinforce/near/mastered), 无命中返回 ""。

    tutor 子图内不查库 (GraphDeps 无 session), 因此这里在路由层算好后注入 state,
    由 policy 决定**起始** hint_level (§18: 讲解先适应"会不会")。

    匹配口径与 tutor_graph._render_profile_section 的开场关联一致: 归一化后做子串
    包含。命中多个时取掌握度**最低**的 —— 一道题涉及多个知识点时, 学生的难点在最
    弱的那个, 按最强的定起始层级会把人架空。
    """
    key = normalize_kc_name(problem or "")
    if not key:
        return ""
    uid = uuid.UUID(str(user_id))
    ids = _norm_ids(kb_ids)
    async with tenant_session(tenant_id) as session:
        stmt = (
            select(
                KnowledgeComponent.canonical_name,
                MasteryRecord.p_mastery,
                func.count(func.distinct(MasteryLog.evidence_id)),
            )
            .join(MasteryRecord, MasteryRecord.kc_id == KnowledgeComponent.id)
            .join(
                MasteryLog,
                (MasteryLog.user_id == MasteryRecord.user_id)
                & (MasteryLog.kc_id == MasteryRecord.kc_id)
                & MasteryLog.evidence_type.in_(VALID_MASTERY_EVIDENCE_TYPES),
            )
            .where(MasteryRecord.user_id == uid)
            .group_by(KnowledgeComponent.canonical_name, MasteryRecord.p_mastery)
            .having(
                func.count(func.distinct(MasteryLog.evidence_id))
                >= EVIDENCE_OBSERVED_MIN_EVENTS
            )
        )
        if ids:
            stmt = stmt.where(KnowledgeComponent.kb_id.in_([uuid.UUID(i) for i in ids]))
        rows = (await session.execute(stmt)).all()

    hits = [
        float(p)
        for name, p, _count in rows
        if name and len(normalize_kc_name(name)) >= _MIN_BAND_MATCH_LEN
        and normalize_kc_name(name) in key
    ]
    return mastery_band(min(hits)) if hits else ""


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
    async with tenant_session(tenant_id) as session:
        return await _load_weak_point_candidates(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            kb_ids=kb_ids,
            threshold=threshold,
            limit=limit,
        )


async def _load_weak_point_candidates(
    session,
    *,
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    kb_ids=(),
    threshold: float = QUIZ_WEAK_THRESHOLD,
    limit: int = QUIZ_WEAK_TOP,
) -> list[dict]:
    """Load and order the server-owned weak-KC facts for every consumer.

    Profile preview and practice recommendation intentionally share this one
    query and ordering policy.  ``p_mastery`` stays in this internal result so
    the recommendation can choose a difficulty, but callers that leave this
    module must decide separately whether that fact is safe to expose.
    """
    uid = uuid.UUID(str(user_id))
    tid = uuid.UUID(str(tenant_id))
    ids = _norm_ids(kb_ids)
    stmt = (
        select(
            MasteryRecord.kc_id,
            MasteryRecord.p_mastery,
            KnowledgeComponent.canonical_name,
            KnowledgeComponent.created_at,
            func.count(func.distinct(MasteryLog.evidence_id)),
        )
        .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
        .join(
            MasteryLog,
            (MasteryLog.user_id == MasteryRecord.user_id)
            & (MasteryLog.kc_id == MasteryRecord.kc_id)
            & MasteryLog.evidence_type.in_(VALID_MASTERY_EVIDENCE_TYPES),
        )
        .where(
            MasteryRecord.tenant_id == tid,
            MasteryRecord.user_id == uid,
            MasteryRecord.p_mastery < threshold,
        )
        .group_by(
            MasteryRecord.kc_id,
            MasteryRecord.p_mastery,
            KnowledgeComponent.canonical_name,
            KnowledgeComponent.created_at,
            KnowledgeComponent.id,
        )
        .having(
            func.count(func.distinct(MasteryLog.evidence_id))
            >= EVIDENCE_OBSERVED_MIN_EVENTS
        )
        .order_by(
            MasteryRecord.p_mastery.asc(),
            KnowledgeComponent.created_at.asc(),
            KnowledgeComponent.id.asc(),
        )
    )
    if ids:
        stmt = stmt.where(
            KnowledgeComponent.kb_id.in_([uuid.UUID(item) for item in ids])
        )
    rows = (await session.execute(stmt)).all()
    since = datetime.now(UTC) - timedelta(days=_RECENT_ERROR_DAYS)
    recent_rows = (
        await session.execute(
            select(ErrorItem.kc_ids).where(
                ErrorItem.tenant_id == tid,
                ErrorItem.user_id == uid,
                ErrorItem.verification_status == "verified",
                ErrorItem.created_at >= since,
            )
        )
    ).all()
    recent_kc_ids = {
        str(kc_id)
        for (kc_ids_for_error,) in recent_rows
        for kc_id in (kc_ids_for_error or [])
    }
    items = []
    for kc_id, p, name, created_at, count in rows:
        recent_error = str(kc_id) in recent_kc_ids
        rationale = difficulty_rationale(name, float(p), recent_error)
        items.append(
            {
                "kc_id": str(kc_id),
                "name": name,
                # 精确数值仅供站内鉴权接口展示 (P2-7); 出境理由只用下面的定性字段
                "p_mastery": round(float(p), 4),
                "evidence_state": evidence_state(int(count)),
                "recent_error": recent_error,
                "zone": rationale["zone"],
                "difficulty": rationale["difficulty"],
                "reason": rationale["reason"],
                "_stable_order": (created_at, str(kc_id)),
            }
        )
    # 近 7 天真的错过的排前面，随后按掌握度，再按稳定 KC 数据库顺序。
    items.sort(
        key=lambda item: (
            not item["recent_error"],
            item["p_mastery"],
            item["_stable_order"],
        )
    )
    bounded_limit = max(0, int(limit))
    for item in items:
        item.pop("_stable_order", None)
    return items[:bounded_limit]


async def assessment_mode_kcs(
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    kb_ids=(),
    limit: int = QUIZ_WEAK_TOP,
) -> list[dict]:
    """证据不足时的诊断出题目标 (§5.3 / §15 Assessment Mode)。

    冷启动**不是异常分支**：`unobserved/estimated` 应安排快速验证，
    而不是把 BKT 先验判定为薄弱或已掌握。

    这里按有效 mastery_log 事件数升序取 KC。完全没有 mastery_record
    的情形一并覆盖；不再从 confidence 反推观测次数。
    """
    uid = uuid.UUID(str(user_id))
    ids = _norm_ids(kb_ids)
    async with tenant_session(tenant_id) as session:
        stmt = (
            select(
                KnowledgeComponent.id,
                KnowledgeComponent.canonical_name,
                KnowledgeComponent.kb_id,
                MasteryRecord.p_mastery,
                func.count(func.distinct(MasteryLog.evidence_id)),
            )
            .join(
                MasteryRecord,
                and_(
                    MasteryRecord.kc_id == KnowledgeComponent.id,
                    MasteryRecord.user_id == uid,
                ),
                isouter=True,
            )
            .join(
                MasteryLog,
                and_(
                    MasteryLog.kc_id == KnowledgeComponent.id,
                    MasteryLog.user_id == uid,
                    MasteryLog.evidence_type.in_(VALID_MASTERY_EVIDENCE_TYPES),
                ),
                isouter=True,
            )
            .group_by(
                KnowledgeComponent.id,
                KnowledgeComponent.canonical_name,
                KnowledgeComponent.kb_id,
                MasteryRecord.p_mastery,
            )
            .order_by(
                func.count(func.distinct(MasteryLog.evidence_id)).asc(),
                KnowledgeComponent.canonical_name.asc(),
            )
        )
        if ids:
            stmt = stmt.where(KnowledgeComponent.kb_id.in_([uuid.UUID(i) for i in ids]))
        rows = (await session.execute(stmt)).all()

    items: list[dict] = []
    for kc_id, name, _kb, p, count in rows:
        state = evidence_state(int(count))
        if state == "observed":
            continue
        # 诊断题的难度固定走中档: 目的是量出水平, 不是照着未验证的 p_mastery 调难度。
        items.append(
            {
                "kc_id": str(kc_id),
                "name": name,
                "p_mastery": round(float(p), 4) if p is not None else None,
                "evidence_state": state,
                "recent_error": False,
                "zone": "unverified",
                "difficulty": "mid",
                "reason": f"{name}尚未测过，先做一次快速验证",
            }
        )
        if len(items) >= limit:
            break
    return items


async def has_any_kc(
    tenant_id: uuid.UUID | str,
    kb_ids=(),
) -> bool:
    """作用域内是否存在任何 KC。

    用于区分两种"出不了题": KC 图为空 (前置缺失, 该去传资料) vs KC 都已验证掌握
    (成功状态, 无题可出)。二者对用户的含义相反, 合并成一个错误码会把文案说反。
    """
    ids = _norm_ids(kb_ids)
    async with tenant_session(tenant_id) as session:
        stmt = select(func.count()).select_from(KnowledgeComponent)
        if ids:
            stmt = stmt.where(KnowledgeComponent.kb_id.in_([uuid.UUID(i) for i in ids]))
        return bool(await session.scalar(stmt))
