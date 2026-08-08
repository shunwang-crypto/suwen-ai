"""demo 磁带驱动器: 按 DEMO_SCRIPT §1-§6 的顺序一镜到底跑 demo 账号, 录制/验证磁带。

磁带口径 (HANDOFF §2): cassette key = hash(prompt 全文), 而 prompt 含画像/偏好段 ——
因此 demo 账号的**状态轨迹**必须在录制与演示两次执行中完全一致。本脚本保证这一点:
1. --reset: 把 demo 账号回到"seed 后零状态"基线 (保留 kb/docs/KC, 清掉一切派生状态);
2. 主流程: 从基线开始, 严格按演示剧本顺序执行每一步 (偏好→辅导预热→问答→出题→
   薄弱点卷→验证卷→兴趣换装), 每步断言响应里没有"开发桩"横幅 (录制期上游超时会
   静默降级 stub, 那样的磁带上台必翻车);
3. 用法:
   录制:  API/worker 以 GATEWAY_MODE=record 启动 → python scripts/record_demo_tape.py --reset
   验证:  API/worker 以 GATEWAY_MODE=replay + GATEWAY_REPLAY_FALLBACK_STUB=false 启动
          → python scripts/record_demo_tape.py --reset --expect-mode replay
   演示日: 先跑一次"验证"命令做彩排, 然后 --reset 后把键盘交给主讲人。

⚠ 演示纪律: 主讲人当天的每一句提问、出题参数、以及**简答题的作答文本**都必须与
本脚本录制时一字不差 (客观题判分是本地纯函数不受限, 简答题判分走 LLM 要吃磁带)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

DEMO_EMAIL = "demo@eduagents.cn"
DEMO_PASS = "demo-pass-123"
STUB_BANNER = "开发桩"

# 演示剧本的固定台词 (改这里 = 改演示剧本, 两边必须同步)
TUTOR_PROBLEM = "用导数定义求 f(x)=x^2 在 x=1 处的导数"
TUTOR_TURNS = ["这道题我不会", "直接告诉我答案吧", "我就要答案"]  # 第二次索要 -> 给答案+小结入画像
CHAT_QUESTION = "泰勒公式的拉格朗日余项是什么形式?"
QUIZ_TOPIC = "泰勒公式"
PREFERENCES = {
    "explanation_style": "analogy",
    "verbosity": "concise",
    "interests": ["篮球", "科幻"],
    "daily_minutes": 45,
    "goal": "高数期末冲 90 分",
}
INTEREST_SWAP = {**PREFERENCES, "interests": ["篮球"]}  # §6-5 兴趣情境化对比

PASS: list[str] = []


def ok(step: str, detail: str = "") -> None:
    PASS.append(step)
    print(f"  PASS {step}" + (f" ({detail})" if detail else ""))


def fail(step: str, detail: str) -> None:
    print(f"  FAIL {step}: {detail}")
    print(f"== demo 磁带驱动失败: {len(PASS)} 过 / 1 失败 ==")
    sys.exit(1)


def no_stub(step: str, text: str) -> None:
    if STUB_BANNER in text:
        fail(step, "响应含开发桩横幅 — 录制期上游降级为 stub, 该磁带不可用, 重录此步")


async def reset_demo_state() -> None:
    """demo 账号回基线: 保留 user/kb/docs/KC, 清一切派生状态 (幂等)。"""
    sys.path.insert(0, "apps/api/src")
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import (
        Attempt,
        ChatMessage,
        ChatSession,
        ErrorItem,
        LearnerInsight,
        MasteryLog,
        MasteryRecord,
        Plan,
        PlanAdjustment,
        Question,
        Quiz,
        User,
        UserPreference,
    )
    from sqlalchemy import delete, select
    from sqlalchemy import text as sql_text

    async with plain_session() as s:
        uid = await s.scalar(select(User.id).where(User.email == DEMO_EMAIL))
    if uid is None:
        raise SystemExit("demo 账号不存在, 先 make seed")
    tid = str(uid)
    async with tenant_session(tid) as s:
        quiz_ids = list(await s.scalars(select(Quiz.id).where(Quiz.user_id == uid)))
        # 子表先删 (无 relationship, UoW 不懂 FK 顺序)
        await s.execute(delete(Attempt).where(Attempt.user_id == uid))
        await s.execute(delete(ErrorItem).where(ErrorItem.user_id == uid))
        if quiz_ids:
            await s.execute(delete(Question).where(Question.quiz_id.in_(quiz_ids)))
            await s.execute(delete(Quiz).where(Quiz.id.in_(quiz_ids)))
        await s.execute(delete(MasteryLog).where(MasteryLog.user_id == uid))
        await s.execute(delete(MasteryRecord).where(MasteryRecord.user_id == uid))
        await s.execute(delete(LearnerInsight).where(LearnerInsight.user_id == uid))
        await s.execute(delete(UserPreference).where(UserPreference.user_id == uid))
        await s.execute(delete(ChatMessage).where(ChatMessage.user_id == uid))
        await s.execute(delete(ChatSession).where(ChatSession.user_id == uid))
        await s.execute(delete(PlanAdjustment).where(PlanAdjustment.user_id == uid))
        await s.execute(delete(Plan).where(Plan.user_id == uid))
    # LangGraph checkpointer: thread_id 含 user_id, 清掉该用户全部图状态
    async with plain_session() as s:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            try:
                await s.execute(
                    sql_text(f"DELETE FROM {table} WHERE thread_id LIKE :pat"),
                    {"pat": f"%{tid}%"},
                )
            except Exception:  # noqa: BLE001 — 表不存在 (checkpointer 未初始化) 可忽略
                pass
    # Redis: 画像摘要缓存 + 反思计数器
    from eduagents_core.config import get_settings
    from redis.asyncio import Redis

    r = Redis.from_url(get_settings().redis_url)
    try:
        async for key in r.scan_iter(f"profile_summary:{tid}*"):
            await r.delete(key)
        async for key in r.scan_iter("reflect:*"):
            await r.delete(key)
    finally:
        await r.aclose()
    print(f"  RESET demo 账号派生状态已清空 (uid={tid[:8]}…)")


def sse(client: httpx.Client, method: str, url: str, token: str, body: dict) -> dict:
    events: dict[str, list] = {}
    current = None
    with client.stream(
        method, url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=300
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        for line in resp.iter_lines():
            if line.startswith("event:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current:
                events.setdefault(current, []).append(json.loads(line.split(":", 1)[1]))
    return events


def run_quiz(
    c: httpx.Client, token: str, step: str, payload: dict, submit_pattern: list[str] | None
) -> None:
    """出题→确认→(可选)按固定模式作答。固定模式保证录制/演示两轮判分输入一致。"""
    ev = sse(c, "POST", "/api/quiz", token, payload)
    if "interrupt" not in ev:
        fail(step, f"无 interrupt: {list(ev.keys())}")
    intr = ev["interrupt"][0]
    ev2 = sse(c, "POST", intr["resume_url"], token, {"value": {"approved": True}})
    result = ev2.get("done", [{}])[0].get("result", {})
    questions = result.get("questions", [])
    if not questions:
        fail(step, "resume 后无题目")
    for q in questions:
        no_stub(step, json.dumps(q, ensure_ascii=False))
    if submit_pattern is not None:
        answers = []
        for i, q in enumerate(questions):
            kind = q.get("type")
            if kind in ("single", "multi"):
                resp = submit_pattern[i % len(submit_pattern)]
            elif kind == "blank":
                resp = "0"
            else:
                resp = "不会"  # 简答固定文本: 判分 LLM 的磁带 key 依赖它, 不许改
            answers.append({"question_id": q["id"], "response": resp})
        r = c.post(
            f"/api/quiz/{result['quiz_id']}/attempts",
            json={"answers": answers},
            headers={"Authorization": f"Bearer {token}"},
        )
        graded = r.json()
        no_stub(step, json.dumps(graded, ensure_ascii=False))
        ok(step, f"{len(questions)} 题, 判分 {graded['correct']}/{graded['total']}")
    else:
        ok(step, f"{len(questions)} 题 (蓝图演示, 不作答)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--reset", action="store_true", help="先把 demo 账号回到基线")
    parser.add_argument(
        "--expect-mode", default="record", choices=["record", "replay", "live"],
        help="校验网关模式, 防止在错误模式下跑 (replay 验证时传 replay)",
    )
    args = parser.parse_args()

    if args.reset:
        asyncio.run(reset_demo_state())

    c = httpx.Client(base_url=args.base, timeout=300)
    health = c.get("/api/health/ready").json()
    if health["gateway_mode"] != args.expect_mode:
        fail("health", f"gateway_mode={health['gateway_mode']}, 期望 {args.expect_mode}")
    ok(f"health (gateway={health['gateway_mode']})")

    r = c.post("/api/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASS})
    if r.status_code != 200:
        fail("login", r.text)
    token = r.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    kbs = c.get("/api/kb", headers=auth).json()
    if not kbs:
        fail("kb", "demo 账号无知识库, 先 make seed")
    kb_id = kbs[0]["id"]
    ok("login + kb", kbs[0]["name"])

    # P.1 偏好 (DEMO §6 会前准备)
    r = c.put("/api/preferences", json=PREFERENCES, headers=auth)
    if r.status_code != 200:
        fail("preferences", r.text)
    ok("P.1 偏好设置")

    # P.2 辅导预热: 两次索要答案 -> 给答案 + 小结写画像 (产出"AI 记住的你"条目)
    thread_local = None
    for i, msg in enumerate(TUTOR_TURNS):
        body = {"problem": TUTOR_PROBLEM, "message": msg}
        if thread_local:
            body["thread_local_id"] = thread_local
        r = c.post("/api/tutor", json=body, headers=auth)
        if r.status_code != 200:
            fail(f"tutor round{i}", r.text)
        data = r.json()
        thread_local = data.get("thread_local_id", thread_local)
        no_stub(f"tutor round{i}", json.dumps(data, ensure_ascii=False))
    ok("P.2 辅导预热 (3 轮, 小结入画像)")

    # §2 带引用问答
    ev = sse(c, "POST", "/api/chat", token, {"message": CHAT_QUESTION, "kb_ids": [kb_id]})
    if "citation" not in ev or "done" not in ev:
        fail("chat", f"事件缺失: {list(ev.keys())}")
    answer = "".join(t.get("text", "") for t in ev.get("token", []))
    no_stub("chat", answer or json.dumps(ev.get("done"), ensure_ascii=False))
    ok("§2 问答", f"{len(ev['citation'])} 条引用")

    # §3 出题 5 题 (UI 默认 mix) -> 固定模式作答 (部分错, 错题入本)
    run_quiz(
        c, token, "§3 出题+作答",
        {"topic": QUIZ_TOPIC, "kb_id": kb_id, "num_questions": 5,
         "difficulty_mix": {"easy": 2, "mid": 2, "hard": 1}},
        submit_pattern=["A", "B"],
    )

    # §6-2 薄弱点定向卷 (看蓝图理由, 不作答 — 与演示动作一致)
    run_quiz(
        c, token, "§6-2 薄弱点卷",
        {"from_weak_points": True, "kb_id": kb_id, "num_questions": 5},
        submit_pattern=None,
    )

    # §6-3 验证一下: 取当前最薄弱 KC 出 3 题并作答 (重估)
    profile = c.get("/api/profile", headers=auth).json()
    mastery = profile.get("mastery") or profile.get("kcs") or []
    if not mastery:
        fail("§6-3 验证卷", "画像无掌握度记录")
    weakest = min(mastery, key=lambda m: m.get("p_mastery", 1.0))
    kc_name = weakest.get("kc_name") or weakest.get("name")
    run_quiz(
        c, token, "§6-3 验证卷",
        {"topic": kc_name, "kc_names": [kc_name], "num_questions": 3,
         "difficulty_mix": {"easy": 3, "mid": 0, "hard": 0}},
        submit_pattern=["A"],
    )

    # §6-5 兴趣换装: 只留篮球 -> 同主题再出 3 题 (情境对比)
    r = c.put("/api/preferences", json=INTEREST_SWAP, headers=auth)
    if r.status_code != 200:
        fail("§6-5 兴趣换装", r.text)
    run_quiz(
        c, token, "§6-5 兴趣情境卷",
        {"topic": QUIZ_TOPIC, "kb_id": kb_id, "num_questions": 3,
         "difficulty_mix": {"easy": 2, "mid": 1, "hard": 0}},
        submit_pattern=None,
    )

    # 收尾: 开场卡数据源 (零 LLM, 顺带校验)
    r = c.get("/api/next-actions", headers=auth)
    items = r.json().get("items", [])
    ok("next-actions", f"{len(items)} 条推荐")

    print(f"== demo 磁带驱动通过: {len(PASS)} PASS (mode={health['gateway_mode']}) ==")


if __name__ == "__main__":
    main()
