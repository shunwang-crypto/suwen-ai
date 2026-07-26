"""KC LLM 抽取层 (§5.8): 全部经 ModelGateway, json_mode=True, 严格解析+一次重试。

- build_vocabulary: 知识库级受控词表 + 先修边 (strong 档, enrich 阶段一次性);
- extract_chunk_kcs: chunk 级知识点名称抽取 (cheap 档, 名称交给 KcNormalizer 归并)。
提示词从 仓库根/prompts/*.md 读取。解析失败重试一次, 仍失败抛 KcExtractionError;
stub 网关的 {"stub": true, ...} 缺业务字段, 同样按解析失败处理 (不崩裸异常)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..gateway.base import ChatMessage, ChatRequest, ModelGateway, Tier
from .dag import add_edge_safe
from .types import KcEntry, KcVocabulary, PrereqEdge, normalize_kc_name

# 本文件位于 packages/core/src/eduagents_core/kc/ -> parents[5] 为仓库根
_PROMPTS_DIR = Path(__file__).resolve().parents[5] / "prompts"
_MAX_ATTEMPTS = 2  # 首次 + 重试一次


class KcExtractionError(Exception):
    """LLM KC 抽取输出无法解析为约定 JSON schema (重试一次后仍失败)。"""


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """尽力解析单个 JSON 对象: 先整体, 再截取首 { 到末 } (容忍代码块围栏)。

    任何失败返回 None, 不向外抛裸异常。
    """
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


def _clean_aliases(canonical: str, raw: object) -> list[str]:
    """别名清洗: 只留非空字符串, 去掉与 canonical 重复/彼此重复 (归一化后比较)。"""
    if not isinstance(raw, list):
        return []
    seen = {normalize_kc_name(canonical)}
    aliases: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        alias = item.strip()
        key = normalize_kc_name(alias)
        if not alias or key in seen:
            continue
        seen.add(key)
        aliases.append(alias)
    return aliases


def _parse_vocab_payload(
    data: dict[str, Any],
) -> tuple[KcVocabulary, list[PrereqEdge]] | None:
    """校验并落地词表 payload; 结构不符 (含 stub 缺字段) 返回 None 触发重试。"""
    raw_kcs = data.get("kcs")
    if not isinstance(raw_kcs, list):
        return None
    vocab = KcVocabulary(entries=[])
    for item in raw_kcs:
        if not isinstance(item, dict):
            return None
        name = item.get("canonical_name")
        if not isinstance(name, str) or not name.strip():
            return None
        name = name.strip()
        description = item.get("description")
        if not isinstance(description, str):
            description = ""
        aliases = _clean_aliases(name, item.get("aliases"))
        existing = vocab.by_name(name)
        if existing is not None:
            # LLM 重复输出同一概念: 合并进已有条目而非另建 (保 kc_id 唯一)
            for alias in aliases:
                if vocab.by_name(alias) is None:
                    existing.aliases.append(alias)
            if not existing.description and description:
                existing.description = description.strip()
            continue
        vocab.entries.append(
            KcEntry(
                kc_id=str(uuid4()),
                canonical_name=name,
                aliases=[a for a in aliases if vocab.by_name(a) is None],
                description=description.strip(),
                embedding=None,
            )
        )

    edges: list[PrereqEdge] = []
    raw_edges = data.get("prerequisite_edges")
    if isinstance(raw_edges, list):
        for item in raw_edges:
            if not isinstance(item, dict):
                continue  # 边允许稀疏: 坏边跳过, 不整体报废
            from_name, to_name = item.get("from"), item.get("to")
            if not isinstance(from_name, str) or not isinstance(to_name, str):
                continue
            source = vocab.by_name(from_name)
            target = vocab.by_name(to_name)
            if source is None or target is None or source.kc_id == target.kc_id:
                continue
            # add_edge_safe 顺带完成环检测 (§5.8 验收 KC-A3), 成环边直接丢弃
            add_edge_safe(edges, PrereqEdge(from_kc=source.kc_id, to_kc=target.kc_id))
    return vocab, edges


def _parse_chunk_payload(data: dict[str, Any]) -> list[str] | None:
    """校验 chunk 抽取 payload: {"kcs": [str]}; 去重去空, 截断至 3 个。"""
    raw_kcs = data.get("kcs")
    if not isinstance(raw_kcs, list):
        return None
    names: list[str] = []
    seen: set[str] = set()
    for item in raw_kcs:
        if not isinstance(item, str):
            return None
        name = item.strip()
        key = normalize_kc_name(name)
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names[:3]


async def build_vocabulary(
    gateway: ModelGateway,
    kb_summary_text: str,
    *,
    tier: Tier = Tier.strong,
) -> tuple[KcVocabulary, list[PrereqEdge]]:
    """知识库级 KC 受控词表构建 (两遍式第一遍)。

    返回 (词表, 先修边列表); 边引用归一后的 kc_id 且保证无环。
    embedding 留空, 由 KcNormalizer 惰性补算; 持久化由调用方负责。
    """
    prompt = _load_prompt("kc_vocab_build.md").replace(
        "{{KB_SUMMARY_TEXT}}", kb_summary_text
    )
    request = ChatRequest(
        messages=(ChatMessage(role="user", content=prompt),),
        tier=tier,
        temperature=0.2,
        json_mode=True,
        feature="kc_vocab_build",
    )
    for _ in range(_MAX_ATTEMPTS):
        result = await gateway.chat(request)
        data = _parse_json_object(result.text)
        if data is None:
            continue
        parsed = _parse_vocab_payload(data)
        if parsed is not None:
            return parsed
    raise KcExtractionError("KC 词表构建输出无法解析为约定 JSON schema (已重试一次)")


async def extract_chunk_kcs(
    gateway: ModelGateway,
    chunk_text: str,
    vocab_names: list[str],
    *,
    tier: Tier = Tier.cheap,
) -> list[str]:
    """chunk 级知识点名称抽取 (≤3 个), 优先复用受控词表已有名称。

    只返回名称列表; kc_id 归并由调用方经 KcNormalizer.match_or_create 完成。
    """
    names_block = "\n".join(f"- {name}" for name in vocab_names) or "(词表暂为空)"
    prompt = (
        _load_prompt("kc_extract_chunk.md")
        .replace("{{VOCAB_NAMES}}", names_block)
        .replace("{{CHUNK_TEXT}}", chunk_text)
    )
    request = ChatRequest(
        messages=(ChatMessage(role="user", content=prompt),),
        tier=tier,
        temperature=0.0,
        json_mode=True,
        feature="kc_extract_chunk",
    )
    for _ in range(_MAX_ATTEMPTS):
        result = await gateway.chat(request)
        data = _parse_json_object(result.text)
        if data is None:
            continue
        names = _parse_chunk_payload(data)
        if names is not None:
            return names
    raise KcExtractionError("chunk KC 抽取输出无法解析为约定 JSON schema (已重试一次)")
