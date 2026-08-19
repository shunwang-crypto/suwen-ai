"""Launch contracts for the question-resource -> assessment -> error path."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from .conftest import requires_services

pytestmark = requires_services
PASSWORD = "pass-12345"
FRONTEND_UPLOAD_DIALOG = (
    Path(__file__).resolve().parents[2]
    / "apps/web/components/resources/resource-upload-dialog.tsx"
)
FRONTEND_MATERIALS_PAGE = (
    Path(__file__).resolve().parents[2] / "apps/web/app/materials/page.tsx"
)
# UI 重构阶段 5 之后 ResourceLibrary 的宿主页: /courses (未归类) 与课程内 materials tab。
# /materials 只保留旧路由重定向, 所以宿主断言必须跟着搬, 否则这条契约会静默失效。
FRONTEND_COURSES_PAGE = (
    Path(__file__).resolve().parents[2] / "apps/web/app/courses/page.tsx"
)
FRONTEND_COURSE_MATERIALS_TAB = (
    Path(__file__).resolve().parents[2]
    / "apps/web/components/courses/course-materials-tab.tsx"
)
FRONTEND_RESOURCE_LIBRARY = (
    Path(__file__).resolve().parents[2]
    / "apps/web/components/resources/resource-library.tsx"
)
FRONTEND_RESOURCE_REVIEW = (
    Path(__file__).resolve().parents[2]
    / "apps/web/components/resources/resource-review.tsx"
)
FRONTEND_CONFIRM_DIALOG = (
    Path(__file__).resolve().parents[2] / "apps/web/components/ui/confirm-dialog.tsx"
)
EXPECTED_UPLOAD_TYPES = [
    "exam_paper",
    "mock_paper",
    "workbook",
    "worksheet",
    "homework",
    "other",
]


def test_frontend_resource_deep_link_and_dialog_contract():
    page = FRONTEND_MATERIALS_PAGE.read_text(encoding="utf-8")
    courses_page = FRONTEND_COURSES_PAGE.read_text(encoding="utf-8")
    materials_tab = FRONTEND_COURSE_MATERIALS_TAB.read_text(encoding="utf-8")
    library = FRONTEND_RESOURCE_LIBRARY.read_text(encoding="utf-8")
    review = FRONTEND_RESOURCE_REVIEW.read_text(encoding="utf-8")
    dialog = FRONTEND_CONFIRM_DIALOG.read_text(encoding="utf-8")
    # 宿主页: 未归类资源在 /courses, 课程内资源在 materials tab。
    assert "ResourceLibrary" in courses_page
    assert "ResourceLibrary" in materials_tab
    # 旧路由只做重定向, 不再自己渲染资源库 (阶段 5 契约)。
    assert 'router.replace("/courses")' in page
    assert "ResourceLibrary" not in page
    assert 'useSearchParams' in library
    assert 'searchParams.get("resource_id")' in library
    assert 'searchParams.get("question_id")' in library
    assert "initialQuestionId" in library and "initialQuestionId" in review
    # 返回列表时清掉深链 query, 且不写死路由 —— 资源库同时挂在 /courses 与
    # /courses/[kbId] 下, 写死路径会把用户踢出当前课程。
    assert "router.replace(`${url.pathname}" in library
    assert '"/materials"' not in library
    assert "useId" in dialog
    assert 'aria-modal="true"' in dialog
    assert 'event.key === "Escape"' in dialog


async def _register(client, prefix: str):
    email = f"{prefix}-{uuid.uuid4().hex[:10]}@test.eduagents.cn"
    response = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_frontend_resource_type_payloads_are_accepted_by_backend(client):
    source = FRONTEND_UPLOAD_DIALOG.read_text(encoding="utf-8")
    block = source.split("const RESOURCE_TYPES", 1)[1].split("];", 1)[0]
    submitted_types = re.findall(r'\["([a-z_]+)",', block)
    assert submitted_types == EXPECTED_UPLOAD_TYPES

    headers = await _register(client, "resource-type-contract")
    resource_ids: list[str] = []
    for resource_type in submitted_types:
        response = await client.post(
            "/api/resources",
            headers=headers,
            json={"title": f"契约测试-{resource_type}", "resource_type": resource_type},
        )
        assert response.status_code == 201, response.text
        resource_ids.append(response.json()["id"])

    for resource_id in resource_ids:
        assert (await client.delete(f"/api/resources/{resource_id}", headers=headers)).status_code == 204


async def test_reference_resources_cannot_enter_question_extraction(client):
    headers = await _register(client, "reference-guard")
    response = await client.post(
        "/api/resources",
        headers=headers,
        json={"title": "历史教材", "resource_type": "textbook"},
    )
    assert response.status_code == 201, response.text
    resource = response.json()
    assert resource["retrieval_policy"] == "reference"

    blocked = await client.post(f"/api/resources/{resource['id']}/extract", headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "REFERENCE_RESOURCE_NOT_EXTRACTABLE"

    assert (await client.delete(f"/api/resources/{resource['id']}", headers=headers)).status_code == 204


async def test_linked_kb_is_scoped_and_can_be_attached_to_question_resource(client):
    owner = await _register(client, "resource-kb-owner")
    other = await _register(client, "resource-kb-other")
    kb = await client.post("/api/kb", headers=owner, json={"name": "数学教材", "subject": "数学"})
    assert kb.status_code == 201, kb.text
    kb_id = kb.json()["id"]

    resource = await client.post(
        "/api/resources",
        headers=owner,
        json={"title": "关联知识点的试卷", "resource_type": "exam_paper", "linked_kb_id": kb_id},
    )
    assert resource.status_code == 201, resource.text
    assert resource.json()["linked_kb_id"] == kb_id

    forbidden = await client.post(
        "/api/resources",
        headers=other,
        json={"title": "越权绑定", "resource_type": "worksheet", "linked_kb_id": kb_id},
    )
    assert forbidden.status_code == 404
    assert forbidden.json()["code"] == "KB_NOT_FOUND"

    assert (await client.patch(
        f"/api/resources/{resource.json()['id']}",
        headers=owner,
        json={"linked_kb_id": None},
    )).status_code == 200
    assert (await client.delete(f"/api/resources/{resource.json()['id']}", headers=owner)).status_code == 204
    assert (await client.delete(f"/api/kb/{kb_id}", headers=owner)).status_code == 204


async def test_assessment_draft_delete_empty_answers_and_scoring_defaults(client):
    headers = await _register(client, "assessment-launch-contract")
    resource = await client.post(
        "/api/resources",
        headers=headers,
        json={"title": "真实试卷作答", "resource_type": "exam_paper"},
    )
    assert resource.status_code == 201, resource.text
    resource_id = resource.json()["id"]
    question = await client.post(
        f"/api/resources/{resource_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "请写出答案", "answer": "42", "points": 2},
    )
    assert question.status_code == 201, question.text
    question_id = question.json()["id"]
    verified = await client.post(
        f"/api/resource-questions/{question_id}/verify",
        headers=headers,
        json={"answer": "42"},
    )
    assert verified.status_code == 200, verified.text

    attempt = await client.post(
        "/api/assessment-attempts",
        headers=headers,
        json={"resource_id": resource_id, "attempt_type": "practice", "title": "期中考试记录"},
    )
    assert attempt.status_code == 201, attempt.text
    attempt_id = attempt.json()["id"]
    assert attempt.json()["attempt_type"] == "formal_exam"

    empty_submit = await client.post(f"/api/assessment-attempts/{attempt_id}/submit", headers=headers)
    assert empty_submit.status_code == 409
    assert empty_submit.json()["code"] == "ASSESSMENT_ANSWER_REQUIRED"
    empty_save = await client.put(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_id, "response": ""}]},
    )
    assert empty_save.status_code == 200
    assert empty_save.json()["answer_count"] == 0

    explicit_unanswered = await client.put(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_id, "unanswered": True}]},
    )
    assert explicit_unanswered.status_code == 200
    assert explicit_unanswered.json()["answer_count"] == 1
    assert (await client.post(f"/api/assessment-attempts/{attempt_id}/submit", headers=headers)).status_code == 200
    verified_result = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": True,
            "answers": [{"resource_question_id": question_id, "response": "未作答", "is_correct": False}],
        },
    )
    assert verified_result.status_code == 200, verified_result.text
    assert verified_result.json()["score"] == 0
    assert verified_result.json()["total_score"] == 2
    assert len(
        [
            item
            for item in (await client.get("/api/errors", headers=headers)).json()["items"]
            if item["resource_question_id"] == question_id
        ]
    ) == 1

    submitted_delete = await client.delete(f"/api/assessment-attempts/{attempt_id}", headers=headers)
    assert submitted_delete.status_code == 409
    assert submitted_delete.json()["code"] == "ASSESSMENT_NOT_DRAFT"

    draft = await client.post(
        "/api/assessment-attempts",
        headers=headers,
        json={"resource_id": resource_id},
    )
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["id"]
    saved = await client.put(
        f"/api/assessment-attempts/{draft_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_id, "response": "42"}]},
    )
    assert saved.status_code == 200, saved.text
    hidden = await client.get(f"/api/resources/{resource_id}/questions", headers=headers)
    assert hidden.status_code == 200
    assert hidden.json()[0].get("answer") is None

    deleted = await client.delete(f"/api/assessment-attempts/{draft_id}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/api/assessment-attempts/{draft_id}", headers=headers)).status_code == 404
    visible = await client.get(f"/api/resources/{resource_id}/questions", headers=headers)
    assert visible.status_code == 200
    assert visible.json()[0]["answer"] == "42"


async def test_assessment_put_replaces_answers_and_patch_is_partial(client):
    headers = await _register(client, "assessment-answer-replacement")
    resource = await client.post(
        "/api/resources",
        headers=headers,
        json={"title": "答案替换测试", "resource_type": "worksheet"},
    )
    assert resource.status_code == 201, resource.text
    resource_id = resource.json()["id"]
    question_ids: list[str] = []
    for stem in ("第一题", "第二题"):
        created = await client.post(
            f"/api/resources/{resource_id}/questions/manual",
            headers=headers,
            json={"type": "short", "stem": stem, "answer": stem},
        )
        assert created.status_code == 201, created.text
        question_id = created.json()["id"]
        verified = await client.post(
            f"/api/resource-questions/{question_id}/verify", headers=headers, json={}
        )
        assert verified.status_code == 200, verified.text
        question_ids.append(question_id)

    attempt = await client.post(
        "/api/assessment-attempts",
        headers=headers,
        json={"resource_id": resource_id},
    )
    assert attempt.status_code == 201, attempt.text
    attempt_id = attempt.json()["id"]

    saved = await client.put(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={
            "answers": [
                {"resource_question_id": question_ids[0], "response": "第一题答案"},
                {"resource_question_id": question_ids[1], "response": "第二题答案"},
            ]
        },
    )
    assert saved.status_code == 200 and saved.json()["answer_count"] == 2

    patched = await client.patch(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_ids[0], "response": "第一题修订"}]},
    )
    assert patched.status_code == 200 and patched.json()["answer_count"] == 2
    detail = await client.get(f"/api/assessment-attempts/{attempt_id}", headers=headers)
    assert detail.status_code == 200
    assert {
        answer["resource_question_id"]: answer["response"]
        for answer in detail.json()["answers"]
    } == {question_ids[0]: "第一题修订", question_ids[1]: "第二题答案"}

    # PUT is the complete current answer set: omitting q1 deletes its old answer.
    replaced = await client.put(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_ids[1], "response": "第二题答案"}]},
    )
    assert replaced.status_code == 200 and replaced.json()["answer_count"] == 1
    detail = await client.get(f"/api/assessment-attempts/{attempt_id}", headers=headers)
    assert [answer["resource_question_id"] for answer in detail.json()["answers"]] == [question_ids[1]]

    # An explicitly empty item also removes the persisted answer.
    cleared = await client.put(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_ids[1], "response": ""}]},
    )
    assert cleared.status_code == 200 and cleared.json()["answer_count"] == 0
    detail = await client.get(f"/api/assessment-attempts/{attempt_id}", headers=headers)
    assert detail.json()["answers"] == []
    submit = await client.post(f"/api/assessment-attempts/{attempt_id}/submit", headers=headers)
    assert submit.status_code == 409
    assert submit.json()["code"] == "ASSESSMENT_ANSWER_REQUIRED"

    # A cleared answer cannot be graded into an error or mastery event.
    save_again = await client.put(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_ids[1], "response": "第二题答案"}]},
    )
    assert save_again.status_code == 200
    assert (await client.post(f"/api/assessment-attempts/{attempt_id}/submit", headers=headers)).status_code == 200
    missing_grade = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": True,
            "answers": [
                {"resource_question_id": question_ids[0], "response": "第一题修订", "is_correct": False},
                {"resource_question_id": question_ids[1], "response": "第二题答案", "is_correct": False},
            ],
        },
    )
    assert missing_grade.status_code == 409
    assert missing_grade.json()["code"] == "ASSESSMENT_ANSWER_NOT_FOUND"
    verified = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": True,
            "answers": [
                {"resource_question_id": question_ids[1], "response": "第二题答案", "is_correct": False},
            ],
        },
    )
    assert verified.status_code == 200, verified.text
    errors = (await client.get("/api/errors", headers=headers)).json()["items"]
    assert [item["question"]["stem"] for item in errors] == ["第二题"]
    report = await client.get("/api/reports/learning", headers=headers)
    assert report.status_code == 200
    assert report.json()["metrics"]["attempt_count"] == 1
    assert sum(point["attempts"] for point in report.json()["trend"]) == 1

    empty_attempt = await client.post(
        "/api/assessment-attempts", headers=headers, json={"resource_id": resource_id}
    )
    assert empty_attempt.status_code == 201
    empty_id = empty_attempt.json()["id"]
    assert (
        await client.put(
            f"/api/assessment-attempts/{empty_id}/answers",
            headers=headers,
            json={"answers": []},
        )
    ).json()["answer_count"] == 0
    empty_submit = await client.post(f"/api/assessment-attempts/{empty_id}/submit", headers=headers)
    assert empty_submit.status_code == 409
    assert empty_submit.json()["code"] == "ASSESSMENT_ANSWER_REQUIRED"


async def test_assessment_subjective_scores_require_explicit_correctness_and_objective_grading(client):
    headers = await _register(client, "assessment-score-contract")
    resource = await client.post(
        "/api/resources",
        headers=headers,
        json={"title": "评分语义测试", "resource_type": "worksheet"},
    )
    assert resource.status_code == 201, resource.text
    resource_id = resource.json()["id"]
    questions: list[dict] = []
    for payload in (
        {"type": "short", "stem": "主观题", "answer": "答案", "points": 10},
        {"type": "single", "stem": "客观题", "options": ["A", "B", "C", "D"], "answer": "B"},
    ):
        created = await client.post(
            f"/api/resources/{resource_id}/questions/manual", headers=headers, json=payload
        )
        assert created.status_code == 201, created.text
        question_id = created.json()["id"]
        assert (await client.post(f"/api/resource-questions/{question_id}/verify", headers=headers, json={})).status_code == 200
        questions.append({"id": question_id, **payload})

    attempt = await client.post(
        "/api/assessment-attempts", headers=headers, json={"resource_id": resource_id}
    )
    assert attempt.status_code == 201
    attempt_id = attempt.json()["id"]
    saved = await client.put(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={
            "answers": [
                {"resource_question_id": questions[0]["id"], "response": "学生答案"},
                {"resource_question_id": questions[1]["id"], "response": "A"},
            ]
        },
    )
    assert saved.status_code == 200
    assert (await client.post(f"/api/assessment-attempts/{attempt_id}/submit", headers=headers)).status_code == 200

    staged = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": False,
            "answers": [
                {"resource_question_id": questions[0]["id"], "response": "学生答案", "score": 5, "max_score": 10},
                # Deliberately wrong client values: objective grading is server-owned.
                {"resource_question_id": questions[1]["id"], "response": "A", "is_correct": True, "score": 1, "max_score": 1},
            ],
        },
    )
    assert staged.status_code == 200, staged.text
    assert staged.json()["status"] == "submitted"
    no_correctness = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": True,
            "answers": [
                {"resource_question_id": questions[0]["id"], "response": "学生答案", "score": 5, "max_score": 10},
                {"resource_question_id": questions[1]["id"], "response": "A"},
            ],
        },
    )
    assert no_correctness.status_code == 409
    assert no_correctness.json()["code"] == "ANSWER_CORRECTNESS_REQUIRED"

    invalid_score = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": False,
            "answers": [{"resource_question_id": questions[0]["id"], "response": "学生答案", "score": 11, "max_score": 10}],
        },
    )
    assert invalid_score.status_code == 422
    assert invalid_score.json()["code"] == "INVALID_ASSESSMENT_SCORE"
    zero_max = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": False,
            "answers": [{"resource_question_id": questions[0]["id"], "response": "学生答案", "score": 0, "max_score": 0}],
        },
    )
    assert zero_max.status_code == 422
    assert zero_max.json()["code"] == "INVALID_ASSESSMENT_SCORE"

    verified = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": True,
            "answers": [
                {"resource_question_id": questions[0]["id"], "response": "学生答案", "is_correct": False, "score": 5, "max_score": 10},
                {"resource_question_id": questions[1]["id"], "response": "A"},
            ],
        },
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "graded"
    detail = await client.get(f"/api/assessment-attempts/{attempt_id}", headers=headers)
    answers = {item["resource_question_id"]: item for item in detail.json()["answers"]}
    assert answers[questions[0]["id"]]["score"] == 5
    assert answers[questions[0]["id"]]["is_correct"] is False
    assert answers[questions[1]["id"]]["is_correct"] is False
    assert answers[questions[1]["id"]]["score"] == 0

    retry = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": True,
            "answers": [
                {"resource_question_id": questions[0]["id"], "response": " 学生答案 ", "is_correct": False, "score": 5, "max_score": 10},
                {"resource_question_id": questions[1]["id"], "response": "A"},
            ],
        },
    )
    assert retry.status_code == 200
    assert retry.json()["mastery_changes"] == []
