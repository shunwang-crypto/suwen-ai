"""用户偏好单元测试 (Wave-P2 P2-3/P2-4, G8): 全离线零 PG/零 Redis 服务。

覆盖四件事:
- 入参校验: 枚举白名单 (与 profile_summary 标签词表同源) / interests 归一化
  (压空白/去重/项数与单项字数上限) / daily_minutes 范围 / goal 长度;
- upsert: 首次 PUT 插入、再次 PUT 整资源替换 (缺省字段即清空), fake session
  不触达 PG (同 test_learner_insights 写法);
- 摘要偏好段: _compose_summary 追加定性偏好描述, 偏好段排在薄弱点段之后,
  超 300 字先砍兴趣、再不够整段让位; 只有偏好的新用户摘要非空 (G12 冷启动);
- PUT 后缓存失效: invalidate_profile_summary_cache 删光 profile_summary:{uid}*
  (裸 key + kb 指纹桶), 缓存故障只吞不抛。
"""

from __future__ import annotations

import uuid
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from eduagents_api.profile_summary import (
    EXPLANATION_STYLES,
    STYLE_LABELS,
    SUMMARY_MAX_CHARS,
    VERBOSITY_LABELS,
    VERBOSITY_LEVELS,
    _compose_summary,
    invalidate_profile_summary_cache,
)
from eduagents_api.routers import preferences as prefs_mod
from eduagents_api.routers.preferences import (
    INTEREST_CHAR_MAX,
    INTERESTS_MAX,
    PreferencesIn,
    get_preferences,
    normalize_interests,
    put_preferences,
)
from fastapi import HTTPException


def _ctx():
    uid = uuid.uuid4()
    return SimpleNamespace(user_id=uid, tenant_id=uid)


# ---- 枚举白名单单一出处 (profile_summary 标签词表 = 校验词表 = 0005 CHECK 口径) ----


def test_enum_whitelists_match_label_maps():
    assert EXPLANATION_STYLES == ("rigorous", "colloquial", "analogy")
    assert VERBOSITY_LEVELS == ("concise", "detailed")
    assert tuple(STYLE_LABELS) == EXPLANATION_STYLES
    assert tuple(VERBOSITY_LABELS) == VERBOSITY_LEVELS


# ---- interests 归一化 ----


def test_normalize_interests_strips_dedupes_and_drops_empty():
    assert normalize_interests([" 篮球 ", "篮球", "机器  学习", "", "  "]) == [
        "篮球",
        "机器 学习",
    ]


def test_normalize_interests_limits():
    with pytest.raises(HTTPException) as e:
        normalize_interests(["兴" * (INTEREST_CHAR_MAX + 1)])
    assert e.value.status_code == 422 and e.value.detail == "INTEREST_TOO_LONG"
    with pytest.raises(HTTPException) as e:
        normalize_interests([f"兴趣{i}" for i in range(INTERESTS_MAX + 1)])
    assert e.value.status_code == 422 and e.value.detail == "TOO_MANY_INTERESTS"
    # 去重后不超限即放行 (原始列表允许重复)
    assert len(normalize_interests(["同一个"] * 20)) == 1


# ---- PUT 校验: 枚举/范围违规 422 ----


class _NoopRedis:
    def __init__(self):
        self.deleted: list[str] = []

    async def scan_iter(self, match=None):
        return
        yield  # pragma: no cover — 空异步生成器写法

    async def delete(self, *keys):
        self.deleted.extend(keys)


class _FakeSession:
    """最小 session 假体: get 按 user_id 出既有行, 记录 add。"""

    def __init__(self, existing=None):
        self.existing = existing
        self.added = []
        self.refreshed = []

    async def get(self, model, pk):
        return self.existing

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def refresh(self, obj):
        self.refreshed.append(obj)


def _patch_session(monkeypatch, session):
    @asynccontextmanager
    async def fake_tenant_session(tid, **kw):
        yield session

    monkeypatch.setattr(prefs_mod, "tenant_session", fake_tenant_session)


async def test_put_rejects_bad_enums(monkeypatch):
    _patch_session(monkeypatch, _FakeSession())
    for body in (
        PreferencesIn(explanation_style="freeform"),  # 词表外风格一律 422
        PreferencesIn(verbosity="verbose"),
        PreferencesIn(daily_minutes=2),
        PreferencesIn(daily_minutes=2000),
        PreferencesIn(goal="冲" * 201),
    ):
        with pytest.raises(HTTPException) as e:
            await put_preferences(body, ctx=_ctx(), redis=_NoopRedis())
        assert e.value.status_code == 422


