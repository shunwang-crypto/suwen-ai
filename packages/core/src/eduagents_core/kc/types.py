"""KC 知识点体系数据类型 (§5.8)。

受控词表 (KcVocabulary) 是两遍式 KC 归一化的核心数据结构:
- 第一遍: 知识库级构建 (extraction.build_vocabulary, strong 档一次性);
- 第二遍: chunk 级抽取先与词表匹配 (normalizer.KcNormalizer)。
出题/测评/画像全部外键引用归一后的 kc_id, 禁止裸字符串。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

# NFKC 之后仍可能残留的各种"中间点"变体 (泰勒·公式 == 泰勒公式)
_MIDDLE_DOTS = "·•‧∙⋅・"


def normalize_kc_name(name: str) -> str:
    """KC 名称匹配用归一化: NFKC (全角转半角) -> 去中间点 -> 去全部空白 -> 小写化。

    仅用于匹配比较, 不改变存储的原始表述。
    """
    text = unicodedata.normalize("NFKC", name)
    for dot in _MIDDLE_DOTS:
        text = text.replace(dot, "")
    text = "".join(text.split())
    return text.lower()


@dataclass
class KcEntry:
    """受控词表中的单个知识点。embedding 允许为 None, 由 KcNormalizer 惰性补算。"""

    kc_id: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    embedding: list[float] | None = None


@dataclass
class KcVocabulary:
    """知识库级 KC 受控词表。纯内存结构, DB 持久化由调用方负责。"""

    entries: list[KcEntry] = field(default_factory=list)

    def by_name(self, name: str) -> KcEntry | None:
        """canonical 与 aliases 全量精确匹配 (双方都先做 normalize_kc_name)。"""
        key = normalize_kc_name(name)
        if not key:
            return None
        for entry in self.entries:
            if normalize_kc_name(entry.canonical_name) == key:
                return entry
            for alias in entry.aliases:
                if normalize_kc_name(alias) == key:
                    return entry
        return None

    def to_payload(self) -> dict[str, Any]:
        return {
            "entries": [
                {
                    "kc_id": e.kc_id,
                    "canonical_name": e.canonical_name,
                    "aliases": list(e.aliases),
                    "description": e.description,
                    "embedding": e.embedding,
                }
                for e in self.entries
            ]
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> KcVocabulary:
        entries: list[KcEntry] = []
        for item in payload.get("entries", []):
            raw_embedding = item.get("embedding")
            entries.append(
                KcEntry(
                    kc_id=str(item["kc_id"]),
                    canonical_name=str(item["canonical_name"]),
                    aliases=[str(a) for a in item.get("aliases", [])],
                    description=str(item.get("description", "")),
                    embedding=(
                        [float(x) for x in raw_embedding]
                        if raw_embedding is not None
                        else None
                    ),
                )
            )
        return cls(entries=entries)


@dataclass(frozen=True)
class PrereqEdge:
    """KC 先修关系边: 必须先掌握 from_kc 才能学 to_kc。"""

    from_kc: str
    to_kc: str
