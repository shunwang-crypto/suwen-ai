"""计划临时概念 (0034) 的集成契约。

覆盖：无资料计划的概念真的落库并出现在计划详情；有正式 KC 承接的不进临时层；
临时概念不产生任何掌握度记录（边界的运行时验证，与 test_plan_concepts 的静态守卫互补）。
"""

import uuid

from sqlalchemy import func, select

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"


async def _register_login(client, prefix="planconcept"):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _structure(kc_names: list[str]) -> dict:
    return {
        "stages": [
            {
                "name": "基础",
                "days": [
                    {
                        "day": 1,
                        "tasks": [
                            {
                                "title": "任务一",
                                "task_id": str(uuid.uuid4()),
                                "kc_names": kc_names,
                                "status": "pending",
                                "action_type": "practice",
                                "practice_count": 3,
                                "difficulty": "mid",
                            }
                        ],
                    }
                ],
            }
        ]
    }


async def _seed_plan_with_concepts(
    user_id: str, kc_names: list[str], *, kb_id: str | None = None, known: list[str] | None = None
) -> str:
    """直接落计划 + 临时概念（复用生产的收集逻辑，不复制判定）。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeComponent, Plan, PlanConcept
    from eduagents_api.plan_concepts import collect_plan_concepts

    structure = _structure(kc_names)
    async with tenant_session(uuid.UUID(user_id)) as session:
        if kb_id and known:
            for name in known:
                session.add(
                    KnowledgeComponent(
                        tenant_id=uuid.UUID(user_id),
                        kb_id=uuid.UUID(kb_id),
                        canonical_name=name,
                    )
                )
            await session.flush()
        plan = Plan(
            tenant_id=uuid.UUID(user_id),
            user_id=uuid.UUID(user_id),
            goal="两周入门算法",
            budget_hours=1.0,
            kb_id=uuid.UUID(kb_id) if kb_id else None,
            plan_mode="course_bound" if kb_id else "goal_only",
            grounding_status="kb_grounded" if kb_id else "unreferenced",
            status="active",
            structure_json=structure,
        )
        session.add(plan)
        await session.flush()
        for index, item in enumerate(collect_plan_concepts(structure, known or [])):
            session.add(
                PlanConcept(
                    tenant_id=uuid.UUID(user_id),
                    plan_id=plan.id,
                    display_name=item["display_name"],
                    normalized_name=item["normalized_name"],
                    ordinal=index,
                )
            )
        await session.flush()
        return str(plan.id)


async def test_plan_without_kb_exposes_provisional_concepts(client):
    user_id, headers = await _register_login(client)
    plan_id = await _seed_plan_with_concepts(user_id, ["Trie", "前缀和"])

    r = await client.get(f"/api/plans/{plan_id}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [item["name"] for item in body["provisional_concepts"]] == ["Trie", "前缀和"]
    assert all(item["status"] == "provisional" for item in body["provisional_concepts"])
    # 无资料 -> 依赖图为空；两者同时成立才说明"计划可读但不冒充知识点"
    assert body["kc_dependency"]["levels"] == []
    assert body["kc_dependency"]["primary_route"] == []


async def test_concepts_backed_by_real_kcs_are_not_provisional(client):
    user_id, headers = await _register_login(client)
    r = await client.post("/api/kb", json={"name": "算法", "subject": "计算机"}, headers=headers)
    kb_id = r.json()["id"]
    plan_id = await _seed_plan_with_concepts(
        user_id, ["Trie", "前缀和"], kb_id=kb_id, known=["Trie"]
    )

    r = await client.get(f"/api/plans/{plan_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert [item["name"] for item in r.json()["provisional_concepts"]] == ["前缀和"]


async def test_provisional_concepts_never_create_mastery_rows(client):
    """边界的运行时验证：临时概念不写 mastery_records / mastery_log。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import MasteryLog, MasteryRecord

    user_id, headers = await _register_login(client)
    plan_id = await _seed_plan_with_concepts(user_id, ["Trie", "前缀和"])
    r = await client.get(f"/api/plans/{plan_id}", headers=headers)
    assert r.status_code == 200

    async with tenant_session(uuid.UUID(user_id)) as session:
        records = await session.scalar(select(func.count()).select_from(MasteryRecord))
        logs = await session.scalar(select(func.count()).select_from(MasteryLog))
    assert records == 0
    assert logs == 0


async def test_provisional_concepts_are_scoped_to_their_owner(client):
    owner_id, _owner_headers = await _register_login(client, "owner")
    plan_id = await _seed_plan_with_concepts(owner_id, ["Trie"])

    _other_id, other_headers = await _register_login(client, "other")
    r = await client.get(f"/api/plans/{plan_id}", headers=other_headers)
    assert r.status_code == 404, r.text


async def test_deleting_the_plan_cascades_its_concepts(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Plan, PlanConcept

    user_id, _headers = await _register_login(client)
    plan_id = await _seed_plan_with_concepts(user_id, ["Trie"])

    async with tenant_session(uuid.UUID(user_id)) as session:
        plan = await session.get(Plan, uuid.UUID(plan_id))
        await session.delete(plan)
    async with tenant_session(uuid.UUID(user_id)) as session:
        left = await session.scalar(select(func.count()).select_from(PlanConcept))
    assert left == 0