# ---- upsert: 插入 / 整资源替换 ----


async def test_put_inserts_row_when_missing(monkeypatch):
    session = _FakeSession(existing=None)
    _patch_session(monkeypatch, session)
    ctx = _ctx()
    out = await put_preferences(
        PreferencesIn(
            explanation_style="analogy",
            verbosity="concise",
            interests=[" 篮球 ", "动漫", "篮球"],
            daily_minutes=30,
            goal="  高数期末冲 90  ",
        ),
        ctx=ctx,
        redis=_NoopRedis(),
    )
    assert len(session.added) == 1
    row = session.added[0]
    assert row.user_id == ctx.user_id and row.tenant_id == ctx.tenant_id
    assert row.explanation_style == "analogy" and row.verbosity == "concise"
    assert row.interests == ["篮球", "动漫"]
    assert row.daily_minutes == 30 and row.goal == "高数期末冲 90"
    assert out["interests"] == ["篮球", "动漫"] and out["goal"] == "高数期末冲 90"
    assert session.refreshed == [row]  # 取回 server 端 updated_at


async def test_put_replaces_whole_resource(monkeypatch):
    from eduagents_api.models import UserPreference

    ctx = _ctx()
    existing = UserPreference(
        user_id=ctx.user_id,
        tenant_id=ctx.tenant_id,
        explanation_style="rigorous",
        verbosity="detailed",
        interests=["天文"],
        daily_minutes=60,
        goal="旧目标",
    )
    session = _FakeSession(existing=existing)
    _patch_session(monkeypatch, session)
    # PUT 空体 = 清空 (设置页"清空全部偏好"走这条路)
    out = await put_preferences(PreferencesIn(), ctx=ctx, redis=_NoopRedis())
    assert not session.added  # 走更新分支, 不重复插行
    assert existing.explanation_style is None and existing.verbosity is None
    assert existing.interests == [] and existing.daily_minutes is None
    assert existing.goal is None
    assert out["explanation_style"] is None and out["interests"] == []


async def test_get_returns_defaults_when_never_set(monkeypatch):
    _patch_session(monkeypatch, _FakeSession(existing=None))
    out = await get_preferences(ctx=_ctx())
    assert out == {
        "explanation_style": None,
        "verbosity": None,
        "interests": [],
        "daily_minutes": None,
        "goal": None,
        "updated_at": None,
    }


# ---- 摘要偏好段 (P2-3): 定性描述 / 排序 / 300 字上限降级 ----

_PREFS = {
    "explanation_style": "analogy",
    "verbosity": "concise",
    "interests": ["篮球", "动漫"],
    "daily_minutes": 30,
    "goal": "高数期末冲 90",
}


def test_summary_appends_pref_segment_after_weak_points():
    mastery = [("kc-1", "导数定义", 0.2)]
    ctx = _compose_summary(mastery, ["导数定义"], Counter({"concept_confusion": 2}), _PREFS)
    s = ctx.summary
    assert "讲解偏好: 类比优先、简洁" in s
    assert "兴趣: 篮球、动漫" in s
    assert "目标: 高数期末冲 90" in s
    assert "每日可学约30分钟" in s
    # 偏好段优先级低于薄弱点段: 排在其后
    assert s.index("薄弱点") < s.index("讲解偏好")
    # 脱敏口径不破坏: 摘要无掌握度小数
    assert "0.2" not in s


def test_prefs_only_summary_nonempty_for_cold_start():
    # G12 冷启动: 无任何掌握度/错题数据, 只有 Onboarding 偏好 -> 摘要非空
    ctx = _compose_summary([], [], Counter(), _PREFS)
    assert not ctx.empty
    assert ctx.summary.startswith("讲解偏好: 类比优先、简洁")
    assert ctx.weak_points == [] and ctx.mastered == []
    # 完全无数据仍为空 (答案缓存互斥判据不受影响)
    assert _compose_summary([], [], Counter(), None).empty
    assert _compose_summary([], [], Counter(), {}).empty


