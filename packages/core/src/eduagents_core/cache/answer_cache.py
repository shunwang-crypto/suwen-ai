"""两级语义答案缓存 (Wave1-包B, ACTION_PLAN 技术决策表"语义缓存")。

- L1 精确: Redis, key = anscache:sha256(tenant|kb_id|kb_version|归一化query),
  value = JSON {answer, citations, followups, model, tier, completion_tokens, at},
  TTL 24h。tier/completion_tokens 是在 payload 规格之上补充的字段: 命中时主线写
  usage_log(mode='cache', cost_cny=0, cached_tokens=completion_tokens, tier=原 tier),
  ops 面板按 tier 输出单价聚合估算"缓存节省"。
- L2 语义: Qdrant 独立 collection "answer_cache" (dense only, dim=settings.embedding_dim),
  payload = L1 value + query 原文 + tenant_id/kb_id/kb_version + at_ts (unix 秒, 供清理)。
  查询: 同租户同库同版本 filter + 余弦 ≥0.95, 命中后数字/否定词正则复核, 不一致拒用。
  kb_version 进 filter -> 重新摄取后旧缓存天然失效, 无需主动删。
- 只应缓存自省环通过后的最终答案, 且不缓存混入学情个性化上下文的回答 (调用方约束)。
- 本模块不依赖 FastAPI; redis 为可选注入 (None 时只走 L2, 供 worker 清理任务复用)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from qdrant_client import models

from ..config import Settings
from ..embedding.base import EmbeddingProvider
from ..logging import get_logger

# 复用 chunks 库的进程级 client 单例: 本地嵌入模式同一路径只允许一个客户端 (文件锁),
# answer_cache collection 必须与 eduagents_chunks 共享同一个 client。
from ..vectorstore.qdrant_store import _get_client

log = get_logger("answer_cache")

COLLECTION = "answer_cache"
L1_TTL_SECONDS = 24 * 3600
SCORE_THRESHOLD = 0.95
CLEANUP_MAX_AGE_DAYS = 7

# 归一化只去空白与标点 (不动数字/否定词, 它们是复核的信号)
_PUNCT = set("，。！？!?,.;；:：、\"'“”‘’()（）[]【】《》<>…·~～")
# 数字序列: ASCII 数字 (NFKC 后全角已折叠) + 中文数字 ("牛顿第一/第三定律"不互相命中)
_NUM_RE = re.compile(r"[0-9]+|[〇零一二三四五六七八九十百千万亿两]+")
_NEG_WORDS = ("不", "没", "非", "无", "别")


def normalize_query(query: str) -> str:
    """NFKC 折叠 + casefold + 去所有空白与常见标点。数字与否定词原样保留。"""
    text = unicodedata.normalize("NFKC", query).casefold()
    return "".join(ch for ch in text if not ch.isspace() and ch not in _PUNCT)


def guard_consistent(query_a: str, query_b: str) -> bool:
    """语义命中后的正则复核: 数字序列不等, 或否定词只出现在一边 -> 拒用 (False)。

    BGE-M3 对中文短 query 的 0.95 阈值偏松, "牛顿第一定律" vs "牛顿第三定律"
    可能超阈值, 这道复核是最后防线。
    """
    norm_a, norm_b = normalize_query(query_a), normalize_query(query_b)
    if _NUM_RE.findall(norm_a) != _NUM_RE.findall(norm_b):
        return False
    for word in _NEG_WORDS:
        if (word in norm_a) != (word in norm_b):
            return False
    return True


@dataclass
class CacheHit:
    answer: str
    citations: list[dict] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    model: str = ""
    tier: str = "standard"
    completion_tokens: int = 0
    at: str = ""  # 原答案写入时刻 (ISO)
    source: str = "exact"  # exact (L1) | semantic (L2)
    matched_query: str = ""  # L2 命中时 = 缓存里的原 query; L1 = 本次 query
    score: float = 1.0


def _key_material(tenant_id: str, kb_id: str, kb_version: int, normalized: str) -> str:
    return hashlib.sha256(
        f"{tenant_id}|{kb_id}|{kb_version}|{normalized}".encode()
    ).hexdigest()


class AnswerCache:
    """接口不依赖 FastAPI; redis=None 时跳过 L1 (仅清理任务需要)。"""

    def __init__(self, settings: Settings, redis: Any = None) -> None:
        self._s = settings
        self._redis = redis
        self._dim = settings.embedding_dim
        self._local = bool(settings.qdrant_local_path)
        self._client = _get_client(settings)
        self._ready = False

    def close(self) -> None:
        """关闭共享 client (与 QdrantStore 共用, 只应在进程退出/测试清理时调用)。"""
        from ..vectorstore.qdrant_store import _client_cache

        for key, value in list(_client_cache.items()):
            if value is self._client:
                _client_cache.pop(key, None)
        self._client.close()

    # ---------- keys ----------

    @staticmethod
    def l1_key(tenant_id: str, kb_id: str, kb_version: int, query: str) -> str:
        return "anscache:" + _key_material(
            tenant_id, kb_id, int(kb_version), normalize_query(query)
        )

    # ---------- schema ----------

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        await asyncio.to_thread(self._ensure_ready_sync)
        self._ready = True

    def _ensure_ready_sync(self) -> None:
        if not self._client.collection_exists(COLLECTION):
            self._client.create_collection(
                COLLECTION,
                vectors_config={
                    "dense": models.VectorParams(
                        size=self._dim, distance=models.Distance.COSINE
                    )
                },
            )
        tenant_schema: models.PayloadSchemaType | models.KeywordIndexParams
        if self._local:
            # 本地嵌入模式 payload 索引是 no-op (UserWarning), 与 qdrant_store 同策略
            tenant_schema = models.PayloadSchemaType.KEYWORD
        else:
            tenant_schema = models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD, is_tenant=True
            )
        with warnings.catch_warnings():
            if self._local:
                warnings.simplefilter("ignore", UserWarning)
            for field_name, schema in (
                ("tenant_id", tenant_schema),
                ("kb_id", models.PayloadSchemaType.KEYWORD),
                ("kb_version", models.PayloadSchemaType.INTEGER),
                ("at_ts", models.PayloadSchemaType.FLOAT),
            ):
                self._client.create_payload_index(
                    COLLECTION, field_name, field_schema=schema
                )

    # ---------- read ----------

    async def get(
        self,
        tenant_id: str,
        kb_id: str,
        kb_version: int,
        query: str,
        embedder: EmbeddingProvider,
    ) -> CacheHit | None:
        kb_version = int(kb_version)
        # L1 精确命中: 不触发向量化
        if self._redis is not None:
            raw = await self._redis.get(self.l1_key(tenant_id, kb_id, kb_version, query))
            if raw:
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    data = None
                if isinstance(data, dict):
                    return self._to_hit(data, source="exact", matched_query=query)

        # L2 语义命中: 同租户同库同版本 filter + 余弦阈值 + 数字/否定词复核
        [vector] = await embedder.embed([normalize_query(query)])
        await self.ensure_ready()
        response = await asyncio.to_thread(
            self._client.query_points,
            COLLECTION,
            query=vector,
            using="dense",
            query_filter=self._scope_filter(tenant_id, kb_id, kb_version),
            limit=3,
            with_payload=True,
            score_threshold=SCORE_THRESHOLD,
        )
        for point in response.points:
            payload = point.payload or {}
            cached_query = str(payload.get("query", ""))
            if not guard_consistent(query, cached_query):
                log.info("answer_cache_guard_rejected", query=query, cached=cached_query)
                continue
            return self._to_hit(
                payload, source="semantic", matched_query=cached_query, score=point.score
            )
        return None

    @staticmethod
    def _to_hit(
        data: dict, source: str, matched_query: str, score: float = 1.0
    ) -> CacheHit:
        return CacheHit(
            answer=str(data.get("answer", "")),
            citations=list(data.get("citations") or []),
            followups=list(data.get("followups") or []),
            model=str(data.get("model", "")),
            tier=str(data.get("tier", "standard")),
            completion_tokens=int(data.get("completion_tokens", 0)),
            at=str(data.get("at", "")),
            source=source,
            matched_query=matched_query,
            score=float(score),
        )

    # ---------- write ----------

    async def put(
        self,
        tenant_id: str,
        kb_id: str,
        kb_version: int,
        query: str,
        embedder: EmbeddingProvider,
        *,
        answer: str,
        citations: list[dict] | None = None,
        followups: list[str] | None = None,
        model: str = "",
        tier: str = "standard",
        completion_tokens: int = 0,
        at: datetime | None = None,
    ) -> None:
        """写入 L1+L2。只应传入自省环通过后的最终答案 (无个性化上下文)。"""
        kb_version = int(kb_version)
        at = at or datetime.now(UTC)
        value = {
            "answer": answer,
            "citations": citations or [],
            "followups": followups or [],
            "model": model,
            "tier": tier,
            "completion_tokens": int(completion_tokens),
            "at": at.isoformat(),
        }
        if self._redis is not None:
            await self._redis.set(
                self.l1_key(tenant_id, kb_id, kb_version, query),
                json.dumps(value, ensure_ascii=False),
                ex=L1_TTL_SECONDS,
            )
        normalized = normalize_query(query)
        [vector] = await embedder.embed([normalized])
        await self.ensure_ready()
        point = models.PointStruct(
            # 同一 (tenant, kb, version, 归一化query) 幂等覆盖, 不产生重复点位
            id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    _key_material(tenant_id, kb_id, kb_version, normalized),
                )
            ),
            vector={"dense": vector},
            payload={
                **value,
                "query": query,
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "kb_version": kb_version,
                "at_ts": at.timestamp(),
            },
        )
        await asyncio.to_thread(
            self._client.upsert, COLLECTION, points=[point], wait=True
        )

    # ---------- cleanup ----------

    async def delete_by_kb(self, tenant_id: str, kb_id: str) -> None:
        """删除该 (tenant, kb) 的全部 L2 点位 — 删库路径调用 (P0-3)。

        L1 (Redis) 不在此清理: 键是 sha256 不可枚举, 且问答路径查缓存前有 PG
        可见性校验 (库已删 -> 404 到不了缓存), 24h TTL 自净; 删文档路径靠
        kb_version 自增让 L1 键与 L2 filter 同时落空。
        """
        await asyncio.to_thread(self._delete_by_kb_sync, str(tenant_id), str(kb_id))

    def _delete_by_kb_sync(self, tenant_id: str, kb_id: str) -> None:
        if not self._client.collection_exists(COLLECTION):
            return
        scope = models.Filter(
            must=[
                models.FieldCondition(
                    key="tenant_id", match=models.MatchValue(value=tenant_id)
                ),
                models.FieldCondition(key="kb_id", match=models.MatchValue(value=kb_id)),
            ]
        )
        self._client.delete(
            COLLECTION,
            points_selector=models.FilterSelector(filter=scope),
            wait=True,
        )

    async def cleanup_older_than(self, cutoff: datetime) -> int:
        """删除 at 早于 cutoff 的 L2 点位, 返回删除数量 (L1 有 24h TTL 自净)。"""
        return await asyncio.to_thread(self._cleanup_sync, cutoff.timestamp())

    def _cleanup_sync(self, cutoff_ts: float) -> int:
        if not self._client.collection_exists(COLLECTION):
            return 0
        stale = models.Filter(
            must=[models.FieldCondition(key="at_ts", range=models.Range(lt=cutoff_ts))]
        )
        count = self._client.count(COLLECTION, count_filter=stale, exact=True).count
        if count:
            self._client.delete(
                COLLECTION,
                points_selector=models.FilterSelector(filter=stale),
                wait=True,
            )
        return int(count)

    @staticmethod
    def _scope_filter(tenant_id: str, kb_id: str, kb_version: int) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="tenant_id", match=models.MatchValue(value=tenant_id)
                ),
                models.FieldCondition(key="kb_id", match=models.MatchValue(value=kb_id)),
                models.FieldCondition(
                    key="kb_version", match=models.MatchValue(value=kb_version)
                ),
            ]
        )


async def cleanup_answer_cache(ctx: dict) -> int:
    """ARQ 任务: 清理 7 天前写入的缓存点位 (worker main 注册每日 cron)。

    以系统 UTC 时间为准 (缓存新鲜度是运维口径, 不跟随演示虚拟时钟)。
    """
    from ..config import get_settings

    settings: Settings = ctx.get("settings") or get_settings()
    cache = AnswerCache(settings)
    cutoff = datetime.now(UTC) - timedelta(days=CLEANUP_MAX_AGE_DAYS)
    deleted = await cache.cleanup_older_than(cutoff)
    log.info("answer_cache_cleanup", deleted=deleted, cutoff=cutoff.isoformat())
    return deleted
