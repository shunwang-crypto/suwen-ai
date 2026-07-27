"""KC 聚合层地基 (UX 审计修复波次): kc -> (kb_id, kb_name, subject, chapter)。

真实规模账号 (3 库 × 152 KC) 暴露的问题: 画像/报告把 KC 摊成平列表, 前端无法
折叠成 库→章→KC 树, 雷达轴也退化成 top12 碎点。本模块提供纯确定性推导 (零 LLM):

- 章节推导: chunks.kc_ids 包含该 KC 的 chunk 的 heading_path, split(" > ") 取第二段
  (章级, 形如"第三章 微分中值定理与导数应用"), 多数决; 平票取章号靠前者 (确定性);
- 无 chunk 关联的孤儿 KC: 用 kc_edges 前置邻居 (prerequisite 的 from 侧) 已定章节
  多数决兜底; 仍无 -> "未归章";
- Redis 缓存 5 分钟, key 按 kb 维度并带 kb_version (语料重摄取会改 heading_path,
  kb_version 递增即自然失效); 缓存故障一律回源, 不阻塞主链路 (同 profile_summary)。

消费方: routers/profile.py (掌握度折叠树), routers/reports.py (章节聚合雷达 +
weak_points 按科目分组), routers/reviews.py (今日复习队列的 subject/kb_name)。
"""

from __future__ import annotations

import re
import uuid
from collections import Counter

import orjson
from eduagents_core.logging import get_logger
from sqlalchemy import select

from .db import tenant_session
from .models import Chunk, Document, KcEdge, KnowledgeBase, KnowledgeComponent

log = get_logger("kc_hierarchy")

CACHE_TTL_SECONDS = 300  # 5 分钟: 章节结构只随语料摄取变化, 短缓存足够
FALLBACK_CHAPTER = "未归章"  # 双兜底后仍无章节归属时的展示值 (前端树的兜底节点)

