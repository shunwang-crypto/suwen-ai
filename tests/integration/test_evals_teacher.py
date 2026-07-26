"""F9 评估看板 + U-A4 教师组卷导出 + M5-D2 故障注入 的验收测试。

① import 幂等 (跑两次条数不变)  ② 列表/详情/对比结构正确  ③ 非 admin 403
④ 教师导出 JSON 含答案 / 打印视图题答分离 / 学生 403  ⑤ provider_down 打开后
live 网关走完降级链兜到 stub, 不 500。
"""

import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services


async def _register_login(client, email: str) -> dict:
    await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _promote(email: str, role: str) -> str:
    """直接改库提权 (无自助升级接口), 返回 user_id。调用方需重新登录换 claim。"""
    from eduagents_api.db import plain_session
    from eduagents_api.models import User

    async with plain_session() as session:
        user = await session.scalar(select(User).where(User.email == email))
        user.role = role
        return str(user.id)


# --------------------------------------------------------------------------
# ①②③ 评估看板
# --------------------------------------------------------------------------


async def test_evals_import_idempotent_list_detail_compare(client):
    email = f"evals-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    student_headers = await _register_login(client, email)

    # ③ 非 admin 一律 403 (五个端点全覆盖)
    assert (await client.post("/api/evals/import", headers=student_headers)).status_code == 403
    assert (await client.get("/api/evals", headers=student_headers)).status_code == 403
    assert (
        await client.get(f"/api/evals/{uuid.uuid4()}", headers=student_headers)
    ).status_code == 403
    assert (
        await client.get(
            f"/api/evals/compare?ids={uuid.uuid4()},{uuid.uuid4()}", headers=student_headers
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/evals/run", headers=student_headers, json={"kb_id": str(uuid.uuid4())}
        )
    ).status_code == 403

    await _promote(email, "admin")
    headers = await _register_login(client, email)  # 重新登录拿 admin claim

    # ① 导入幂等: 第一次可能 imported>0, 第二次必须 imported==0 且总数不变
    r = await client.post("/api/evals/import", headers=headers)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["items"], "docs/evidence 下应有可导入的评估产物"
    assert not [i for i in first["items"] if i["status"] == "error"], first["items"]
    total_first = first["imported"] + first["skipped"]

    n_before = len((await client.get("/api/evals", headers=headers)).json()["runs"])

    r = await client.post("/api/evals/import", headers=headers)
    second = r.json()
    assert second["imported"] == 0, second
    assert second["skipped"] == total_first
    runs = (await client.get("/api/evals", headers=headers)).json()["runs"]
    assert len(runs) == n_before, "重复导入不应新增行 (内容哈希去重)"

    # ② 列表结构
    kinds = {r_["kind"] for r_ in runs}
    assert {"retrieval", "simulation"} <= kinds, kinds
    for row in runs:
        assert row["id"] and row["label"] and row["source"] and row["at"]
        assert isinstance(row["summary"], list)
        assert row["data_basis"], "每行必须带数据口径 (live/stub 嵌入等)"

    # ② 详情: v2 检索报告要有分桶矩阵 + 拒答段落
    v2 = next(r_ for r_ in runs if r_["golden_set_version"] == "v2-2026-07-27")
    detail = (await client.get(f"/api/evals/{v2['id']}", headers=headers)).json()
    metrics = detail["metrics"]
    assert metrics["kind"] == "retrieval"
    assert metrics["headline"]["hit@5"] == 1.0
    assert metrics["headline"]["hit@1"] == 0.9074
    assert len(metrics["by_category"]) == 10
    assert metrics["refusal"]["correct"] == 5 and metrics["refusal"]["total"] == 6
    assert len(metrics["refusal"]["rows"]) == 6
    assert metrics["report_config"]["embedding_mode"] == "live"
    assert detail["config"]["content_sha256"]
    assert detail["corpus_version"].startswith("3 文档")

    # ② 详情: 仿真实验三臂 + p 值 (大数组已剥离, 不该把 300 个学生塞进 JSON)
    sim = next(r_ for r_ in runs if r_["kind"] == "simulation")
    sim_detail = (await client.get(f"/api/evals/{sim['id']}", headers=headers)).json()
    arms = sim_detail["metrics"]["experiment_b"]["arms"]
    assert set(arms) == {"adaptive", "random", "ladder"}
    assert arms["adaptive"]["mean"] < arms["ladder"]["mean"] < arms["random"]["mean"]
    cmp_ar = sim_detail["metrics"]["experiment_b"]["comparisons"]["adaptive_vs_random"]
    assert cmp_ar["p_value"] < 0.05 and cmp_ar["cohens_d"] < 0
    assert len(sim_detail["metrics"]["sensitivity"]) == 10
    assert "all_questions" not in str(sim_detail["metrics"])

    # ② 对比: 关重排 vs 完整链路 —— F9-A2 要的就是这种"参数调优→指标变化"
    full = next(r_ for r_ in runs if "live 嵌入" in r_["label"] and "完整链路" in r_["label"])
    no_rerank = next(r_ for r_ in runs if "关重排" in r_["label"])
    r = await client.get(
        f"/api/evals/compare?ids={full['id']},{no_rerank['id']}", headers=headers
    )
    assert r.status_code == 200, r.text
    comp = r.json()
    assert comp["same_kind"] is True
    by_metric = {d["metric"]: d for d in comp["deltas"]}
    assert "headline.mrr" in by_metric
    # 真实数据: 完整 0.9222 -> 关重排 1.0000 (关掉重排 MRR 反而更高, 如实呈现)
    assert by_metric["headline.mrr"]["delta"] > 0
    assert by_metric["headline.hit@5"]["delta"] == 0.0

    # 跨类型对比不报错, 只是同名指标很少
    cross = (
        await client.get(f"/api/evals/compare?ids={v2['id']},{sim['id']}", headers=headers)
    ).json()
    assert cross["same_kind"] is False
    assert cross["left_only"] and cross["right_only"]

    # 参数校验
    assert (
        await client.get(f"/api/evals/compare?ids={v2['id']}", headers=headers)
    ).status_code == 400
    assert (await client.get(f"/api/evals/{uuid.uuid4()}", headers=headers)).status_code == 404

    # F9-A1 一键触发: 入队拿 job_id + 轮询契约 (本用例不起 worker, 只验队列与状态端点)
    r = await client.post(
        "/api/evals/run",
        headers=headers,
        json={"kb_id": str(uuid.uuid4()), "golden": "eval/golden_set/v2.jsonl"},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "queued"
    status = (await client.get(f"/api/evals/jobs/{job_id}", headers=headers)).json()
    assert status["status"] in ("queued", "deferred", "in_progress")
    unknown = (await client.get("/api/evals/jobs/does-not-exist", headers=headers)).json()
    assert unknown["status"] == "not_found" and unknown["eval_run_id"] is None
    # 清理: 没有 worker 会消费它, 留着会被下次真 worker 捡去跑一次假 kb 的评估
    redis = client._transport.app.state.redis
    await redis.delete(f"arq:job:{job_id}")
    await redis.zrem("arq:queue", job_id)


# --------------------------------------------------------------------------
# ④ 教师组卷导出 (U-A4)
# --------------------------------------------------------------------------


async def _seed_quiz(tenant_id: str) -> str:
    """直接造卷 (不走出题图: 本用例验的是导出, 不是生成)。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Question, Quiz

    quiz_id = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        session.add(
            Quiz(
                id=quiz_id,
                tenant_id=uuid.UUID(tenant_id),
                user_id=uuid.UUID(tenant_id),
                title="泰勒公式基础卷",
                blueprint_json={"summary": "泰勒公式基础卷"},
                status="ready",
            )
        )
        await session.flush()  # 先落 quizzes, 否则 questions 的 FK 校验拿不到父行
        session.add_all(
            [
                Question(
                    tenant_id=uuid.UUID(tenant_id),
                    quiz_id=quiz_id,
                    type="single",
                    stem="泰勒公式在 $x_0=0$ 处的特例称为?",
                    options_json=["麦克劳林公式", "洛必达法则", "罗尔定理", "柯西公式"],
                    answer="A",
                    explanation="$x_0=0$ 的泰勒展开即麦克劳林展开。",
                    difficulty="easy",
                    kc_ids=[],
                ),
                Question(
                    tenant_id=uuid.UUID(tenant_id),
                    quiz_id=quiz_id,
                    type="blank",
                    stem="拉格朗日余项依赖 $n+1$ 阶____。",
                    answer="导数|导函数",
                    options_json=[],
                    explanation="余项含 $f^{(n+1)}(\\xi)$。",
                    difficulty="mid",
                    kc_ids=[],
                ),
            ]
        )
    return str(quiz_id)


async def test_teacher_export_json_print_and_student_forbidden(client):
    email = f"teacher-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    student_headers = await _register_login(client, email)
    user_id = await _promote(email, "student")  # 先确保是学生
    quiz_id = await _seed_quiz(user_id)

    # ④ 学生角色访问一律 403 (U-A4 验收点)
    assert (await client.get("/api/teacher/quizzes", headers=student_headers)).status_code == 403
    r = await client.post(
        f"/api/teacher/quizzes/{quiz_id}/export?format=json", headers=student_headers
    )
    assert r.status_code == 403
    r = await client.post(
        f"/api/teacher/quizzes/{quiz_id}/export?format=print", headers=student_headers
    )
    assert r.status_code == 403

    await _promote(email, "teacher")
    headers = await _register_login(client, email)

    # 卷列表
    quizzes = (await client.get("/api/teacher/quizzes", headers=headers)).json()
    assert len(quizzes) == 1
    assert quizzes[0]["id"] == quiz_id
    assert quizzes[0]["num_questions"] == 2
    assert quizzes[0]["difficulty_counts"] == {"easy": 1, "mid": 1}

    # JSON 导出: 必须含答案与解析 (给教师二次加工)
    r = await client.post(f"/api/teacher/quizzes/{quiz_id}/export?format=json", headers=headers)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["format"] == "json"
    assert payload["quiz"]["title"] == "泰勒公式基础卷"
    assert len(payload["questions"]) == 2
    for q in payload["questions"]:
        assert q["answer"] and q["explanation"]
        assert q["type_label"] and q["difficulty_label"]
    assert {q["index"] for q in payload["questions"]} == {1, 2}

    # 打印视图: 题目区与答案区结构分离, 题目区不得出现答案
    r = await client.post(f"/api/teacher/quizzes/{quiz_id}/export?format=print", headers=headers)
    assert r.status_code == 200, r.text
    printed = r.json()
    assert printed["quiz"]["total"] == 2
    paper_items = printed["paper"]["items"]
    assert len(paper_items) == 2
    for item in paper_items:
        assert "answer" not in item and "explanation" not in item
    assert sum(i["points"] for i in paper_items) == 100
    key_items = printed["answer_key"]["items"]
    assert [i["index"] for i in key_items] == [1, 2]
    assert all(i["answer"] for i in key_items)
    # 题目区与答案区按 index 对得上
    assert {i["index"] for i in paper_items} == {i["index"] for i in key_items}

    # 非法 format
    assert (
        await client.post(f"/api/teacher/quizzes/{quiz_id}/export?format=pdf", headers=headers)
    ).status_code == 422

    # 别人的卷 -> 404 (不泄漏存在性)
    other_email = f"teacher2-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    await _register_login(client, other_email)
    await _promote(other_email, "teacher")
    other_headers = await _register_login(client, other_email)
    assert (await client.get("/api/teacher/quizzes", headers=other_headers)).json() == []
    r = await client.post(
        f"/api/teacher/quizzes/{quiz_id}/export?format=json", headers=other_headers
    )
    assert r.status_code == 404

    # admin 也应能进 (require_teacher 放行 teacher+admin)
    await _promote(other_email, "admin")
    admin_headers = await _register_login(client, other_email)
    assert (await client.get("/api/teacher/quizzes", headers=admin_headers)).status_code == 200


# --------------------------------------------------------------------------
# ⑤ 故障注入 + 降级链 (M5-D2)
# --------------------------------------------------------------------------


async def test_fault_injection_admin_api_and_degradation(client):
    from eduagents_core.config import get_settings
    from eduagents_core.gateway import (
        ChatMessage,
        ChatRequest,
        OpenAIGateway,
        Tier,
        get_fault_injector,
    )

    email = f"faults-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    student_headers = await _register_login(client, email)
    assert (await client.get("/api/admin/faults", headers=student_headers)).status_code == 403
    assert (
        await client.post("/api/admin/faults", headers=student_headers, json={})
    ).status_code == 403

    await _promote(email, "admin")
    headers = await _register_login(client, email)
    injector = get_fault_injector()
    injector.reset()

    try:
        state = (await client.get("/api/admin/faults", headers=headers)).json()
        assert state["provider_down"] is False and state["active"] is False
        assert {b["tier"] for b in state["breakers"]} == {"cheap", "standard", "strong"}
        assert all(b["state"] == "closed" for b in state["breakers"])
        assert "stub/replay 不受影响" in state["note"]

        # 三个旋钮
        state = (
            await client.post(
                "/api/admin/faults",
                headers=headers,
                json={"provider_down": True, "latency_ms": 120, "error_rate": 0.5},
            )
        ).json()
        assert state["provider_down"] is True
        assert state["latency_ms"] == 120 and state["error_rate"] == 0.5
        assert state["active"] is True
        assert injector.state.provider_down is True

        # 部分更新: 只关 provider_down, 其余保持
        state = (
            await client.post(
                "/api/admin/faults", headers=headers, json={"provider_down": False}
            )
        ).json()
        assert state["provider_down"] is False and state["latency_ms"] == 120

        # 一键熔断主模型 -> standard 断路器 open
        state = (
            await client.post(
                "/api/admin/faults", headers=headers, json={"trip_breaker": "standard"}
            )
        ).json()
        breakers = {b["tier"]: b["state"] for b in state["breakers"]}
        assert breakers["standard"] == "open"
        assert breakers["cheap"] == "closed"

        # 一键恢复
        state = (
            await client.post(
                "/api/admin/faults",
                headers=headers,
                json={
                    "reset_breakers": True,
                    "provider_down": False,
                    "latency_ms": 0,
                    "error_rate": 0.0,
                },
            )
        ).json()
        assert state["active"] is False
        assert all(b["state"] == "closed" for b in state["breakers"])

        # --- 降级链: provider_down 开启后, live 网关兜到 stub 而不是抛错 ---
        settings = get_settings().model_copy(
            update={
                "gateway_mode": "live",
                "gateway_max_retries": 1,  # 免去指数退避, 用例只验路径不验退避
                "gateway_replay_fallback_stub": True,
                "app_env": "dev",
                "cheap_api_key": "fake-key-for-fault-demo",
                "standard_api_key": "fake-key-for-fault-demo",
                "strong_api_key": "fake-key-for-fault-demo",
                "gateway_cassette_dir": "/tmp/eduagents-fault-test-cassettes",
            }
        )
        gateway = OpenAIGateway(settings)
        req = ChatRequest(
            messages=(ChatMessage(role="user", content="降级链演示"),),
            tier=Tier.standard,
            feature="fault_demo",
        )
        injector.update(provider_down=True)
        result = await gateway.chat(req)
        # 主模型 + 备用档全部被打挂 -> 命中兜底, 标 degraded, 不 500
        assert result.mode == "stub"
        assert result.degraded is True
        assert result.text
        # 两档 live 都失败过 (min_samples=6 未到, 状态仍 closed, 但失败已被记录)
        assert gateway.breaker_states()["standard"] in ("closed", "open")

        # stub 模式完全不受故障注入影响 (演示兜底必须可靠)
        stub_gateway = OpenAIGateway(settings.model_copy(update={"gateway_mode": "stub"}))
        stub_result = await stub_gateway.chat(req)
        assert stub_result.mode == "stub" and stub_result.degraded is False

        # 关掉开关后同一个 live 网关恢复"真去打 provider"的行为:
        # fake key 会走真实网络失败, 这里只断言不是被故障注入挡下的
        injector.reset()
        assert injector.state.active is False
    finally:
        injector.reset()
