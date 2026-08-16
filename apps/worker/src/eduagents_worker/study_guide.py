"""摄取后自动学习指南 (对标 NotebookLM Source guide / Studio, brief-1 建议 #3)。

痛点: 文档摄取完用户面对一个空输入框, 不知道下一步干嘛。本任务在知识库首个文档
enrich 完成后自动生成一份**库级**指南 (大纲 / 术语表 / 推荐提问), 前端知识库页
右侧常驻, 推荐提问点击即跳对话页直接发问。

存储 (D16): 不加新表, 落 ``knowledge_bases.config_json["study_guide"]`` ——
指南是库级单例、整体读写、无需检索, JSONB 一列足够; 建表会带来迁移 + RLS 策略 +
清理路径三份成本却零收益。

失败语义与 enrichment 一致: 全程 try/except, 失败只记日志不改动既有指南;
stub 网关跳过 (D2: stub 不产生可入库内容)。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eduagents_core.config import get_settings
from eduagents_core.gateway import ChatMessage, ChatRequest, ModelGateway, Tier
from eduagents_core.json_utils import parse_json_object
from eduagents_core.logging import get_logger
from sqlalchemy import select, update

log = get_logger("study_guide")

# apps/worker/src/eduagents_worker/study_guide.py -> 仓库根 = parents[4]
_PROMPTS_DIR = Path(__file__).resolve().parents[4] / "prompts"
_MAX_ATTEMPTS = 2  # 首次 + 重试一次 (与 kc/extraction.py 同约定)
_MAX_HEADINGS = 80
_MAX_KC_NAMES = 60
_SAMPLE_PARENTS = 8
_SAMPLE_CHARS = 500

STUDY_GUIDE_VERSION = 1


async def _set_generation_status(
    tenant_id: str,
    kb_id: str,
    status: str,
    *,
    job_id: str | None = None,
    message: str | None = None,
) -> bool:
    """回写指南任务状态；job_id 不一致时拒绝旧任务覆盖新任务。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeBase

    async with tenant_session(tenant_id) as session:
        kb = await session.get(KnowledgeBase, uuid.UUID(kb_id))
        if kb is None:
            return False
        config = dict(kb.config_json or {})
        current = config.get("study_guide_generation")
        current = current if isinstance(current, dict) else {}
        current_job_id = current.get("job_id")
        if job_id and current_job_id and current_job_id != job_id:
            return False
        now = datetime.now(UTC).isoformat()
        generation = {
            **current,
            "status": status,
            **({"job_id": job_id} if job_id else {}),
        }
        if status == "running":
            generation["started_at"] = now
            generation.pop("finished_at", None)
            generation.pop("message", None)
        elif status in {"completed", "failed"}:
            generation["finished_at"] = now
        if message:
            generation["message"] = message
        config["study_guide_generation"] = generation
        kb.config_json = config
    return True