_HEADING_SEP = " > "
# "第三章" / "第十二章" / "第3章" —— 章号解析供轴排序 (中文数字不能按字典序排)
_CHAPTER_NO_RE = re.compile(r"^第\s*([0-9〇零一二三四五六七八九十百]+)\s*章")
_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_numeral(text: str) -> int | None:
    """中文数字 (百以内为主) / 阿拉伯数字 -> int; 解析不了返回 None。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if "百" in text:
        head, _, rest = text.partition("百")
        hundreds = _CN_DIGITS.get(head, 1) if head else 1
        rest_val = _cn_numeral(rest) if rest else 0
        return hundreds * 100 + (rest_val or 0)
    if "十" in text:
        tens_part, _, ones_part = text.partition("十")
        tens = _CN_DIGITS.get(tens_part, 1) if tens_part else 1
        ones = _CN_DIGITS.get(ones_part, 0) if ones_part else 0
        return tens * 10 + ones
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    return None


def chapter_sort_key(chapter: str) -> tuple[int, int, str]:
    """章节排序键: 能解析出章号的按章号升序, 解析不了的 (含"未归章") 排最后按名。"""
    m = _CHAPTER_NO_RE.match(chapter or "")
    if m is not None:
        no = _cn_numeral(m.group(1))
        if no is not None:
            return (0, no, chapter)
    return (1, 0, chapter or "")


def chapter_from_heading(heading_path: str) -> str:
    """heading_path -> 章级段 (第二段)。形如"书名 > 第三章 xxx > 3.1 yyy" -> "第三章 xxx"。

    无第二段 (只有书名级 / 空) 视为无章节信息, 返回空串 —— 交由多数决忽略。
    """
    segments = [s.strip() for s in (heading_path or "").split(_HEADING_SEP)]
    if len(segments) < 2:
        return ""
    return segments[1]


def derive_chapter_map(
    chunk_rows: list[tuple[str, list]],
    kc_ids: list[str],
    prereq_edges: list[tuple[str, str]],
) -> dict[str, str]:
    """纯确定性章节推导 (单测入口, 零 DB/零 LLM)。

    - chunk_rows: (heading_path, 该 chunk 的 kc_ids 列表);
    - kc_ids: 该 kb 全量 KC id (str);
    - prereq_edges: (from_kc, to_kc) 前置边, from 是 to 的前置。

    多数决平票取章号靠前者 (chapter_sort_key), 保证同一语料两次推导结果一致;
    孤儿 KC 用前置邻居**已由 chunk 定章**的章节再多数决, 仍无 -> FALLBACK_CHAPTER。
    """
    votes: dict[str, Counter] = {}
    for heading_path, chunk_kc_ids in chunk_rows:
        chapter = chapter_from_heading(heading_path)
        if not chapter:
            continue
        for kc_id in chunk_kc_ids or []:
            votes.setdefault(str(kc_id), Counter())[chapter] += 1

    def _majority(counter: Counter) -> str:
        # 票数降序, 平票按章号升序 (确定性, 不吃 dict 插入序)
        return min(counter, key=lambda ch: (-counter[ch], chapter_sort_key(ch)))

    result: dict[str, str] = {}
    all_ids = [str(k) for k in kc_ids]
    for kc_id in all_ids:
        counter = votes.get(kc_id)
        if counter:
            result[kc_id] = _majority(counter)
    # 孤儿兜底: 前置邻居 (from 侧) 已定章节的多数决; 只用 chunk 直接定章的结果,
    # 不做多级传播 (避免孤儿链把"未归章"的猜测层层放大)
    chunk_resolved = dict(result)
    for kc_id in all_ids:
        if kc_id in result:
            continue
        neighbor_chapters = Counter(
            chunk_resolved[str(from_kc)]
            for from_kc, to_kc in prereq_edges
            if str(to_kc) == kc_id and str(from_kc) in chunk_resolved
        )
        result[kc_id] = _majority(neighbor_chapters) if neighbor_chapters else FALLBACK_CHAPTER
    return result


def _cache_key(kb_id: str, kb_version: int) -> str:
    """kb 维度缓存 key; kb_version 进 key: 语料重摄取 bump 版本号即自然失效。"""
    return f"kc_chapter_map:{kb_id}:v{kb_version}"


async def _kb_chapter_map(session, kb: KnowledgeBase, redis=None) -> dict[str, str]:
    """单库全量 kc_id -> chapter (含 5 分钟 Redis 缓存; 故障回源不阻塞)。"""
    key = _cache_key(str(kb.id), kb.kb_version)
    if redis is not None:
        try:
            cached = await redis.get(key)
            if cached is not None:
                data = orjson.loads(cached)
                return {str(k): str(v) for k, v in data.items()}
        except Exception:  # noqa: BLE001 — 缓存故障回源 (同 profile_summary 口径)
            log.warning("kc_chapter_cache_get_failed", kb_id=str(kb.id))
    kc_id_rows = (
        await session.scalars(
            select(KnowledgeComponent.id).where(KnowledgeComponent.kb_id == kb.id)
        )
    ).all()
    kc_ids = [str(k) for k in kc_id_rows]
    chunk_rows = (
        await session.execute(
            select(Chunk.heading_path, Chunk.kc_ids)
            .join(Document, Document.id == Chunk.doc_id)
            .where(Document.kb_id == kb.id)
        )
    ).all()
    edge_rows = (
        await session.execute(
            select(KcEdge.from_kc, KcEdge.to_kc).where(
                KcEdge.type == "prerequisite",
                KcEdge.to_kc.in_(kc_id_rows),
            )
        )
    ).all()
    result = derive_chapter_map(
        [(hp, ids) for hp, ids in chunk_rows],
        kc_ids,
        [(str(f), str(t)) for f, t in edge_rows],
    )
    if redis is not None:
        try:
            await redis.setex(key, CACHE_TTL_SECONDS, orjson.dumps(result))
        except Exception:  # noqa: BLE001
            log.warning("kc_chapter_cache_put_failed", kb_id=str(kb.id))
    return result


async def resolve_kc_meta(
    tenant_id: uuid.UUID | str,
    kc_ids,
    redis=None,
) -> dict[str, dict]:
    """给定 tenant 的 KC 集合, 逐 KC 推导 {kb_id, kb_name, subject, chapter}。

    返回 dict: kc_id(str) -> 元信息; 库中不存在的 KC id 不出现在结果里 (调用方
    自行兜底展示)。纯确定性, 零 LLM; 内部自开 RLS 事务 (同 weak_point_kcs 惯例)。
    """
    ids: list[uuid.UUID] = []
    seen: set[str] = set()
    for kc_id in kc_ids or []:
        s = str(kc_id)
        if s in seen:
            continue
        seen.add(s)
        try:
            ids.append(uuid.UUID(s))
        except (ValueError, AttributeError, TypeError):
            continue
    if not ids:
        return {}
    async with tenant_session(tenant_id) as session:
        kc_rows = (
            await session.execute(
                select(KnowledgeComponent.id, KnowledgeComponent.kb_id).where(
                    KnowledgeComponent.id.in_(ids)
                )
            )
        ).all()
        kb_ids = {kb_id for _, kb_id in kc_rows}
        kbs = {}
        if kb_ids:
            kbs = {
                str(kb.id): kb
                for kb in (
                    await session.scalars(
                        select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids))
                    )
                ).all()
            }
        chapter_maps: dict[str, dict[str, str]] = {}
        for kb_key, kb in kbs.items():
            chapter_maps[kb_key] = await _kb_chapter_map(session, kb, redis)
    meta: dict[str, dict] = {}
    for kc_id, kb_id in kc_rows:
        kb_key = str(kb_id)
        kb = kbs.get(kb_key)
        meta[str(kc_id)] = {
            "kb_id": kb_key,
            "kb_name": kb.name if kb is not None else "",
            "subject": kb.subject if kb is not None else "",
            "chapter": chapter_maps.get(kb_key, {}).get(str(kc_id), FALLBACK_CHAPTER),
        }
    return meta
