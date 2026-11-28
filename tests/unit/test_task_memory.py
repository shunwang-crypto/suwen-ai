"""Memory recall, evidence grounding and native tool admission without paid models."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from eduagents_api.routers.chat_support import _maybe_enqueue_reflection
from eduagents_api.routers.profile import InsightPatch
from eduagents_core.education.memory import MAX_CONTEXT_CHARS, fingerprint, recall
from eduagents_core.harness.task import AgentTask, MemoryTaskStore
from eduagents_worker.reflection import grounded_insight_rows
from fakeredis.aioredis import FakeRedis
from pydantic import ValidationError

from eval.run_memory_eval import evaluate

from .test_native_agent_tasks import ScriptedModel, deps


def test_synthetic_memory_regressions():
    cases = json.loads((Path(__file__).resolve().parents[2] / "eval/memory_cases.json").read_text())
    report = evaluate(cases)
    assert report["total"] >= 10
    assert report["passed"] == report["total"], report


def test_grounding_rejects_assistant_quotes_and_fabrications_and_old_evidence():
    now = datetime.now(UTC)
    vocab = [(str(uuid.uuid4()), "导数定义", [])]
    messages = [
        SimpleNamespace(id=uuid.uuid4(), role="assistant", content="你还不懂导数", created_at=now),
        SimpleNamespace(id=uuid.uuid4(), role="user", content="昨天还不理解导数定义", created_at=now - timedelta(days=1)),
        SimpleNamespace(id=uuid.uuid4(), role="user", content="现在会按导数定义算了", created_at=now),
    ]
    insights = [{"kc_name": "导数定义", "signal": "confused", "evidence": text} for text in (
        "你还不懂导数", "编造的学生表达", "昨天还不理解导数定义"
    )] + [{"kc_name": "导数定义", "signal": "understood", "evidence": "现在会按导数定义算了"}]
    result = grounded_insight_rows(insights, vocab, messages, extraction_after=now - timedelta(hours=1))
    assert len(result) == 1 and result[0]["kind"] == "understood"
    assert result[0]["source_message_id"] == str(messages[2].id)


def test_context_budget_and_fingerprint_normalization():
    now = datetime.now(UTC)
    items = [dict(id=str(i), kc_id=str(i), kc_name="导数定义", text="导数" * 300,
                  created_at=now, updated_at=now, source="chat_reflection", kind="confused") for i in range(100)]
    result = recall(items, "导数定义", [], now=now, limit=100)
    assert 0 < len(result["memories"]) <= 4
    assert len(json.dumps(result, ensure_ascii=False)) <= MAX_CONTEXT_CHARS
    assert fingerprint("ＡＢＣ 导数\n") == fingerprint("abc导数")


def test_source_course_disambiguates_identical_knowledge_names():
    now = datetime.now(UTC)
    vocab = [("kc-a", "导数定义", []), ("kc-b", "导数定义", [])]
    message = SimpleNamespace(id=uuid.uuid4(), role="user", content="导数定义不会代入", created_at=now, meta_json={"kb_ids": ["course-b"]})
    insights = [{"kc_name": "导数定义", "signal": "confused", "evidence": message.content}]
    scopes = {"kc-a": "course-a", "kc-b": "course-b"}
    rows = grounded_insight_rows(insights, vocab, [message], kc_scopes=scopes)
    assert len(rows) == 1 and rows[0]["kc_id"] == "kc-b"
    message.meta_json = {}
    assert grounded_insight_rows(insights, vocab, [message], kc_scopes=scopes) == []


def test_memory_patch_rejects_blank_invalid_and_empty_updates():
    for patch in ({"text": "  "}, {"kind": "mastered"}, {"status": "deleted"}, {}):
        with pytest.raises(ValidationError):
            InsightPatch(**patch)


async def test_reflection_enqueue_uses_distinct_cumulative_round_ids():
    redis = FakeRedis(decode_responses=True)
    jobs = []

    async def enqueue(*args, **kwargs):
        jobs.append((args, kwargs))
        return object()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis, arq=SimpleNamespace(enqueue_job=enqueue))))
    owner = uuid.uuid4()
    ctx = SimpleNamespace(user_id=owner, tenant_id=owner)
    session_id = uuid.uuid4()
    outcomes = [await _maybe_enqueue_reflection(request, ctx, session_id) for _ in range(10)]
    assert sum(outcomes) == 2
    assert [job[1]["_job_id"] for job in jobs] == [f"reflect:{session_id}:5", f"reflect:{session_id}:10"]
    await redis.aclose()


class FakeMemory:
    def __init__(self):
        self.revision = 0
        self.searches = []

    async def snapshot(self, state):
        return {"revision": self.revision, "enabled": True}

    async def search(self, state, query, kc_names, limit):
        self.searches.append((state["user_id"], query, kc_names, limit))
        return {"memories": [{"id": "memory", "text": "原先把导数当成直接代入", "observed_at": "2026-11-20"}]}


async def test_native_harness_reads_memory_and_discards_recalled_archive_after_correction():
    memory, store = FakeMemory(), MemoryTaskStore()
    model = ScriptedModel([
        ("search_learning_memories", {"query": "导数定义"}),
        ("submit_answer", {"answer": "可以从极限定义开始。", "source_ids": []}), "finished",
    ])
    payload = {"query": "导数怎么定义", "user_id": "owner", "tenant_id": "owner", "request_id": "first"}
    first = await AgentTask("qa", deps(model, memory=memory), store).run("memory-qa", payload)
    assert first.status == "completed" and first.native_archive
    assert memory.searches == [("owner", "导数定义", [], 4)]
    assert any(m.role == "tool" and "直接代入" in m.content for m in model.requests[1].messages)
    memory.revision += 1
    second_model = ScriptedModel([("submit_answer", {"answer": "使用最新的学习情况。", "source_ids": []}), "finished"])
    second = await AgentTask("qa", deps(second_model, memory=memory), store).run("memory-qa", {**payload, "request_id": "second"})
    assert second.native_session_id != first.native_session_id
    assert all("直接代入" not in m.content for m in second_model.requests[0].messages)


async def test_memory_tool_cannot_request_another_user_or_unbounded_results():
    memory = FakeMemory()
    model = ScriptedModel([
        ("search_learning_memories", {"user_id": "other", "query": "导数"}),
        ("search_learning_memories", {"query": "导数", "limit": 100}),
        ("submit_answer", {"answer": "从定义出发。", "source_ids": []}), "finished",
    ])
    result = await AgentTask("qa", deps(model, memory=memory)).run("denied-memory", {"user_id": "owner", "query": "导数"})
    assert result.status == "completed"
    assert memory.searches == []


def test_semantic_recall_adds_paraphrases_without_reviving_suppressed_or_corrected_history():
    now = datetime.now(UTC)
    def item(id, kc, text, **extra):
        return dict(id=id, kc_id=kc, kc_name="", text=text, kind="confused", created_at=now,
                    observed_at=now, source="chat_reflection", **extra)
    items = [item("related", "kc-a", "分母被我直接相加"),
             item("suppressed", "kc-b", "相加", suppressed=True),
             item("old", "kc-c", "老错误"),
             item("correction", "kc-c", "已经学会", user_corrected=True, status="obsolete")]
    assert not recall(items, "异分母分数的通分", [], now=now)["memories"]
    result = recall(items, "异分母分数的通分", [], now=now,
                    semantic_scores=dict.fromkeys([v["id"] for v in items], 0.9))
    assert [v["id"] for v in result["memories"]] == ["related"]
    assert result["interpretation"] == "historical_observation_not_mastery_evidence"
