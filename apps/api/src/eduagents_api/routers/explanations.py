"""按知识点的结构化讲解接口（0033）。

- ``GET  /api/kb/{kb_id}/kc/{kc_id}/explanation``：读缓存，如实标记 stale；
- ``POST /api/kb/{kb_id}/kc/{kc_id}/explanation``：按当前资料与掌握档生成/重生成。

三条边界（细节见 ``kc_explanation`` 模块）：出处必须落在学生本人资料的真实 chunk；
掌握档不足证据时按 unobserved 讲；生成失败不写库、不清空已有内容，由接口报错让
界面显示"暂不可用"。
"""

from __future__ import annotations

import uuid

import orjson
from eduagents_core.gateway.base import ChatMessage as GwMessage
from eduagents_core.gateway.base import ChatRequest as GwRequest
from eduagents_core.gateway.base import Tier
from eduagents_core.logging import get_logger
from eduagents_core.mastery.thresholds import (
    VALID_MASTERY_EVIDENCE_TYPES,
    evidence_state,
    mastery_band,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..kc_explanation import (
    BLOCK_TYPES,
    EXPLANATION_GENERATOR_VERSION,
    band_for,
    context_hash,
    is_stale,
    validate_blocks,
)
from ..models import (
    Chunk,
    Document,
    KcExplanation,
    KnowledgeBase,
    KnowledgeComponent,
    MasteryLog,
    MasteryRecord,
)

log = get_logger("kc_explanation")

router = APIRouter(prefix="/api/kb", tags=["explanations"])

#: 参与生成的 chunk 数上限（提示词长度与成本边界；不足就用现有的，不编造）。
_MAX_SOURCES = 6
_SOURCE_TEXT_MAX = 1200

_BAND_GUIDANCE = {
    "unobserved": "学生在这个知识点上还没有作答证据：从最基本的直觉讲起，不要假设任何先验。",
    "weak": "学生这个知识点较弱：拆小步子，先把基础概念立住，例子要最简。",
    "reinforce": "学生这个知识点待巩固：讲清关键步骤与易错处，例子要贴近常规题型。",
    "near": "学生接近掌握：重点讲边界情形与常见混淆，不要重复最基础的内容。",
    "mastered": "学生已掌握：讲深一层的联系与边界，避免复述基础定义。",
}

_SYSTEM = (
    "你是一位严谨的学科老师，只依据给定的资料原文为一个知识点写结构化讲解。\n"
    f"只允许输出这几类 block：{', '.join(BLOCK_TYPES)}。\n"
    "每个 block 必须给出 source_refs：数组，元素取自给定资料片段的 id，"
    "不得编造 id，不得引用没给你的内容。资料没讲到的内容不要写。\n"
    "diagram 只在结构本身有助于理解时才给，且必须用 data.nodes=[{id,label}] 与 "
    "data.edges=[{source,target}] 表达真实关系；给不出可靠结构就不要给 diagram。\n"
    "绝对不要出练习题、选项、答案或判分。\n"
    '只输出 JSON：{"blocks":[{"type":...,"title":...,"content":...,"source_refs":[...],'
    '"data":{...}}]}。数学表达式用成对的 $...$。'
)


async def _resolve_kb(ctx: AuthContext, kb_id: uuid.UUID) -> KnowledgeBase:
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        kb = await session.get(KnowledgeBase, kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        session.expunge(kb)
        return kb


async def _load_context(ctx: AuthContext, kb_id: uuid.UUID, kc_id: uuid.UUID):
    """取 KC 名、掌握档与本人资料里挂在该 KC 上的 chunk（确定性，不做向量检索）。"""
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        kc = await session.get(KnowledgeComponent, kc_id)
        if kc is None or str(kc.kb_id) != str(kb_id):
            raise HTTPException(status_code=404, detail="KC_NOT_FOUND")
        rows = (
            await session.execute(
                select(Chunk.id, Chunk.text, Chunk.heading_path, Chunk.page_start, Document.filename)
                .join(Document, Document.id == Chunk.doc_id)
                .where(
                    Chunk.kc_ids.contains([str(kc_id)]),
                    Document.kb_id == kb_id,
                )
                .order_by(Chunk.page_start.asc().nulls_last(), Chunk.id.asc())
                .limit(_MAX_SOURCES)
            )
        ).all()
        mastery = (
            await session.execute(
                select(
                    MasteryRecord.p_mastery,
                    func.count(func.distinct(MasteryLog.evidence_id)),
                )
                .outerjoin(
                    MasteryLog,
                    (MasteryLog.user_id == MasteryRecord.user_id)
                    & (MasteryLog.kc_id == MasteryRecord.kc_id)
                    & MasteryLog.evidence_type.in_(VALID_MASTERY_EVIDENCE_TYPES),
                )
                .where(MasteryRecord.user_id == ctx.user_id, MasteryRecord.kc_id == kc_id)
                .group_by(MasteryRecord.p_mastery)
            )
        ).first()
    sources = [
        {
            "id": str(chunk_id),
            "document": doc_name,
            "heading_path": heading_path or "",
            "page_start": page_start,
            "text": (text or "")[:_SOURCE_TEXT_MAX],
        }
        for chunk_id, text, heading_path, page_start, doc_name in rows
    ]
    if mastery is None:
        band = "unobserved"
    else:
        p_mastery, evidence_count = mastery
        band = band_for(evidence_state(int(evidence_count or 0)), mastery_band(float(p_mastery)))
    return kc.canonical_name, band, sources


def _payload(row: KcExplanation, current_hash: str, sources: list[dict]) -> dict:
    stale = is_stale(row.generator_version, row.context_hash, current_hash)
    return {
        "kc_id": str(row.kc_id),
        "band": row.band,
        "blocks": row.blocks_json,
        "sources": [s for s in sources if s["id"] in set(row.source_refs_json or [])] or sources,
        "generator_version": row.generator_version,
        "stale": stale,
        # 过期只是"按旧教学契约/旧资料生成"，内容仍可读；界面据此提示并给重新生成入口，
        # 绝不因为 stale 就把学生正在读的讲解清空。
        "stale_reason": (
            None
            if not stale
            else (
                "teaching_contract_changed"
                if row.generator_version != EXPLANATION_GENERATOR_VERSION
                else "material_changed"
            )
        ),
        "updated_at": row.updated_at,
    }


@router.get("/{kb_id}/kc/{kc_id}/explanation")
async def get_kc_explanation(
    kb_id: uuid.UUID, kc_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)
):
    """读缓存；没有就 available=false（界面显示"生成讲解"，不伪造内容）。"""
    await _resolve_kb(ctx, kb_id)
    kc_name, band, sources = await _load_context(ctx, kb_id, kc_id)
    current_hash = context_hash(kc_name, band, [s["text"] for s in sources])
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await session.scalar(
            select(KcExplanation).where(
                KcExplanation.kb_id == kb_id,
                KcExplanation.kc_id == kc_id,
                KcExplanation.band == band,
            )
        )
        if row is None:
            return {
                "available": False,
                "kc_id": str(kc_id),
                "kc_name": kc_name,
                "band": band,
                "source_count": len(sources),
            }
        payload = _payload(row, current_hash, sources)
    return {"available": True, "kc_name": kc_name, **payload}


@router.post("/{kb_id}/kc/{kc_id}/explanation")
async def generate_kc_explanation(
    kb_id: uuid.UUID, kc_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    """按当前资料与掌握档生成讲解（幂等 upsert；失败不写库、不动已有内容）。"""
    await _resolve_kb(ctx, kb_id)
    kc_name, band, sources = await _load_context(ctx, kb_id, kc_id)
    if not sources:
        # 没有资料就没有出处可引 —— 如实拒绝，不生成"看起来像讲解"的东西
        raise HTTPException(status_code=409, detail="NO_SOURCE_MATERIAL")

    gateway = request.app.state.gateway
    material = "\n\n".join(
        f"[{item['id']}] 《{item['document']}》{item['heading_path']}\n{item['text']}"
        for item in sources
    )
    result = await gateway.chat(
        GwRequest(
            messages=(
                GwMessage(role="system", content=_SYSTEM),
                GwMessage(
                    role="user",
                    content=(
                        f"知识点：{kc_name}\n"
                        f"学情提示：{_BAND_GUIDANCE.get(band, _BAND_GUIDANCE['unobserved'])}\n\n"
                        f"可用资料片段（引用时用方括号里的 id）：\n{material}"
                    ),
                ),
            ),
            tier=Tier.standard,
            json_mode=True,
            feature="kc_explanation",
            tenant_id=str(ctx.tenant_id),
            user_id=str(ctx.user_id),
            request_id=getattr(request.state, "request_id", ""),
        )
    )
    try:
        parsed = orjson.loads(result.text)
    except Exception:  # noqa: BLE001 - 模型未给可解析 JSON
        log.warning("kc_explanation_unparsable", kc_id=str(kc_id))
        raise HTTPException(status_code=502, detail="EXPLANATION_UNAVAILABLE") from None

    allowed = {item["id"] for item in sources}
    blocks, issues = validate_blocks(parsed.get("blocks"), allowed)
    if issues:
        log.info("kc_explanation_blocks_dropped", kc_id=str(kc_id), issues=issues[:5])
    if not blocks:
        # 一块都不剩：如实失败，保留库里已有内容不动
        log.warning("kc_explanation_no_valid_block", kc_id=str(kc_id), issues=issues[:5])
        raise HTTPException(status_code=502, detail="EXPLANATION_UNAVAILABLE")

    used_refs = sorted({ref for block in blocks for ref in block["source_refs"]})
    current_hash = context_hash(kc_name, band, [s["text"] for s in sources])
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await session.scalar(
            select(KcExplanation).where(
                KcExplanation.kb_id == kb_id,
                KcExplanation.kc_id == kc_id,
                KcExplanation.band == band,
            )
        )
        if row is None:
            row = KcExplanation(
                tenant_id=ctx.tenant_id,
                kb_id=kb_id,
                kc_id=kc_id,
                band=band,
                generator_version=EXPLANATION_GENERATOR_VERSION,
                context_hash=current_hash,
                blocks_json=blocks,
                source_refs_json=used_refs,
            )
            session.add(row)
        else:
            row.generator_version = EXPLANATION_GENERATOR_VERSION
            row.context_hash = current_hash
            row.blocks_json = blocks
            row.source_refs_json = used_refs
            row.updated_at = func.now()
        await session.flush()
        payload = _payload(row, current_hash, sources)
    log.info("kc_explanation_generated", kc_id=str(kc_id), band=band, blocks=len(blocks))
    return {"available": True, "kc_name": kc_name, **payload}
