"""Rerankers (M2 精排)。

- StubReranker: 确定性词面重合分 (Jaccard), 零网络, rerank_mode=stub。
- SiliconFlowReranker: POST {base_url}/rerank (bge-reranker-v2-m3), rerank_mode=live。
- create_reranker: 按 settings.rerank_mode 选择实现。
"""

from __future__ import annotations

import httpx
import jieba

from ..config import Settings
from .base import Reranker
from .providers import _REQUEST_TIMEOUT_SECONDS, _post_json_with_retry


class StubReranker:
    """确定性精排分: query 与 doc 的 jieba 词项集合 Jaccard 重合度 (0-1)。"""

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        query_tokens = self._token_set(query)
        return [self._jaccard(query_tokens, self._token_set(doc)) for doc in documents]

    @staticmethod
    def _token_set(text: str) -> set[str]:
        return {t.strip().lower() for t in jieba.lcut(text) if t.strip()}

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)


class SiliconFlowReranker:
    """SiliconFlow rerank 端点; 解析 results[].relevance_score, 按 documents 原顺序返回。

    429/5xx 指数退避重试 3 次 (复用 providers._post_json_with_retry)。
    """

    def __init__(self, settings: Settings) -> None:
        self._model = settings.rerank_model
        self._url = settings.embedding_base_url.rstrip("/") + "/rerank"
        self._headers = {"Authorization": f"Bearer {settings.embedding_api_key}"}

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            data = await _post_json_with_retry(
                client,
                self._url,
                self._headers,
                {"model": self._model, "query": query, "documents": documents},
            )
        scores = [0.0] * len(documents)
        for item in data.get("results", []):
            scores[int(item["index"])] = float(item["relevance_score"])
        return scores


def create_reranker(settings: Settings) -> Reranker:
    if settings.rerank_mode == "stub":
        return StubReranker()
    return SiliconFlowReranker(settings)
