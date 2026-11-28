"""AnswerCache 单测 — L1 fakeredis + L2 Qdrant 本地嵌入模式 (零服务依赖)。

覆盖: L1 精确命中(不触发向量化)/归一化等价, L2 语义命中, 0.95 阈值,
数字(阿拉伯+中文)不一致拒用, 否定词不一致拒用, kb_version 失效, 租户隔离,
7 天清理, L1 TTL。
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta

import pytest
from eduagents_core.cache.answer_cache import (
    L1_TTL_SECONDS,
    AnswerCache,
    guard_consistent,
    normalize_query,
)
from eduagents_core.config import Settings
from fakeredis import FakeAsyncRedis

DIM = 4
TENANT = "tenant-a"
KB = "kb-1"


def _unit(x: float) -> list[float]:
    """与 [1,0,0,0] 余弦相似度恰为 x 的单位向量。"""
    return [x, math.sqrt(max(0.0, 1.0 - x * x)), 0.0, 0.0]


BASE = [1.0, 0.0, 0.0, 0.0]

# key = normalize_query 后的文本 (本表所有查询归一化都是恒等变换)
VECTORS = {
    "牛顿第一定律是什么": BASE,
    "牛顿第一定律是啥": _unit(0.96),  # 阈值之上 -> 语义命中
    "热力学第一定律是什么": _unit(0.90),  # 阈值之下 -> miss
    "牛顿第三定律是什么": BASE,  # 余弦 1.0 但中文数字不同 -> 复核拒用
    "第1章讲了什么": BASE,
    "第2章讲了什么": BASE,  # 阿拉伯数字不同 -> 复核拒用
    "光速可以被超过吗": BASE,
    "光速不可以被超过吗": BASE,  # 否定词只在一边 -> 复核拒用
}


class MapEmbedder:
    dim = DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(VECTORS[t]) for t in texts]


class ExplodingEmbedder:
    dim = DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("L1 精确命中不应触发向量化")


class BlockingEmbedder:
    dim = DIM

    def __init__(self) -> None:
        self.cancelled = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.fixture
async def redis():
    r = FakeAsyncRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def cache(tmp_path, redis):
    settings = Settings(
        _env_file=None, qdrant_local_path=str(tmp_path / "qdrant"), embedding_dim=DIM
    )
    c = AnswerCache(settings, redis=redis)
    yield c
    c.close()


async def _put(cache: AnswerCache, query: str, *, kb_version: int = 1, **kwargs) -> None:
    defaults = dict(
        answer=f"answer of {query}",
        citations=[{"chunk_id": "c1", "quote": "引文"}],
        followups=["追问一", "追问二"],
        model="deepseek-chat",
        tier="standard",
        completion_tokens=120,
    )
    defaults.update(kwargs)
    await cache.put(TENANT, KB, kb_version, query, MapEmbedder(), **defaults)


async def test_l1_exact_hit_without_embedding(cache):
    await _put(cache, "牛顿第一定律是什么")
    hit = await cache.get(TENANT, KB, 1, "牛顿第一定律是什么", ExplodingEmbedder())
    assert hit is not None
    assert hit.source == "exact"
    assert hit.answer == "answer of 牛顿第一定律是什么"
    assert hit.citations == [{"chunk_id": "c1", "quote": "引文"}]
    assert hit.followups == ["追问一", "追问二"]
    assert hit.tier == "standard"
    assert hit.completion_tokens == 120


async def test_l1_exact_hit_is_not_subject_to_semantic_timeout(cache):
    await _put(cache, "牛顿第一定律是什么")
    hit = await cache.get(
        TENANT,
        KB,
        1,
        "牛顿第一定律是什么",
        ExplodingEmbedder(),
        semantic_timeout_seconds=0,
    )
    assert hit is not None and hit.source == "exact"


async def test_l2_timeout_is_a_cache_miss_and_cancels_embedding(cache):
    embedder = BlockingEmbedder()
    hit = await cache.get(
        TENANT,
        KB,
        1,
        "牛顿第一定律是什么",
        embedder,
        semantic_timeout_seconds=0.01,
    )
    assert hit is None
    assert embedder.cancelled is True


async def test_l1_normalization_equivalent_queries(cache):
    # 空白/标点/全角差异归一化后同 key, 仍走 L1 精确命中
    await _put(cache, "牛顿第一定律是什么")
    hit = await cache.get(TENANT, KB, 1, "  牛顿第一定律是什么？ ", ExplodingEmbedder())
    assert hit is not None and hit.source == "exact"
    assert normalize_query("牛顿第一定律是什么？") == normalize_query(" 牛顿第一定律是什么")


async def test_l1_ttl_is_24h(cache, redis):
    await _put(cache, "牛顿第一定律是什么")
    ttl = await redis.ttl(AnswerCache.l1_key(TENANT, KB, 1, "牛顿第一定律是什么"))
    assert 0 < ttl <= L1_TTL_SECONDS


async def test_l2_semantic_hit_above_threshold(cache):
    await _put(cache, "牛顿第一定律是什么")
    hit = await cache.get(TENANT, KB, 1, "牛顿第一定律是啥", MapEmbedder())
    assert hit is not None
    assert hit.source == "semantic"
    assert hit.matched_query == "牛顿第一定律是什么"
    assert hit.score >= 0.95
    assert hit.answer == "answer of 牛顿第一定律是什么"


async def test_l2_below_threshold_is_miss(cache):
    await _put(cache, "牛顿第一定律是什么")
    assert await cache.get(TENANT, KB, 1, "热力学第一定律是什么", MapEmbedder()) is None


async def test_chinese_numeral_mismatch_rejected(cache):
    # 验收线: "牛顿第一/第三定律"不互相命中 — 向量余弦 1.0 仍被数字复核拒用
    await _put(cache, "牛顿第一定律是什么")
    assert await cache.get(TENANT, KB, 1, "牛顿第三定律是什么", MapEmbedder()) is None


async def test_arabic_digit_mismatch_rejected(cache):
    await _put(cache, "第1章讲了什么")
    assert await cache.get(TENANT, KB, 1, "第2章讲了什么", MapEmbedder()) is None
    assert not guard_consistent("第1章讲了什么", "第2章讲了什么")


async def test_negation_mismatch_rejected(cache):
    await _put(cache, "光速可以被超过吗")
    assert await cache.get(TENANT, KB, 1, "光速不可以被超过吗", MapEmbedder()) is None
    assert not guard_consistent("光速可以被超过吗", "光速不可以被超过吗")


async def test_kb_version_invalidates_both_levels(cache):
    await _put(cache, "牛顿第一定律是什么", kb_version=1)
    # 同版本命中
    assert await cache.get(TENANT, KB, 1, "牛顿第一定律是什么", MapEmbedder()) is not None
    # 版本+1: L1 key 不同, L2 filter 不同 -> 天然失效 (精确与语义路径都 miss)
    assert await cache.get(TENANT, KB, 2, "牛顿第一定律是什么", MapEmbedder()) is None
    assert await cache.get(TENANT, KB, 2, "牛顿第一定律是啥", MapEmbedder()) is None


async def test_tenant_isolation(cache):
    await _put(cache, "牛顿第一定律是什么")
    assert await cache.get("tenant-b", KB, 1, "牛顿第一定律是什么", MapEmbedder()) is None
    assert await cache.get(TENANT, "kb-other", 1, "牛顿第一定律是什么", MapEmbedder()) is None


async def test_cleanup_deletes_only_stale_points(tmp_path):
    # 无 L1 (redis=None), 纯 L2: 8 天前的点被清, 新点保留
    settings = Settings(
        _env_file=None, qdrant_local_path=str(tmp_path / "qdrant"), embedding_dim=DIM
    )
    cache = AnswerCache(settings, redis=None)
    try:
        now = datetime.now(UTC)
        await cache.put(
            TENANT, KB, 1, "牛顿第一定律是什么", MapEmbedder(),
            answer="stale", at=now - timedelta(days=8),
        )
        await cache.put(
            TENANT, KB, 1, "光速可以被超过吗", MapEmbedder(), answer="fresh", at=now,
        )
        deleted = await cache.cleanup_older_than(now - timedelta(days=7))
        assert deleted == 1
        assert await cache.get(TENANT, KB, 1, "牛顿第一定律是什么", MapEmbedder()) is None
        hit = await cache.get(TENANT, KB, 1, "光速可以被超过吗", MapEmbedder())
        assert hit is not None and hit.answer == "fresh"
        # 幂等: 再清一次无点可删
        assert await cache.cleanup_older_than(now - timedelta(days=7)) == 0
    finally:
        cache.close()


async def test_formulae_keep_grouping_and_fill_once(cache):
    assert cache.l1_key(TENANT, KB, 1, "2*(3+4)") != cache.l1_key(TENANT, KB, 1, "2*3+4")
    assert not guard_consistent("2*(3+4)", "2*3+4")
    assert not guard_consistent("x < Y", "x < y")
    calls = 0

    async def ask():
        nonlocal calls
        async with cache.filling(TENANT, KB, 1, "牛顿第一定律是什么"):
            hit = await cache.get(TENANT, KB, 1, "牛顿第一定律是什么", MapEmbedder(), semantic_timeout_seconds=0)
            if hit:
                return hit.answer
            calls += 1
            await asyncio.sleep(0)
            await _put(cache, "牛顿第一定律是什么")
            return "answer of 牛顿第一定律是什么"
    assert len(set(await asyncio.gather(*(ask() for _ in range(20))))) == 1
    assert calls == 1
