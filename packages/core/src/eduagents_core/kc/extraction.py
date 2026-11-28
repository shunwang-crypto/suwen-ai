"""KC LLM 抽取层 (§5.8): 全部经 ModelGateway, json_mode=True, 严格解析+一次重试。

- build_vocabulary: 知识库级受控词表 + 先修边 (strong 档, enrich 阶段一次性);
- extract_chunk_kcs: chunk 级知识点名称抽取 (cheap 档, 名称交给 KcNormalizer 归并)。
提示词从 仓库根/prompts/*.md 读取。解析失败重试一次, 仍失败抛 KcExtractionError;
stub 网关的 {"stub": true, ...} 缺业务字段, 同样按解析失败处理 (不崩裸异常)。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..gateway.base import ChatMessage, ChatRequest, ModelGateway, Tier
from ..json_utils import parse_json_object
from .dag import add_edge_safe
from .types import KcEntry, KcVocabulary, PrereqEdge, normalize_kc_name

# 本文件位于 packages/core/src/eduagents_core/kc/ -> parents[5] 为仓库根
_PROMPTS_DIR = Path(__file__).resolve().parents[5] / "prompts"
_MAX_ATTEMPTS = 2  # 首次 + 重试一次
# (词条上限, 先修边上限, 输出 token 上限)。重试使用同一教材，缩小输出。
_VOCAB_LIMITS = ((30, 24, 4800), (16, 12, 3000))
_KC_NAME = {"type": "string", "minLength": 1, "maxLength": 80}
_CHUNK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kcs", "section_quote"],
    "properties": {
        "kcs": {"type": "array", "maxItems": 3, "items": _KC_NAME},
        "section_quote": {"type": ["null", "string"], "maxLength": 500},
    },
}
_SECTION_PREFIX = re.compile(
    r"^\s*(?:第[\d一二三四五六七八九十百零〇]+[章节篇][\s:：、.-]*"
    r"|\d+(?:\.\d+)+(?:\.\s*|[\s:：、-]+)|\d+[.、:：]\s*"
    r"|[（(][\d一二三四五六七八九十]+[）)]\s*)"
)
_GENERIC_HEADINGS = {
    "来源", "图源", "资料来源", "图片来源", "版权", "版权页", "目录", "参考资料", "参考文献",
    "附录", "前言", "引言", "正文", "概述", "简介", "定义", "例题", "练习", "习题",
    "思考", "小结", "总结", "其他章节", "未分章节", "目次", "鸣谢",
    "source", "sources", "references", "contents", "preface", "introduction", "summary",
    "exercises", "examples", "appendix", "chapter",
}


class KcExtractionError(Exception):
    """LLM KC 抽取输出无法解析为约定 JSON schema (重试一次后仍失败)。"""


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _vocab_schema(max_kcs: int, max_edges: int) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["kcs", "prerequisite_edges"],
        "properties": {
            "kcs": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_kcs,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["canonical_name", "aliases", "description"],
                    "properties": {
                        "canonical_name": _KC_NAME,
                        "aliases": {"type": "array", "maxItems": 2, "items": _KC_NAME},
                        "description": {"type": "string", "maxLength": 50},
                    },
                },
            },
            "prerequisite_edges": {
                "type": "array",
                "maxItems": max_edges,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["from", "to"],
                    "properties": {"from": _KC_NAME, "to": _KC_NAME},
                },
            },
        },
    }


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
    data: dict[str, Any], *, max_kcs: int, max_edges: int,
) -> tuple[KcVocabulary, list[PrereqEdge]] | None:
    """校验并落地词表 payload; 结构不符 (含 stub 缺字段) 返回 None 触发重试。"""
    raw_kcs = data.get("kcs")
    if not isinstance(raw_kcs, list) or not 1 <= len(raw_kcs) <= max_kcs:
        return None
    vocab = KcVocabulary(entries=[])
    for item in raw_kcs:
        if not isinstance(item, dict):
            return None
        name = item.get("canonical_name")
        if not isinstance(name, str) or not normalize_kc_name(name):
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
        if len(raw_edges) > max_edges:
            return None
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


def section_heading_names(heading_path: str) -> tuple[str, str]:
    """Return the actual leaf heading and its name without section numbering."""
    leaf = heading_path.rsplit(" > ", 1)[-1].strip()
    return leaf, _SECTION_PREFIX.sub("", leaf).strip(" \t:：")


def section_topic_name(heading_path: str, document_name: str) -> str:
    """Select a candidate worth asking the model to verify against the body."""
    _, topic = section_heading_names(heading_path)
    key = normalize_kc_name(topic.strip("：:。.!?"))
    if (
        len(topic) > 80
        or not any(char.isalpha() for char in key)
        or key in _GENERIC_HEADINGS
        or topic.startswith("《") and topic.endswith("》")
        or key == normalize_kc_name(Path(document_name).stem)
        or key == normalize_kc_name(document_name)
        or re.match(r"^(?:来源|图源|资料来源|图片来源|sources?)\s*[:：]", topic, re.IGNORECASE)
    ):
        return ""
    return topic


def _confirmed_section_quote(value: object, text: str, title: str) -> bool:
    if not isinstance(value, str):
        return False
    quote = value.strip()
    if (
        len(quote) > 500
        or sum(char.isalnum() for char in quote) < 10
        or normalize_kc_name(section_heading_names(quote.strip("。.!?！？"))[1])
        == normalize_kc_name(title)
    ):
        return False
    boundaries = "\n。！？.!?；;"
    for match in re.finditer(re.escape(quote), text):
        before = text[:match.start()].rstrip(" \t")
        after = text[match.end():].lstrip(" \t")
        if (not before or before[-1] in boundaries) and (
            not after or quote[-1] in boundaries or after[0] in boundaries
        ):
            return True
    return False


def _parse_chunk_payload(data: dict[str, Any], excluded_names: set[str]) -> list[str] | None:
    """校验 chunk 抽取 payload: {"kcs": [str]}; 去重去空, 截断至 3 个。"""
    raw_kcs = data.get("kcs")
    if not isinstance(raw_kcs, list):
        return None
    names: list[str] = []
    seen = set(excluded_names)
    for item in raw_kcs:
        if not isinstance(item, str):
            return None
        name = item.strip()
        key = normalize_kc_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names[:3]


async def build_vocabulary(
    gateway: ModelGateway,
    kb_summary_text: str,
    *,
    tier: Tier = Tier.strong,
    tenant_id: str = "",
    user_id: str = "",
) -> tuple[KcVocabulary, list[PrereqEdge]]:
    """知识库级 KC 受控词表构建 (两遍式第一遍)。

    返回 (词表, 先修边列表); 边引用归一后的 kc_id 且保证无环。
    embedding 留空, 由 KcNormalizer 惰性补算; 持久化由调用方负责。
    tenant_id/user_id 必须透传 (P0-2a): 缺省空串会被 usage sink 丢弃 = 不计量不扣预算。
    """
    template = _load_prompt("kc_vocab_build.md")
    failures = []
    for attempt, (max_kcs, max_edges, max_tokens) in enumerate(_VOCAB_LIMITS):
        prompt = (
            template.replace("{{MAX_KCS}}", str(max_kcs))
            .replace("{{MAX_EDGES}}", str(max_edges))
            .replace("{{KB_SUMMARY_TEXT}}", kb_summary_text)
        )
        if attempt:
            prompt += (
                "\n上一次没有得到有效的词表 JSON。"
                "这次只提取教材中最关键的知识点，描述使用简短短语，省略不确定的先修边。"
            )
        request = ChatRequest(
            messages=(ChatMessage(role="user", content=prompt),),
            tier=tier,
            temperature=0.0,
            max_tokens=max_tokens,
            json_mode=True,
            response_schema=_vocab_schema(max_kcs, max_edges),
            feature="kc_vocab_build",
            tenant_id=tenant_id,
            user_id=user_id,
        )
        result = await gateway.chat(request)
        if result.finish_reason == "length":
            failures.append(f"token_limit({result.completion_tokens})")
            continue
        data = parse_json_object(result.text)
        if data is None:
            failures.append(f"invalid_json({result.completion_tokens})")
            continue
        parsed = _parse_vocab_payload(data, max_kcs=max_kcs, max_edges=max_edges)
        if parsed is not None:
            return parsed
        failures.append(f"invalid_vocabulary({result.completion_tokens})")
    raise KcExtractionError("KC 词表构建未得到有效结果 (已重试一次): " + ", ".join(failures))


async def extract_chunk_kcs(
    gateway: ModelGateway,
    chunk_text: str,
    vocab_names: list[str],
    *,
    heading_path: str = "",
    document_name: str = "",
    tier: Tier = Tier.cheap,
    tenant_id: str = "",
    user_id: str = "",
) -> list[str]:
    """抽取至多三个细粒度知识点，加上一个由 child 正文证实的小节主题。

    只返回名称列表; kc_id 归并由调用方经 KcNormalizer.match_or_create 完成。
    tenant_id/user_id 必须透传 (P0-2a): 缺省空串会被 usage sink 丢弃 = 不计量不扣预算。
    """
    heading_names = section_heading_names(heading_path)
    section_name = section_topic_name(heading_path, document_name)
    excluded = {normalize_kc_name(name) for name in heading_names}
    names_block = "\n".join(f"- {name}" for name in vocab_names) or "(词表暂为空)"
    prompt = (
        _load_prompt("kc_extract_chunk.md")
        .replace("{{VOCAB_NAMES}}", names_block)
        .replace("{{SECTION_TITLE}}", section_name or "(无候选标题)")
        .replace("{{CHUNK_TEXT}}", chunk_text)
    )
    request = ChatRequest(
        messages=(ChatMessage(role="user", content=prompt),),
        tier=tier,
        temperature=0.0,
        max_tokens=800,
        json_mode=True,
        response_schema=_CHUNK_SCHEMA,
        feature="kc_extract_chunk",
        tenant_id=tenant_id,
        user_id=user_id,
    )
    for _ in range(_MAX_ATTEMPTS):
        result = await gateway.chat(request)
        if result.finish_reason == "length":
            continue
        data = parse_json_object(result.text)
        if data is None:
            continue
        names = _parse_chunk_payload(data, excluded)
        if names is not None:
            if section_name and _confirmed_section_quote(
                data.get("section_quote"), chunk_text, section_name
            ):
                names.append(section_name)
            return names
    raise KcExtractionError("chunk KC 抽取输出无法解析为约定 JSON schema (已重试一次)")
