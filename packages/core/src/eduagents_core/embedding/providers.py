"""Dense embedding providers (M2).

- StubEmbedder: 确定性哈希向量, 零网络, dev/CI 专用 (embedding_mode=stub)。
- SiliconFlowEmbedder: OpenAI 兼容 /embeddings 端点 (BGE-M3), embedding_mode=live。
- create_embedder: 按 settings.embedding_mode 选择实现。
"""

from __future__ import annotations

import asyncio
import math
from functools import lru_cache
from typing import Any

import httpx
import jieba
import xxhash

from ..config import Settings
from .base import EmbeddingProvider

# --- shared retry helper (embeddings 与 rerank 复用) ---

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 0.5
_REQUEST_TIMEOUT_SECONDS = 30.0


async def _post_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST JSON; 429/5xx 与网络错误指数退避重试 _MAX_RETRIES 次, 其余 4xx 立即抛。"""
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.TransportError as exc:
            last_exc = exc
        else:
            if resp.status_code < 400:
                return resp.json()
            if resp.status_code != 429 and resp.status_code < 500:
                resp.raise_for_status()  # 非 429 的 4xx: 不可重试
            last_exc = httpx.HTTPStatusError(
                f"retryable status {resp.status_code} from {url}",
                request=resp.request,
                response=resp,
            )
        if attempt < _MAX_RETRIES:
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
    raise last_exc


# --- stub embedder ---

# PCG 论文里的标准 64 位 LCG 常数; 用 token 的 xxhash 值播种,
# 完全无 random 全局态, 同 token 恒同向量, 跨进程可复现。
_LCG_MULT = 6364136223846793005
_LCG_INC = 1442695040888963407
_MASK64 = (1 << 64) - 1


@lru_cache(maxsize=8192)
def _token_vector(token: str, dim: int) -> tuple[float, ...]:
    """token -> dim 维伪随机向量, 值域 [-1, 1), xxhash 播种的 LCG 展开。"""
    state = xxhash.xxh64_intdigest(token.encode())
    out: list[float] = []
    for _ in range(dim):
        state = (state * _LCG_MULT + _LCG_INC) & _MASK64
        # 取高 53 位映射到 [-1, 1)
        out.append((state >> 11) / float(1 << 52) - 1.0)
    return tuple(out)


class StubEmbedder:
    """确定性哈希向量 (dev/CI): jieba 分词, 逐 token 伪随机向量求和后 L2 归一化。

    同文本必然同向量; 有词重叠的文本因共享 token 向量而有非零相似度;
    空文本 (无有效 token) 返回全零向量, 避免除零。
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        acc = [0.0] * self.dim
        for token in jieba.lcut(text):
            token = token.strip()
            if not token:
                continue
            vec = _token_vector(token, self.dim)
            for i in range(self.dim):
                acc[i] += vec[i]
        norm = math.sqrt(sum(x * x for x in acc))
        if norm == 0.0:
            return acc
        return [x / norm for x in acc]


# --- live embedder ---


class SiliconFlowEmbedder:
    """SiliconFlow BGE-M3, OpenAI 兼容 POST {base_url}/embeddings。

    批量 ≤32 一批; 429/5xx 指数退避重试 3 次; 响应按 data[].index 排序,
    保证返回向量与输入文本一一对应。
    """

    _BATCH_SIZE = 32

    def __init__(self, settings: Settings) -> None:
        self.dim = settings.embedding_dim
        self._model = settings.embedding_model
        self._url = settings.embedding_base_url.rstrip("/") + "/embeddings"
        self._headers = {"Authorization": f"Bearer {settings.embedding_api_key}"}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            for start in range(0, len(texts), self._BATCH_SIZE):
                batch = texts[start : start + self._BATCH_SIZE]
                data = await _post_json_with_retry(
                    client,
                    self._url,
                    self._headers,
                    {"model": self._model, "input": batch},
                )
                items = sorted(data["data"], key=lambda item: item["index"])
                vectors.extend([float(x) for x in item["embedding"]] for item in items)
        return vectors


def create_embedder(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_mode == "stub":
        return StubEmbedder(settings.embedding_dim)
    return SiliconFlowEmbedder(settings)
