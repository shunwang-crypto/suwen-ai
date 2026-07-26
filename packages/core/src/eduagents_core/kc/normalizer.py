"""KC 归并器 (§5.8 第二遍): 词表匹配 -> embedding 余弦归并 -> 新建。

防止 LLM 逐 chunk 自由抽取产生"泰勒公式/Taylor公式/泰勒展开"碎片 KC
击穿 BKT 外键。纯内存操作 (直接改 vocab), DB 持久化由调用方负责。
"""

from __future__ import annotations

import math
from uuid import uuid4

from ..embedding.base import EmbeddingProvider
from .types import KcEntry, KcVocabulary


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度; 任一侧零向量或维度不一致按 0 处理 (视为不相似)。"""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class KcNormalizer:
    """三级归并: a) 归一化精确/别名命中 b) 余弦 >= 阈值归并追加别名 c) 新建 KC。"""

    def __init__(
        self,
        vocab: KcVocabulary,
        embedder: EmbeddingProvider,
        merge_threshold: float = 0.92,
    ) -> None:
        self.vocab = vocab
        self._embedder = embedder
        self._merge_threshold = merge_threshold

    async def match_or_create(self, name: str, description: str = "") -> tuple[str, bool]:
        """返回 (kc_id, 是否新建)。归并时把原始 name 追加进命中 entry 的 aliases。"""
        # a) canonical/aliases 归一化精确命中
        hit = self.vocab.by_name(name)
        if hit is not None:
            return hit.kc_id, False

        # b) embedding 余弦归并
        vector = (await self._embedder.embed([name]))[0]
        await self._ensure_entry_embeddings()
        best_entry: KcEntry | None = None
        best_score = -1.0
        for entry in self.vocab.entries:
            if entry.embedding is None:
                continue
            score = cosine_similarity(vector, entry.embedding)
            if score > best_score:
                best_score, best_entry = score, entry
        if best_entry is not None and best_score >= self._merge_threshold:
            if name not in best_entry.aliases:
                best_entry.aliases.append(name)
            return best_entry.kc_id, False

        # c) 无匹配 -> 新建 (复用刚算的向量)
        entry = KcEntry(
            kc_id=str(uuid4()),
            canonical_name=name,
            aliases=[],
            description=description,
            embedding=vector,
        )
        self.vocab.entries.append(entry)
        return entry.kc_id, True

    async def _ensure_entry_embeddings(self) -> None:
        """词表 entry 缺 embedding 时按 canonical_name 批量惰性补算并缓存回 entry。"""
        missing = [e for e in self.vocab.entries if e.embedding is None]
        if not missing:
            return
        vectors = await self._embedder.embed([e.canonical_name for e in missing])
        for entry, vector in zip(missing, vectors, strict=True):
            entry.embedding = vector
