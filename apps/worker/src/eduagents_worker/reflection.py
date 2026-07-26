"""Reflection 节点 (§5.5 画像写入双通道的第二条): 对话洞察异步入画像。

测评/辅导是确定性直写 (services._update_mastery), 对话只能靠抽取, 因此:
- 触发: 由 chat 路由每会话每 5 轮入队一次 (routers/chat.py, Redis 计数), 不是每轮都跑;
- 抽取: cheap 档 json_mode 从最近 ≤10 轮对话抽 {"insights":[{kc_name, signal, evidence}]};
- 落库: 仅接受能在该用户可见 KC 受控词表里匹配到的 kc_name (词表外如实丢弃, 不新建 KC);
  confused -> BKT correct=False, understood -> correct=True, **权重一律 0.3**
  (对话证据弱于测评的 1.0 与主观题/辅导小结的 0.5), evidence_type="chat_reflection";
- stub 网关下跳过 (D2 约定, 同 enrichment: stub 不产生可入库内容)。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from eduagents_core.config import get_settings
from eduagents_core.gateway import ChatMessage, ChatRequest, ModelGateway, Tier
from eduagents_core.logging import get_logger
from sqlalchemy import select

log = get_logger("reflection")

# apps/worker/src/eduagents_worker/reflection.py -> 仓库根 = parents[4]
_PROMPTS_DIR = Path(__file__).resolve().parents[4] / "prompts"

MAX_TURNS = 10  # 抽取窗口: 最近 10 条消息
CHAT_EVIDENCE_WEIGHT = 0.3  # 对话证据权重 (必须低于测评 1.0 / 辅导小结 0.5)
EVIDENCE_TYPE = "chat_reflection"
_MAX_INSIGHTS = 5
_MAX_MSG_CHARS = 600  # 单条消息进 prompt 的截断长度 (控 cheap 档上下文)
_MIN_KC_MATCH_LEN = 2  # 归一化后过短的 KC 名不参与子串匹配 (防单字误伤)


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """尽力解析单个 JSON 对象 (整体 -> 首 { 到末 }); 失败返回 None。"""
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


def match_insights(
    insights: list[dict], vocab: list[tuple[str, str, list[str]]]
) -> tuple[list[str], list[str], dict[str, str]]:
    """洞察 -> (confused kc_ids, understood kc_ids, kc_id->name)。纯函数, 便于单测。

    匹配规则与 services.apply_tutor_summary 同源: KC canonical/alias 归一化后精确相等,
    或作为子串出现在 kc_name 文本中。同一 KC 只计一次 (先出现者胜)。
    """
    from eduagents_core.kc.types import normalize_kc_name

    confused: list[str] = []
    understood: list[str] = []
    names: dict[str, str] = {}
    seen: set[str] = set()
    for item in insights[:_MAX_INSIGHTS]:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("kc_name")
        signal = item.get("signal")
        evidence = item.get("evidence")
        if not isinstance(raw_name, str) or signal not in ("confused", "understood"):
            continue
        if not isinstance(evidence, str) or not evidence.strip():
            continue  # 无学生原话支撑的洞察一律丢弃
        target = normalize_kc_name(raw_name)
        if not target:
            continue
        for kc_id, canonical, aliases in vocab:
            keys = [normalize_kc_name(canonical)] + [
                normalize_kc_name(a) for a in aliases if isinstance(a, str)
            ]
            keys = [k for k in keys if len(k) >= _MIN_KC_MATCH_LEN]
            if not any(k == target or k in target for k in keys):
                continue
            if kc_id in seen:
                break
            seen.add(kc_id)
            names[kc_id] = canonical
            (confused if signal == "confused" else understood).append(kc_id)
            break
    return confused, understood, names


async def reflect_session(
    ctx: dict,
    tenant_id: str,
    user_id: str,
    session_id: str,
    gateway: ModelGateway | None = None,
) -> str:
    """会话对话洞察 -> 画像 (ARQ 任务)。返回状态串: skipped/no_messages/no_vocab/
    parse_failed/no_match/applied:{n}。任何失败只降级, 不重试轰炸 LLM。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import ChatMessage as ChatMessageRow
    from eduagents_api.models import ChatSession, KnowledgeComponent
    from eduagents_api.services import _update_mastery
    from eduagents_core.gateway import OpenAIGateway

    settings = get_settings()
    if gateway is None:
        if settings.gateway_mode == "stub":
            log.info("reflect_skipped_stub_mode", session_id=session_id)
            return "skipped"
        gateway = OpenAIGateway(settings)

    async with tenant_session(tenant_id) as session:
        chat_session = await session.get(ChatSession, uuid.UUID(session_id))
        if chat_session is None or str(chat_session.user_id) != str(user_id):
            log.warning("reflect_session_not_found", session_id=session_id)
            return "no_messages"
        rows = (
            await session.scalars(
                select(ChatMessageRow)
                .where(ChatMessageRow.session_id == uuid.UUID(session_id))
                .order_by(ChatMessageRow.created_at.desc())
                .limit(MAX_TURNS)
            )
        ).all()
        kc_rows = (
            await session.scalars(
                select(KnowledgeComponent).order_by(KnowledgeComponent.created_at)
            )
        ).all()
        vocab = [(str(r.id), r.canonical_name, list(r.aliases or [])) for r in kc_rows]

    messages = list(reversed(rows))
    if not messages:
        return "no_messages"
    if not vocab:
        # 无受控词表 = 无处落脚 (画像外键只认归一化 kc_id, F5-A2), 不调 LLM
        log.info("reflect_no_vocabulary", session_id=session_id)
        return "no_vocab"

    history = "\n".join(
        f"{'学生' if m.role == 'user' else '助教'}: {m.content[:_MAX_MSG_CHARS]}"
        for m in messages
        if m.role in ("user", "assistant")
    )
    prompt = (
        _load_prompt("chat_reflect.md")
        .replace("{{KC_NAMES}}", "\n".join(f"- {name}" for _id, name, _a in vocab))
        .replace("{{HISTORY}}", history)
    )
    result = await gateway.chat(
        ChatRequest(
            messages=(ChatMessage(role="user", content=prompt),),
            tier=Tier.cheap,
            temperature=0.0,
            json_mode=True,
            feature="chat_reflect",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
        )
    )
    data = _parse_json_object(result.text)
    insights = data.get("insights") if isinstance(data, dict) else None
    if not isinstance(insights, list):
        log.warning("reflect_parse_failed", session_id=session_id)
        return "parse_failed"

    confused, understood, names = match_insights(insights, vocab)
    if not confused and not understood:
        return "no_match"

    auth = AuthContext(
        user_id=uuid.UUID(str(user_id)),
        tenant_id=uuid.UUID(str(tenant_id)),
        role="student",
        jti="",
    )
    changes: list[dict] = []
    async with tenant_session(tenant_id) as session:
        for kc_ids, correct in ((confused, False), (understood, True)):
            if not kc_ids:
                continue
            changes.extend(
                await _update_mastery(
                    session,
                    auth,
                    kc_ids,
                    correct=correct,
                    weight=CHAT_EVIDENCE_WEIGHT,
                    evidence_id=uuid.UUID(session_id),
                    evidence_type=EVIDENCE_TYPE,
                )
            )
    log.info(
        "reflect_done",
        session_id=session_id,
        confused=len(confused),
        understood=len(understood),
        kcs=[names.get(k, "") for k in (*confused, *understood)],
    )
    return f"applied:{len(changes)}"
