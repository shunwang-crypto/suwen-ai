"""用户偏好全链路集成测试 (Wave-P2 P2-3/P2-4, G8)。

- PUT/GET /api/preferences 往返 (upsert + 整资源替换 + 清空) 与入参校验 422;
- 偏好 e2e 注入 qa prompt: PUT 偏好后直连问答 (无 kb) 的 system prompt
  含偏好段 (兴趣/目标/讲解偏好) 与兴趣情境化指令 (Khanmigo 式);
- PUT 后缓存立即失效: 先问答一次把摘要写进 Redis, 改偏好再问, 新偏好
  立即出现在 prompt (不等 60s TTL);
- 本人隔离: 他人 GET 只见自己的空缺省。

真实 PG/Redis, 网关换脚本假体 (捕获出境 prompt, 零真实 LLM)。
"""

from __future__ import annotations

import uuid

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"

PREFS = {
    "explanation_style": "analogy",
    "verbosity": "concise",
    "interests": ["篮球", "动漫"],
    "daily_minutes": 30,
    "goal": "高数期末冲 90",
}


class _CaptureStreamGateway:
    """直连路径假网关: 记录 stream_chat 请求并回一句定长答案。"""

    def __init__(self):
        self.requests = []

    async def stream_chat(self, req):
        from eduagents_core.gateway import ChatResult, StreamDelta

        self.requests.append(req)
        yield StreamDelta(text="这是测试回答。")
        yield ChatResult(
            text="这是测试回答。",
            model="fake",
            tier=str(req.tier),
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="replay",
        )


async def _register_login(client, prefix="pref"):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _chat_once(client, headers, message="讲讲导数的定义"):
    """走直连问答 (无 kb) 消费完 SSE, 顺带把画像摘要写进 Redis 缓存。"""
    async with client.stream(
        "POST", "/api/chat", json={"message": message}, headers=headers, timeout=60
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for _line in resp.aiter_lines():
            pass


def _system_prompt(gateway: _CaptureStreamGateway) -> str:
    assert gateway.requests, "假网关未捕获到任何请求"
    req = gateway.requests[-1]
    system = [m for m in req.messages if m.role == "system"]
    assert len(system) == 1
    return system[0].content


# ---- PUT/GET 往返 + 校验 ----


async def test_put_get_roundtrip_replace_and_clear(client):
    _uid, headers = await _register_login(client, "pref-crud")

    # 从未设置: GET 返回全空缺省 (前端不必区分 404)
    r = await client.get("/api/preferences", headers=headers)
    assert r.status_code == 200
    assert r.json()["explanation_style"] is None and r.json()["interests"] == []

    # 首次 PUT: 归一化 (兴趣去空白去重) 后原样读回
    r = await client.put(
        "/api/preferences",
        headers=headers,
        json=dict(PREFS, interests=[" 篮球 ", "动漫", "篮球"]),
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["explanation_style"] == "analogy" and out["verbosity"] == "concise"
    assert out["interests"] == ["篮球", "动漫"]
    assert out["daily_minutes"] == 30 and out["goal"] == "高数期末冲 90"
    assert out["updated_at"] is not None

    r = await client.get("/api/preferences", headers=headers)
    assert r.json()["interests"] == ["篮球", "动漫"]

    # PUT 整资源替换: 缺省字段清空 (设置页"清空全部偏好")
    r = await client.put("/api/preferences", headers=headers, json={})
    assert r.status_code == 200
    r = await client.get("/api/preferences", headers=headers)
    out = r.json()
    assert out["explanation_style"] is None and out["verbosity"] is None
    assert out["interests"] == [] and out["daily_minutes"] is None
    assert out["goal"] is None


async def test_put_validation_422(client):
    _uid, headers = await _register_login(client, "pref-val")
    for payload in (
        {"explanation_style": "freeform"},  # 词表外枚举
        {"verbosity": "verbose"},
        {"daily_minutes": 2},
        {"interests": ["兴" * 21]},  # 单项超 20 字
        {"interests": [f"兴趣{i}" for i in range(9)]},  # 超 8 项
        {"goal": "冲" * 201},
    ):
        r = await client.put("/api/preferences", headers=headers, json=payload)
        assert r.status_code == 422, (payload, r.text)


# ---- 偏好 e2e 注入 qa prompt (P2-3 核心验收) ----


async def test_preferences_reach_qa_system_prompt(client):
    app = client._transport.app
    _uid, headers = await _register_login(client, "pref-qa")

    r = await client.put("/api/preferences", headers=headers, json=PREFS)
    assert r.status_code == 200, r.text

    fake = _CaptureStreamGateway()
    real_gateway = app.state.gateway
    app.state.gateway = fake
    try:
        await _chat_once(client, headers)
    finally:
        app.state.gateway = real_gateway

    system = _system_prompt(fake)
    # 画像段 (与 qa 子图同款模板) 带偏好定性描述
    assert "学生画像" in system
    assert "讲解偏好: 类比优先、简洁" in system
    assert "兴趣: 篮球、动漫" in system
    assert "目标: 高数期末冲 90" in system
    # 兴趣情境化指令 (Khanmigo 式): 情境贴兴趣, 不改知识点
    assert "不得生硬堆砌兴趣词" in system
    # P2-7 脱敏口径: prompt 不含掌握度小数 (本用户本就无掌握度, 双保险断言格式)
    assert "p_mastery" not in system


async def test_put_invalidates_summary_cache_immediately(client):
    app = client._transport.app
    _uid, headers = await _register_login(client, "pref-cache")

    r = await client.put("/api/preferences", headers=headers, json=PREFS)
    assert r.status_code == 200

    fake = _CaptureStreamGateway()
    real_gateway = app.state.gateway
    app.state.gateway = fake
    try:
        # 第一次问答: 摘要 (含旧兴趣) 写入 Redis 60s 缓存
        await _chat_once(client, headers)
        assert "兴趣: 篮球、动漫" in _system_prompt(fake)

        # 改偏好 (换兴趣与目标) -> PUT 内部立即删缓存桶
        r = await client.put(
            "/api/preferences",
            headers=headers,
            json=dict(PREFS, interests=["天文"], goal="考研数学一 120"),
        )
        assert r.status_code == 200

        # 第二次问答: 不等 TTL, 新偏好立即进 prompt
        await _chat_once(client, headers, message="再讲讲极限")
        system = _system_prompt(fake)
        assert "兴趣: 天文" in system and "目标: 考研数学一 120" in system
        assert "篮球" not in system
    finally:
        app.state.gateway = real_gateway


# ---- 本人隔离 ----


async def test_preferences_isolated_per_user(client):
    _uid_a, headers_a = await _register_login(client, "pref-iso-a")
    _uid_b, headers_b = await _register_login(client, "pref-iso-b")

    r = await client.put("/api/preferences", headers=headers_a, json=PREFS)
    assert r.status_code == 200

    # 他人 GET 只见自己的空缺省 (RLS 租户隔离 + user_id 主键)
    r = await client.get("/api/preferences", headers=headers_b)
    assert r.status_code == 200
    assert r.json()["interests"] == [] and r.json()["goal"] is None

    r = await client.get("/api/preferences", headers=headers_a)
    assert r.json()["interests"] == ["篮球", "动漫"]


# 说明: 导出/注销对 user_preferences 的覆盖已在
# test_insights_flow.test_export_includes_memory_tables 与
# test_lifecycle.test_account_deletion_wipes_all_tables 断言, 此处不重复。