def test_overlong_summary_cuts_interests_first():
    # 基础段 + 全量偏好 > 300 字, 但砍掉兴趣后 ≤ 300: 只砍兴趣, 其余偏好保留
    mastery = [
        (f"kc-{i}", "微分中值定理与泰勒展开的综合应用场景" + f"{i:02d}", 0.2)
        for i in range(5)
    ]
    prefs = dict(_PREFS, interests=["兴" * 18 + f"{i:02d}" for i in range(8)])
    full = _compose_summary(mastery, [], Counter(), prefs)
    base_only = _compose_summary(mastery, [], Counter(), None)
    # 前置自检: 不砍确实超长, 基础段自身不超长 (测试构造有效性)
    assert len(base_only.summary) <= SUMMARY_MAX_CHARS
    assert len(full.summary) <= SUMMARY_MAX_CHARS
    assert "兴趣:" not in full.summary
    assert "讲解偏好: 类比优先、简洁" in full.summary
    assert "目标: 高数期末冲 90" in full.summary
    assert "薄弱点" in full.summary and not full.summary.endswith("…")


def test_overlong_summary_drops_whole_pref_segment_last():
    # 基础段自身已超 300 -> 砍兴趣不够, 偏好整段让位, 与无偏好时逐字一致
    mastery = [(f"kc-{i}", "长" * 60 + f"{i:02d}", 0.2) for i in range(5)]
    long_base = _compose_summary(mastery, [], Counter(), None).summary
    ctx = _compose_summary(mastery, [], Counter(), dict(_PREFS))
    assert len(ctx.summary) <= SUMMARY_MAX_CHARS
    assert "讲解偏好" not in ctx.summary and "目标:" not in ctx.summary
    assert ctx.summary == long_base  # 基础段不受偏好影响 (末位截断口径也一致)


def test_partial_prefs_render_only_present_fields():
    ctx = _compose_summary([], [], Counter(), {"interests": ["科幻"]})
    assert ctx.summary == "兴趣: 科幻。"
    ctx = _compose_summary([], [], Counter(), {"goal": "考研数学一 120"})
    assert ctx.summary == "目标: 考研数学一 120。"


# ---- PUT 后缓存失效 ----


class _ScanRedis:
    """记录 scan/delete 的假 Redis: 预置 3 个本人 key + 1 个他人 key。"""

    def __init__(self, uid: str, fail: bool = False):
        self.fail = fail
        self.keys = {
            f"profile_summary:{uid}",
            f"profile_summary:{uid}:abcd1234",
            f"profile_summary:{uid}:ffff0000",
            "profile_summary:other-user",
        }
        self.deleted: list[str] = []
        self.patterns: list[str] = []

    async def scan_iter(self, match=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.patterns.append(match)
        import fnmatch

        for key in sorted(self.keys):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    async def delete(self, *keys):
        self.deleted.extend(keys)
        return len(keys)


async def test_invalidate_deletes_all_buckets_of_this_user_only():
    uid = str(uuid.uuid4())
    redis = _ScanRedis(uid)
    n = await invalidate_profile_summary_cache(redis, uid)
    assert n == 3
    assert redis.patterns == [f"profile_summary:{uid}*"]
    assert set(redis.deleted) == {
        f"profile_summary:{uid}",
        f"profile_summary:{uid}:abcd1234",
        f"profile_summary:{uid}:ffff0000",
    }
    assert "profile_summary:other-user" not in redis.deleted


async def test_invalidate_swallows_cache_failure_and_none():
    uid = str(uuid.uuid4())
    assert await invalidate_profile_summary_cache(None, uid) == 0
    assert await invalidate_profile_summary_cache(_ScanRedis(uid, fail=True), uid) == 0


async def test_put_invalidates_profile_summary_cache(monkeypatch):
    ctx = _ctx()
    _patch_session(monkeypatch, _FakeSession())
    redis = _ScanRedis(str(ctx.user_id))
    await put_preferences(PreferencesIn(interests=["篮球"]), ctx=ctx, redis=redis)
    # PUT 成功后立即删光该用户全部摘要缓存桶 (60s TTL 等不起演示即时性)
    assert set(redis.deleted) == {
        f"profile_summary:{ctx.user_id}",
        f"profile_summary:{ctx.user_id}:abcd1234",
        f"profile_summary:{ctx.user_id}:ffff0000",
    }
