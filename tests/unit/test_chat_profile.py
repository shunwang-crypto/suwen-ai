"""chat 路由画像接线单元测试 (P1-5 全路径画像注入)。

覆盖三件事, 全离线零 LLM:
- 直连路径 system prompt 组装: 画像非空追加画像段 (与 qa 子图同款模板), 空画像保持裸 prompt;
- 答案缓存准入 _cache_admissible: 空画像不禁缓存、非空画像禁缓存 (个性化答案不得跨用户复用),
  以及单库/首轮/组件可用各判据;
- profile_summary 的 Redis 缓存 key 形态: 无 kb 时为 profile_summary:{user_id} (不带 kb 指纹),
  绑定 kb 时带 8 位指纹分桶。
"""

from __future__ import annotations

import uuid

import orjson
from eduagents_api.profile_summary import ProfileContext, build_profile_context
from eduagents_api.routers.chat import (
    _SYSTEM_PROMPT,
    _cache_admissible,
    _direct_system_prompt,
)

WEAK_SUMMARY = "薄弱点: 导数定义(较弱)、极值判定(待巩固)。"


def filled_profile() -> ProfileContext:
    return ProfileContext(summary=WEAK_SUMMARY, weak_points=["导数定义", "极值判定"])


# --- 直连路径 system prompt 组装 ---


def test_direct_system_prompt_appends_profile_section():
    prompt = _direct_system_prompt(filled_profile())
    assert prompt.startswith(_SYSTEM_PROMPT)  # 基础助教文案原样保留
    assert "学生画像" in prompt
    assert WEAK_SUMMARY in prompt
    assert "个性化指令" in prompt  # 画像只调讲法, 不替代事实依据的约束段


def test_direct_system_prompt_empty_profile_stays_bare():
    assert _direct_system_prompt(ProfileContext()) == _SYSTEM_PROMPT


# --- 答案缓存准入 (缓存 × 个性化互斥) ---


def test_cache_admissible_profile_mutex():
    kb = [uuid.uuid4()]
    cache, embedder = object(), object()
    # 空画像 (新用户): 单库首轮照常走缓存
    assert _cache_admissible(kb, [], cache, embedder, ProfileContext()) is True
    # 非空画像: 答案已按薄弱点个性化, 一票否决
    assert _cache_admissible(kb, [], cache, embedder, filled_profile()) is False


def test_cache_admissible_other_gates():
    kb = [uuid.uuid4()]
    cache, embedder = object(), object()
    empty = ProfileContext()
    assert _cache_admissible(kb * 2, [], cache, embedder, empty) is False  # 多库
    assert _cache_admissible(kb, ["历史轮"], cache, embedder, empty) is False  # 非首轮
    assert _cache_admissible([], [], cache, embedder, empty) is False  # 无 kb (直连路径)
    assert _cache_admissible(kb, [], None, embedder, empty) is False  # 缓存组件缺失
    assert _cache_admissible(kb, [], cache, None, empty) is False  # embedder 缺失


# --- profile_summary 缓存 key 形态 (无 kb 不带指纹) ---


class RecordingRedis:
    """记录 get 的 key 并恒命中: 只验证 key 形态, 不触达 PG。"""

    def __init__(self, payload: dict):
        self.payload = orjson.dumps(payload)
        self.keys: list[str] = []

    async def get(self, key: str):
        self.keys.append(key)
        return self.payload

    async def setex(self, key: str, ttl: int, value):  # pragma: no cover — 命中即不回写
        raise AssertionError("缓存命中路径不应回写")


async def test_profile_cache_key_without_kb_has_no_fingerprint():
    uid = str(uuid.uuid4())
    redis = RecordingRedis(
        {"summary": WEAK_SUMMARY, "weak_points": ["导数定义"], "mastered": []}
    )
    ctx = await build_profile_context(uid, uid, [], redis=redis)
    assert redis.keys == [f"profile_summary:{uid}"]
    assert ctx.summary == WEAK_SUMMARY
    assert ctx.weak_points == ["导数定义"]


async def test_profile_cache_key_with_kb_is_fingerprinted():
    uid = str(uuid.uuid4())
    redis = RecordingRedis({"summary": "", "weak_points": [], "mastered": []})
    await build_profile_context(uid, uid, [str(uuid.uuid4())], redis=redis)
    prefix = f"profile_summary:{uid}:"
    assert redis.keys[0].startswith(prefix)
    assert len(redis.keys[0]) == len(prefix) + 8  # 8 位 kb 指纹分桶