def _clean_str_list(raw: object, *, limit: int, max_len: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        value = " ".join(item.split())[:max_len]
        if value and value not in seen:
            seen.add(value)
            out.append(value)
        if len(out) >= limit:
            break
    return out


def parse_guide_payload(data: dict[str, Any], valid_kc_names: set[str]) -> dict | None:
    """校验并归一 LLM 输出; 结构不符 (含 stub 缺字段) 返回 None 触发重试。

    kc_names 只保留词表内真实存在的名称 —— 指南上的 KC 标签必须能对上库里的
    知识点, 否则前端点击标签会指向不存在的概念 (§14 禁伪造展示)。
    """
    raw_outline = data.get("outline")
    if not isinstance(raw_outline, list) or not raw_outline:
        return None
    outline: list[dict] = []
    for item in raw_outline:
        if not isinstance(item, dict):
            return None
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        summary = item.get("summary")
        outline.append(
            {
                "title": " ".join(title.split())[:60],
                "summary": " ".join(summary.split())[:300] if isinstance(summary, str) else "",
                "kc_names": [
                    n
                    for n in _clean_str_list(item.get("kc_names"), limit=4, max_len=60)
                    if not valid_kc_names or n in valid_kc_names
                ],
            }
        )
        if len(outline) >= 8:
            break

    glossary: list[dict] = []
    raw_glossary = data.get("glossary")
    if isinstance(raw_glossary, list):
        for item in raw_glossary:
            if not isinstance(item, dict):
                continue  # 术语允许稀疏: 坏项跳过不整体报废
            term, definition = item.get("term"), item.get("definition")
            if not isinstance(term, str) or not term.strip():
                continue
            glossary.append(
                {
                    "term": " ".join(term.split())[:60],
                    "definition": (
                        " ".join(definition.split())[:200]
                        if isinstance(definition, str)
                        else ""
                    ),
                }
            )
            if len(glossary) >= 12:
                break

    questions = _clean_str_list(data.get("suggested_questions"), limit=5, max_len=120)
    if not questions:
        return None
    return {"outline": outline, "glossary": glossary, "suggested_questions": questions}


async def _generate(
    gateway: ModelGateway,
    *,
    kb_name: str,
    kb_subject: str,
    headings: list[str],
    kc_names: list[str],
    sample: str,
    tenant_id: str,
    user_id: str = "",
) -> dict | None:
    template = (_PROMPTS_DIR / "study_guide.md").read_text(encoding="utf-8")
    prompt = (
        template.replace("{{KB_NAME}}", kb_name)
        .replace("{{KB_SUBJECT}}", kb_subject)
        .replace("{{HEADING_PATHS}}", "\n".join(f"- {h}" for h in headings) or "(无标题结构)")
        .replace("{{KC_NAMES}}", "、".join(kc_names) or "(词表暂为空)")
        .replace("{{CONTENT_SAMPLE}}", sample)
    )
    valid = set(kc_names)
    for _ in range(_MAX_ATTEMPTS):
        result = await gateway.chat(
            ChatRequest(
                messages=(ChatMessage(role="user", content=prompt),),
                tier=Tier.standard,
                temperature=0.3,
                json_mode=True,
                feature="study_guide",
                tenant_id=tenant_id,
                user_id=user_id,
                max_tokens=2000,
            )
        )
        if result.mode == "stub":
            return None  # stub 网关不产生可入库内容 (D2)
        data = parse_json_object(result.text)
        if data is None:
            continue
        guide = parse_guide_payload(data, valid)
        if guide is not None:
            return guide
    return None


async def build_study_guide(
    ctx: dict, tenant_id: str, kb_id: str, gateway: ModelGateway | None = None
) -> str:
    """生成/重建知识库级学习指南, 写入 kb.config_json["study_guide"]。

    返回 skipped | empty | failed | built。
    """
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document, KnowledgeBase, KnowledgeComponent

    from .enrichment import get_worker_gateway

    job_id = str(ctx.get("job_id") or "") or None
    if not await _set_generation_status(
        tenant_id, kb_id, "running", job_id=job_id
    ):
        log.info("study_guide_stale_job_skipped", kb_id=kb_id, job_id=job_id)
        return "skipped"

    settings = get_settings()
    if gateway is None:
        if settings.gateway_mode == "stub":
            log.info("study_guide_skipped_stub_mode", kb_id=kb_id)
            await _set_generation_status(
                tenant_id,
                kb_id,
                "failed",
                job_id=job_id,
                message="当前环境未启用 AI 生成服务",
            )
            return "skipped"
        # P0-2a: 共享带 usage_sink+budget_guard 的进程级单例, 不再裸构造
        gateway = get_worker_gateway(settings)

    try:
        async with tenant_session(tenant_id) as session:
            kb = await session.get(KnowledgeBase, uuid.UUID(kb_id))
            if kb is None:
                return "empty"
            kb_name, kb_subject = kb.name, kb.subject
            doc_ids = (
                await session.scalars(
                    select(Document.id).where(Document.kb_id == uuid.UUID(kb_id))
                )
            ).all()
            uploader = await session.scalar(
                select(Document.uploaded_by)
                .where(Document.kb_id == uuid.UUID(kb_id), Document.uploaded_by.isnot(None))
                .limit(1)
            )
            effective_user_id = str(uploader) if uploader else ""
            if not doc_ids:
                log.info("study_guide_no_documents", kb_id=kb_id)
                await _set_generation_status(
                    tenant_id,
                    kb_id,
                    "failed",
                    job_id=job_id,
                    message="知识库中还没有可用于生成指南的资料",
                )
                return "empty"
            rows = (
                await session.scalars(
                    select(Chunk).where(Chunk.doc_id.in_(doc_ids)).order_by(Chunk.created_at)
                )
            ).all()
            kc_names = [
                r.canonical_name
                for r in (
                    await session.scalars(
                        select(KnowledgeComponent).where(
                            KnowledgeComponent.kb_id == uuid.UUID(kb_id)
                        )
                    )
                ).all()
            ][:_MAX_KC_NAMES]

        if not rows:
            log.info("study_guide_no_chunks", kb_id=kb_id)
            await _set_generation_status(
                tenant_id,
                kb_id,
                "failed",
                job_id=job_id,
                message="资料尚未完成解析，请稍后重试",
            )
            return "empty"
        headings = [h for h in dict.fromkeys(r.heading_path for r in rows) if h][:_MAX_HEADINGS]
        parents = [r for r in rows if r.parent_id is None][:_SAMPLE_PARENTS]
        sample = "\n\n".join(p.text[:_SAMPLE_CHARS] for p in parents) or rows[0].text[:2000]

        guide = await _generate(
            gateway,
            kb_name=kb_name,
            kb_subject=kb_subject,
            headings=headings,
            kc_names=kc_names,
            sample=sample,
            tenant_id=tenant_id,
            user_id=effective_user_id,
        )
        if guide is None:
            log.warning("study_guide_unparseable", kb_id=kb_id)
            await _set_generation_status(
                tenant_id,
                kb_id,
                "failed",
                job_id=job_id,
                message="学习指南生成失败，请重试",
            )
            return "failed"

        guide = {
            **guide,
            "version": STUDY_GUIDE_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "doc_count": len(doc_ids),
        }
        async with tenant_session(tenant_id) as session:
            kb = await session.get(KnowledgeBase, uuid.UUID(kb_id))
            if kb is None:
                return "empty"
            # JSONB 就地改字典 SQLAlchemy 检测不到: 必须整列重写
            config = dict(kb.config_json or {})
            current = config.get("study_guide_generation")
            current = current if isinstance(current, dict) else {}
            if job_id and current.get("job_id") and current.get("job_id") != job_id:
                log.info("study_guide_stale_result_discarded", kb_id=kb_id, job_id=job_id)
                return "skipped"
            config["study_guide"] = guide
            config["study_guide_generation"] = {
                **current,
                "status": "completed",
                **({"job_id": job_id} if job_id else {}),
                "finished_at": datetime.now(UTC).isoformat(),
            }
            await session.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == uuid.UUID(kb_id))
                .values(config_json=config)
            )
        log.info(
            "study_guide_built",
            kb_id=kb_id,
            outline=len(guide["outline"]),
            glossary=len(guide["glossary"]),
            questions=len(guide["suggested_questions"]),
        )
        return "built"
    except Exception:  # noqa: BLE001 — 指南是增强功能, 失败不该影响摄取主链路
        log.exception("study_guide_failed", kb_id=kb_id)
        await _set_generation_status(
            tenant_id,
            kb_id,
            "failed",
            job_id=job_id,
            message="学习指南生成失败，请稍后重试",
        )
        return "failed"
