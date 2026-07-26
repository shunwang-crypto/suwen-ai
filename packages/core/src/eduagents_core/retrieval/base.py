from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    heading_path: str
    doc_id: str
    page_start: int | None
    page_end: int | None
    score: float
    source: str  # dense | sparse | fused
    rerank_score: float | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk]
    # 过程可观测 (检索调试面板 + trace_event 用)
    dense_hits: int = 0
    sparse_hits: int = 0
    used_sparse: bool = False
    used_rerank: bool = False
    confident: bool = False  # top 结果均分 >= 阈值 -> 可走快路径 (§5.3)


def rrf_fuse(
    rankings: list[list[str]], k: int = 60
) -> dict[str, float]:
    """Reciprocal Rank Fusion: score(d) = Σ 1/(k + rank_i(d))。
    rankings: 每路召回的有序 id 列表。返回 id -> 融合分。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return scores
