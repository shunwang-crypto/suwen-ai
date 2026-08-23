"""摄取成本安全线集成测试 (INTEGRATION=1, 真 PG):

- P0-2c chunk 配额: 单文档 / 用户级超限 -> status=failed + 错误信息含配额说明;
- P1-6 CancelledError (job_timeout): 状态落 failed 后 re-raise, 不再永久卡 parsing;
- P1-6 reap_stale_ingestion: >30 分钟停滞的中间态置 failed 并重入队 ≤3 次。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest
from eduagents_core.config import get_settings
from fakeredis import FakeAsyncRedis

from .conftest import requires_services

pytestmark = requires_services

_MD = """# 第一章 导数

## 1.1 导数定义

导数描述函数在某点的瞬时变化率，定义为增量比的极限。

## 1.2 求导法则

和差积商的求导法则与链式法则是计算导数的基础工具。
"""


async def _register_login(client):
    email = f"quota-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _upload_doc(client, headers) -> tuple[str, str]:
    r = await client.post("/api/kb", json={"name": "配额库", "subject": "math"}, headers=headers)
    kb_id = r.json()["id"]
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("calc.md", _MD.encode(), "text/markdown")},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return kb_id, r.json()["id"]


async def _doc_row(client, headers, kb_id: str, doc_id: str) -> dict:
    r = await client.get(f"/api/kb/{kb_id}/documents", headers=headers)
    return {d["id"]: d for d in r.json()}[doc_id]


async def test_doc_chunk_quota_marks_failed(client, monkeypatch):
    user_id, headers = await _register_login(client)
    kb_id, doc_id = await _upload_doc(client, headers)

    import eduagents_worker.ingestion as ingestion

    tight = get_settings().model_copy(update={"max_chunks_per_doc": 1})
    monkeypatch.setattr(ingestion, "get_settings", lambda: tight)
    assert await ingestion.ingest_document({}, user_id, doc_id) == "failed"

    doc = await _doc_row(client, headers, kb_id, doc_id)
    assert doc["status"] == "failed"
    assert "CHUNK_QUOTA_EXCEEDED" in doc["error_msg"]
    assert "配额" in doc["error_msg"]


async def test_user_chunk_quota_marks_failed(client, monkeypatch):
    user_id, headers = await _register_login(client)
    kb_id, doc_id = await _upload_doc(client, headers)

    import eduagents_worker.ingestion as ingestion

    tight = get_settings().model_copy(update={"max_chunks_per_user": 1})
    monkeypatch.setattr(ingestion, "get_settings", lambda: tight)
    assert await ingestion.ingest_document({}, user_id, doc_id) == "failed"

    doc = await _doc_row(client, headers, kb_id, doc_id)
    assert doc["status"] == "failed"
    assert "CHUNK_QUOTA_EXCEEDED" in doc["error_msg"]
    assert "账户" in doc["error_msg"]


async def test_cancelled_error_marks_failed_and_reraises(client, monkeypatch):
    """P1-6: arq job_timeout 的 CancelledError 必须落 failed 后 re-raise,
    不再让状态永久停在 parsing。"""
    user_id, headers = await _register_login(client)
    kb_id, doc_id = await _upload_doc(client, headers)

    class _CancelParser:
        def parse(self, path):
            raise asyncio.CancelledError()

    monkeypatch.setattr(
        "eduagents_core.parsing.registry.get_parser", lambda suffix: _CancelParser()
    )
    from eduagents_worker.ingestion import ingest_document

    with pytest.raises(asyncio.CancelledError):
        await ingest_document({}, user_id, doc_id)

    doc = await _doc_row(client, headers, kb_id, doc_id)
    assert doc["status"] == "failed"
    assert "超时" in doc["error_msg"]


class _ReapRedis(FakeAsyncRedis):
    """reap cron 依赖的最小 ctx["redis"]: fakeredis + enqueue_job 录制。"""

    def __init__(self):
        super().__init__(decode_responses=True)
        self.enqueued: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.enqueued.append((name, *args))
        # 源码 reap 会校验 enqueue_job 返回值 (job is None 视为入队失败)；真 arq
        # 成功入队返回 Job 句柄，这里如实模拟一个非 None 句柄。
        return SimpleNamespace(job_id=kwargs.get("_job_id"))


async def test_reap_stale_ingestion_requeues_then_gives_up(client):
    user_id, headers = await _register_login(client)
    kb_id, doc_id = await _upload_doc(client, headers)

    from eduagents_worker.ingestion import (
        INGEST_WATCH_KEY,
        REAP_MAX_RETRIES,
        _set_status,
        reap_stale_ingestion,
    )

    redis = _ReapRedis()
    ctx = {"redis": redis}
    field = f"{user_id}:{doc_id}"

    # 新鲜登记 (< 30 分钟) 不回收
    await _set_status(doc_id, user_id, "parsing")
    await redis.hset(INGEST_WATCH_KEY, field, time.time())
    assert await reap_stale_ingestion(ctx) == {"reaped": 0, "requeued": 0}
    assert (await _doc_row(client, headers, kb_id, doc_id))["status"] == "parsing"

    # 停滞 >30 分钟: 前 REAP_MAX_RETRIES 次置 failed + 重入队
    for i in range(1, REAP_MAX_RETRIES + 1):
        await _set_status(doc_id, user_id, "parsing")
        await redis.hset(INGEST_WATCH_KEY, field, time.time() - 3600)
        result = await reap_stale_ingestion(ctx)
        assert result == {"reaped": 1, "requeued": 1}
        doc = await _doc_row(client, headers, kb_id, doc_id)
        assert doc["status"] == "failed"
        assert len(redis.enqueued) == i
        assert redis.enqueued[-1] == (
            "ingest_document",
            user_id,
            doc_id,
            f"ingest:{doc_id}:v1:retry:{i}",
        )

    # 第 4 次: 只回收不再重入队, 错误信息提示放弃
    await _set_status(doc_id, user_id, "parsing")
    await redis.hset(INGEST_WATCH_KEY, field, time.time() - 3600)
    assert await reap_stale_ingestion(ctx) == {"reaped": 1, "requeued": 0}
    doc = await _doc_row(client, headers, kb_id, doc_id)
    assert doc["status"] == "failed"
    assert "仍失败" in doc["error_msg"]
    assert len(redis.enqueued) == REAP_MAX_RETRIES

    # 终态 (failed) 的停滞登记残留只清理, 不再计数/入队
    await redis.hset(INGEST_WATCH_KEY, field, time.time() - 3600)
    assert await reap_stale_ingestion(ctx) == {"reaped": 0, "requeued": 0}
    assert not await redis.hexists(INGEST_WATCH_KEY, field)
    assert len(redis.enqueued) == REAP_MAX_RETRIES
