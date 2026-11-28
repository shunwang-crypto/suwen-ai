"""M2 嵌入/稀疏编码/精排单测 — 全部离线, 零网络。"""

from __future__ import annotations

import math

import httpx
import pytest
import xxhash
from eduagents_core.config import Settings
from eduagents_core.embedding.providers import (
    SiliconFlowEmbedder,
    StubEmbedder,
    create_embedder,
)
from eduagents_core.embedding.rerankers import (
    SiliconFlowReranker,
    StubReranker,
    create_reranker,
)
from eduagents_core.embedding.sparse import Bm25SparseEncoder


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# --- StubEmbedder ---


async def test_stub_embedder_deterministic_and_normalized():
    settings = make_settings(embedding_mode="stub", embedding_dim=64)
    embedder = create_embedder(settings)
    assert isinstance(embedder, StubEmbedder)

    v1, v2, v3 = await embedder.embed(
        ["泰勒公式的余项", "泰勒公式的余项", "拉格朗日中值定理"]
    )
    assert v1 == v2  # 同文本必然同向量
    assert v1 != v3  # 不同文本不同向量
    assert len(v1) == settings.embedding_dim
    assert len(v3) == settings.embedding_dim
    assert math.isclose(math.sqrt(sum(x * x for x in v1)), 1.0, rel_tol=1e-9)
    assert math.isclose(math.sqrt(sum(x * x for x in v3)), 1.0, rel_tol=1e-9)


async def test_stub_embedder_empty_text_is_zero_vector():
    embedder = StubEmbedder(dim=32)
    (vec,) = await embedder.embed([""])
    assert vec == [0.0] * 32


async def test_stub_embedder_word_overlap_gives_higher_similarity():
    embedder = StubEmbedder(dim=64)
    base, overlap, unrelated = await embedder.embed(
        ["泰勒公式的余项", "泰勒公式的证明", "食堂今天供应红烧肉"]
    )
    assert _dot(base, overlap) > _dot(base, unrelated)
    assert _dot(base, overlap) > 0.0


async def test_create_embedder_live_mode_returns_siliconflow():
    settings = make_settings(embedding_mode="live", embedding_api_key="sk-test")
    embedder = create_embedder(settings)
    assert isinstance(embedder, SiliconFlowEmbedder)
    assert embedder.dim == settings.embedding_dim


# --- Bm25SparseEncoder ---


def test_sparse_chinese_segmentation_and_stable_indices():
    encoder = Bm25SparseEncoder()
    query = encoder.encode_query("泰勒公式的余项")
    # 分词后 ≥2 个词项 (停用词 "的" 被过滤)
    assert len(query) >= 2
    assert xxhash.xxh32_intdigest("的".encode()) not in query
    # 同 token 跨调用索引稳定, 且 doc 端与 query 端映射到同一索引
    assert query == encoder.encode_query("泰勒公式的余项")
    doc = encoder.encode_document("泰勒公式的余项")
    assert set(doc) == set(query)
    assert xxhash.xxh32_intdigest("泰勒".encode()) in query


def test_sparse_document_tf_sublinear():
    encoder = Bm25SparseEncoder()
    idx = xxhash.xxh32_intdigest("泰勒".encode())
    w1 = encoder.encode_document("泰勒")[idx]
    w3 = encoder.encode_document("泰勒 泰勒 泰勒")[idx]
    assert w3 > w1  # tf 增加权重增加
    assert w3 < 3 * w1  # 但次线性 (BM25 饱和)


def test_sparse_query_weights_all_one():
    encoder = Bm25SparseEncoder()
    query = encoder.encode_query("泰勒公式的余项估计与拉格朗日中值定理")
    assert query
    assert all(weight == 1.0 for weight in query.values())


# --- Reranker ---


async def test_stub_reranker_relevant_doc_scores_higher():
    reranker = create_reranker(make_settings(rerank_mode="stub"))
    assert isinstance(reranker, StubReranker)
    scores = await reranker.rerank(
        "泰勒公式的余项",
        ["泰勒公式的余项如何估计", "食堂今天供应红烧肉"],
    )
    assert len(scores) == 2
    assert scores[0] > scores[1]
    assert all(0.0 <= s <= 1.0 for s in scores)


async def test_create_reranker_live_mode_returns_siliconflow():
    reranker = create_reranker(make_settings(rerank_mode="live"))
    assert isinstance(reranker, SiliconFlowReranker)
    assert await SiliconFlowReranker(make_settings()).rerank("q", []) == []


async def test_ollama_embedding_keeps_batch_order_and_rejects_wrong_space(monkeypatch):
    import json

    settings = make_settings(embedding_mode="ollama", qdrant_collection="school")
    provider = create_embedder(settings)
    batches = []

    def respond(request):
        batch = json.loads(request.content)["input"]
        batches.append(batch)
        return httpx.Response(200, json={"embeddings": [[int(text)] * (1 if text == "9" else 1024)
                                                        for text in batch]})

    factory = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: factory(transport=httpx.MockTransport(respond), **kw))
    vectors = await provider.embed(["1", "2", "3", "4", "5"])
    assert batches == [["1", "2", "3", "4"], ["5"]]
    assert [v[0] for v in vectors] == [1, 2, 3, 4, 5]
    assert settings.retired_chunk_collection == "school" and settings.qdrant_collection != "school"
    with pytest.raises(ValueError, match="dimension"):
        await provider.embed(["9"])


@pytest.mark.parametrize("resident", [4096, 2048, 0])
async def test_strict_gpu_checks_each_loaded_model_and_context(monkeypatch, resident):
    import json

    from eduagents_core.gateway.ollama import verify_ollama_gpu

    settings = make_settings(
        gateway_mode="live", embedding_mode="ollama", ollama_chat_model="local-qa",
        ollama_require_gpu=True, harness_context_window_tokens=16384,
    )
    loaded = []

    def respond(request):
        if request.method == "POST":
            model = json.loads(request.content)["model"]
            loaded.append((request.url.path, model))
            return httpx.Response(200, json={"model": model})
        row = {"name": loaded[-1][1], "size": 4096, "size_vram": resident, "context_length": 8192}
        return httpx.Response(200, json={"models": [row]})

    factory = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: factory(transport=httpx.MockTransport(respond), **kw))
    if resident < 4096:
        with pytest.raises(RuntimeError, match="fully resident"):
            await verify_ollama_gpu(settings)
        return
    verified = await verify_ollama_gpu(settings)
    assert loaded == [("/api/embed", settings.embedding_model), ("/api/generate", "local-qa")]
    assert set(verified) == {settings.embedding_model, "local-qa"}
    assert settings.harness_context_window_tokens == 8192
