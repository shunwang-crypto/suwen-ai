"""M3-D2/M3-D3/F5-A3: 出题 Blueprint→interrupt→resume→生成→作答→BKT 直写 全链路。
FakeGateway 注入 GraphRuntime (PG Checkpointer 真实), 图挂起与恢复跨 HTTP 请求。"""
import json
import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

_BLUEPRINT = {
    "items": [
        {"kc_name": "泰勒公式", "type": "single", "difficulty": "easy"},
        {"kc_name": "拉格朗日余项", "type": "blank", "difficulty": "mid"},
    ],
    "summary": "泰勒公式基础卷",
}
_QUESTIONS = {
    "questions": [
        {
            "type": "single",
            "stem": "泰勒公式在 $x_0=0$ 处的特例称为?",
            "options": ["麦克劳林公式", "洛必达法则", "拉格朗日中值定理", "柯西公式"],
            "answer": "A",
            "explanation": "选 B 通常混淆了求极限工具与展开工具。",
            "kc_names": ["泰勒公式"],
            "difficulty": "easy",
            "source_hint": None,
        },
        {
            "type": "blank",
            "stem": "泰勒公式的余项 $R_n(x)$ 的拉格朗日形式依赖 $n+1$ 阶____。",
            "options": [],
            "answer": "导数|导函数",
            "explanation": "余项公式含 $f^{(n+1)}(\\xi)$。",
            "kc_names": ["拉格朗日余项"],
            "difficulty": "mid",
            "source_hint": None,
        },
    ]
}


class FakeQuizGateway:
    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        payload = _BLUEPRINT if req.feature == "quiz_blueprint" else _QUESTIONS
        if not req.json_mode:
            payload = {"text": "ok"}
        return ChatResult(
            text=json.dumps(payload, ensure_ascii=False),
            model="fake", tier=req.tier.value, prompt_tokens=5,
            completion_tokens=10, cost_cny=0.0, latency_ms=1, mode="live",
        )


async def _sse(client, method, url, headers, body) -> dict[str, list]:
    events: dict[str, list] = {}
    current = None
    async with client.stream(method, url, json=body, headers=headers, timeout=60) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current:
                events.setdefault(current, []).append(json.loads(line.split(":", 1)[1]))
    return events


async def test_quiz_interrupt_resume_grade_bkt(client):
    # 注入 FakeGateway 的 GraphRuntime (Checkpointer 走真实 PG)
    from eduagents_api.graph_runtime import GraphRuntime
    from eduagents_core.config import get_settings
    from eduagents_core.graphs.state import GraphDeps

    email = f"quiz-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    app = client._transport.app  # ASGITransport 持有 app
    settings = get_settings()
    runtime = GraphRuntime(
        settings,
        GraphDeps(settings=settings, gateway=FakeQuizGateway(), retrieval=None, web_search=None),
    )
    await runtime.start()
    old_runtime = app.state.graphs
    app.state.graphs = runtime
    try:
        # 1) 发起出题 -> interrupt 挂起, 带 blueprint 与 resume_url (M3-D2)
        events = await _sse(client, "POST", "/api/quiz", headers, {
            "topic": "泰勒公式", "num_questions": 2,
            "difficulty_mix": {"easy": 1, "mid": 1, "hard": 0},
        })
        assert "interrupt" in events, events
        intr = events["interrupt"][0]
        assert intr["kind"] == "blueprint_confirm" and intr["thread_id"].startswith("quiz:")
        thread_id = intr["thread_id"]

        # 2) 跨请求 resume (Checkpointer 恢复) -> 生成+校验+落库 -> done
        events2 = await _sse(
            client, "POST", intr["resume_url"], headers, {"value": {"approved": True}}
        )
        assert "done" in events2, events2
        result = events2["done"][0]["result"]
        assert len(result["questions"]) == 2
        quiz_id = result["quiz_id"]

        # 3) 持久化可查 (M3-D3: schema 全过才落库)
        r = await client.get(f"/api/quiz/{quiz_id}", headers=headers)
        questions = r.json()["questions"]
        assert len(questions) == 2

        # 4) 作答: 1 对 1 错 -> 判分/错题入本/BKT 审计日志 (F5-A3)
        q_single = next(q for q in questions if q["type"] == "single")
        q_blank = next(q for q in questions if q["type"] == "blank")
        r = await client.post(
            f"/api/quiz/{quiz_id}/attempts", headers=headers,
            json={"answers": [
                {"question_id": q_single["id"], "response": "A"},
                {"question_id": q_blank["id"], "response": "微分"},
            ]},
        )
        body = r.json()
        assert body["total"] == 2 and body["correct"] == 1
        per = {x["question_id"]: x for x in body["results"]}
        assert per[q_single["id"]]["correct"] is True
        assert per[q_blank["id"]]["correct"] is False

        from eduagents_api.db import tenant_session
        from eduagents_api.models import Attempt, ErrorItem, LearningEvent

        async with tenant_session(
            await _uid(client, email)
        ) as session:
            attempts = (await session.scalars(select(Attempt))).all()
            errors = (await session.scalars(select(ErrorItem))).all()
            learning_events = (
                await session.scalars(
                    select(LearningEvent).where(
                        LearningEvent.source_type == "quiz_attempt"
                    )
                )
            ).all()
        assert len(attempts) == 2
        assert len(errors) == 1  # 错题 100% 自动入本 (F6-A1)
        assert len(learning_events) == len(attempts)
        assert {event.source_id for event in learning_events} == {
            attempt.id for attempt in attempts
        }
        assert {event.question_id for event in learning_events} == {
            attempt.question_id for attempt in attempts
        }

        # 5) 越权 resume -> 404 (不泄漏存在性)
        email_b = f"quiz-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
        await client.post("/api/auth/register", json={"email": email_b, "password": "pass-12345"})
        rb = await client.post("/api/auth/login", json={"email": email_b, "password": "pass-12345"})
        headers_b = {"Authorization": f"Bearer {rb.json()['access_token']}"}
        r = await client.post(
            f"/api/threads/{thread_id}/resume", headers=headers_b,
            json={"value": {"approved": True}},
        )
        assert r.status_code == 404
    finally:
        app.state.graphs = old_runtime
        await runtime.stop()


async def _uid(client, email):
    from eduagents_api.db import plain_session
    from eduagents_api.models import User
    from sqlalchemy import select

    async with plain_session() as s:
        user = await s.scalar(select(User).where(User.email == email))
        return str(user.id)
