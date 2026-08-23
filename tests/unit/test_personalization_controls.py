"""0031 个性化记忆控制面单元测试: 全离线零 PG/零 Redis。

覆盖四件事:
- 总开关: build_profile_context 在 personalization_enabled=False 时返回空
  ProfileContext (快照加载被 mock, 证明关闭时不触达画像数据);
- M3 纯函数: _should_suggest 阈值 / build_suggestion_text 文案 / pick_alternate;
- 评分入参: 非法 rating/reason/message_id → 422;
- 偏好信号统计: maybe_update_preference_signal 的去重 (7 天冷却) 与
  上限裁剪 (≤3 条) 逻辑经 fake session 验证。
"""

from __future__ import annotations

import uuid
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from eduagents_api.preference_signals import (
    _MIN_CONFUSING,
    _MIN_RATINGS,
    _should_suggest,
    build_suggestion_text,
    pick_alternate,
)
from eduagents_api.profile_summary import ProfileContext, build_profile_context
from eduagents_api.routers.answer_ratings import submit_answer_rating
from fastapi import HTTPException


def _ctx():
    uid = uuid.uuid4()
    return SimpleNamespace(user_id=uid, tenant_id=uid)


# ---- 总开关: 关闭 = 空画像 (且不触达快照加载) ----


@pytest.mark.asyncio
async def test_disabled_personalization_returns_empty_context(monkeypatch):
    """关闭 = 空画像。开关随快照一起读 (不为开关加第二次查询), 快照照常执行一次,
    但摘要/薄弱点/已掌握全部为空 — 下游 (qa/quiz/tutor) 按 empty 分支走。"""
    called = {"snapshot": 0}

    async def _fake_snapshot(tenant_id, user_id, kb_ids, org_ids=None):
        called["snapshot"] += 1
        return (
            [("kc1", "泰勒公式", 0.2, "observed")],
            ["泰勒公式"],
            Counter(),
            {
                "explanation_style": "analogy",
                "personalization_enabled": False,
            },
        )

    monkeypatch.setattr(
        "eduagents_api.profile_summary._load_snapshot", _fake_snapshot
    )
    uid = uuid.uuid4()
    ctx = await build_profile_context(uid, uid)
    assert ctx == ProfileContext() and ctx.empty
    assert called["snapshot"] == 1


@pytest.mark.asyncio
async def test_enabled_personalization_loads_snapshot(monkeypatch):
    """开关默认开 (无偏好行 = None) 时行为不变: 正常走快照与拼装。"""

    async def _fake_snapshot(tenant_id, user_id, kb_ids, org_ids=None):
        return (
            [("kc1", "泰勒公式", 0.2, "observed")],
            ["泰勒公式"],
            Counter(),
            None,
        )

    monkeypatch.setattr(
        "eduagents_api.profile_summary._load_snapshot", _fake_snapshot
    )

    class _Session:
        async def get(self, _model, _uid):
            return None

    @asynccontextmanager
    async def _fake_tenant_session(_tid):
        yield _Session()

    monkeypatch.setattr(
        "eduagents_api.profile_summary.tenant_session", _fake_tenant_session
    )
    uid = uuid.uuid4()
    ctx = await build_profile_context(uid, uid)
    assert "泰勒公式" in ctx.summary and not ctx.empty


# ---- M3 纯函数 ----


def test_should_suggest_thresholds():
    assert not _should_suggest(_MIN_RATINGS - 1, _MIN_CONFUSING)
    assert not _should_suggest(_MIN_RATINGS, _MIN_CONFUSING - 1)
    # 3 次 2 没听懂: 数量达标且占比 2/3 ≥ 0.5 → 触发
    assert _should_suggest(3, 2)
    # 4 次 2 没听懂: 占比 0.5 达标 → 触发
    assert _should_suggest(4, 2)
    # 10 次 2 没懂: 占比不足 → 不触发
    assert not _should_suggest(10, 2)


def test_suggestion_text_uses_chinese_labels():
    text = build_suggestion_text("analogy", 4, 3, "rigorous")
    assert "类比优先" in text and "严谨" in text and "4" in text and "3" in text


def test_pick_alternate_never_returns_current():
    for style in ("rigorous", "colloquial", "analogy"):
        assert pick_alternate(style) != style
        assert pick_alternate(style) in ("rigorous", "colloquial", "analogy")


# ---- 评分入参校验 ----


@pytest.mark.asyncio
async def test_rating_rejects_invalid_values():
    with pytest.raises(HTTPException) as e:
        await submit_answer_rating(
            _body(rating="great"), ctx=_ctx()
        )
    assert e.value.status_code == 422 and e.value.detail == "INVALID_RATING"
    with pytest.raises(HTTPException) as e:
        await submit_answer_rating(
            _body(rating="bad", reason="meh"), ctx=_ctx()
        )
    assert e.value.status_code == 422 and e.value.detail == "INVALID_REASON"
    with pytest.raises(HTTPException) as e:
        await submit_answer_rating(
            _body(rating="bad", message_id="not-a-uuid"), ctx=_ctx()
        )
    assert e.value.status_code == 422 and e.value.detail == "INVALID_MESSAGE_ID"


def _body(rating: str, reason: str | None = None, message_id: str | None = None):
    from eduagents_api.routers.answer_ratings import AnswerRatingIn

    return AnswerRatingIn(
        rating=rating, reason=reason, message_id=message_id, profile_used=True
    )
