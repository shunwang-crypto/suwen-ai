"""U-A4 教师组卷导出 (require_teacher; 学生一律 403)。

§3.0 写死的边界: 教师角色**只**保留"复用出题模块的组卷并导出"这一个最小可验证切片
—— 不做班级管理、不做班级学情、不做学生分发。本文件因此只有三个只读端点, 且完全
不碰 routers/quiz.py 的出题链路 (组卷仍走学生侧那条图, 教师侧只负责取出来和导出)。

导出两种口径:
- json  : 完整题目 + 答案 + 解析, 给二次加工/题库迁移
- print : 题目区与答案区**结构上分离** (paper / answer_key), 前端据此排出
          "题目页 + 分页符 + 答案页"的打印视图 —— 印卷子时不能把答案印在题目旁边
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select

from ..db import tenant_session
from ..deps import AuthContext, require_teacher
from ..models import ERROR_IMPORT_QUIZ_KIND, Question, Quiz

router = APIRouter(prefix="/api/teacher", tags=["teacher"])

_TYPE_LABELS = {
    "single": "单选题",
    "multi": "多选题",
    "blank": "填空题",
    "short": "简答题",
    "judge": "判断题",
}
_DIFF_LABELS = {"easy": "易", "mid": "中", "hard": "难"}


def _type_label(value: str) -> str:
    return _TYPE_LABELS.get(value, value)


def _diff_label(value: str) -> str:
    return _DIFF_LABELS.get(value, value)


async def _load_quiz(ctx: AuthContext, quiz_id: uuid.UUID) -> tuple[Quiz, list[Question]]:
    """取卷 + 题。别人的卷返回 404 (不是 403) —— 不泄漏"这个 id 存在"。"""
    async with tenant_session(ctx.tenant_id) as session:
        quiz = await session.get(Quiz, quiz_id)
        if (
            quiz is None
            or quiz.user_id != ctx.user_id
            or (quiz.blueprint_json or {}).get("kind") == ERROR_IMPORT_QUIZ_KIND
        ):
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        questions = (
            await session.scalars(
                # 新卷保留生成题序；旧卷无 ordinal 时按 id 稳定排序。
                select(Question)
                .where(Question.quiz_id == quiz_id)
                .order_by(Question.source_meta_json["quiz_ordinal"].as_integer(), Question.id)
            )
        ).all()
    return quiz, list(questions)


@router.get("/quizzes")
async def list_teacher_quizzes(ctx: AuthContext = Depends(require_teacher)):
    """该教师自己创建的卷 (含题量与难度分布, 免得列表页要 N+1 拉详情)。"""
    async with tenant_session(ctx.tenant_id) as session:
        quizzes = (
            await session.scalars(
                select(Quiz)
                .where(
                    Quiz.user_id == ctx.user_id,
                    or_(
                        Quiz.blueprint_json["kind"].astext.is_(None),
                        Quiz.blueprint_json["kind"].astext != ERROR_IMPORT_QUIZ_KIND,
                    ),
                )
                .order_by(Quiz.created_at.desc())
            )
        ).all()
        quiz_ids = [q.id for q in quizzes]
        stats: dict[uuid.UUID, Counter] = {qid: Counter() for qid in quiz_ids}
        if quiz_ids:
            rows = (
                await session.execute(
                    select(Question.quiz_id, Question.difficulty, func.count())
                    .where(Question.quiz_id.in_(quiz_ids))
                    .group_by(Question.quiz_id, Question.difficulty)
                )
            ).all()
            for quiz_id, difficulty, count in rows:
                stats[quiz_id][difficulty] = int(count)
    return [
        {
            "id": str(q.id),
            "title": q.title,
            "status": q.status,
            "created_at": q.created_at.isoformat() if q.created_at else None,
            "kb_id": str(q.kb_id) if q.kb_id else None,
            "num_questions": sum(stats[q.id].values()),
            "difficulty_counts": dict(stats[q.id]),
        }
        for q in quizzes
    ]


@router.post("/quizzes/{quiz_id}/export")
async def export_quiz(
    quiz_id: uuid.UUID,
    format: str = Query(default="json", pattern="^(json|print)$"),
    ctx: AuthContext = Depends(require_teacher),
):
    quiz, questions = await _load_quiz(ctx, quiz_id)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    created_at = quiz.created_at.isoformat() if quiz.created_at else None

    if format == "json":
        return {
            "format": "json",
            "exported_at": now,
            "exported_by": str(ctx.user_id),
            "quiz": {
                "id": str(quiz.id),
                "title": quiz.title,
                "status": quiz.status,
                "created_at": created_at,
                "kb_id": str(quiz.kb_id) if quiz.kb_id else None,
                "blueprint": quiz.blueprint_json,
            },
            "questions": [
                {
                    "index": i,
                    "id": str(q.id),
                    "type": q.type,
                    "type_label": _type_label(q.type),
                    "stem": q.stem,
                    "options": q.options_json,
                    "answer": q.answer,
                    "explanation": q.explanation,
                    "difficulty": q.difficulty,
                    "difficulty_label": _diff_label(q.difficulty),
                    "kc_ids": q.kc_ids,
                }
                for i, q in enumerate(questions, start=1)
            ],
        }

    # --- print: 题目区与答案区分离 ---
    total = len(questions)
    # 均分 100 分, 余数摊给前几题 (打印卷子上分值必须加得起来)
    base, extra = (100 // total, 100 % total) if total else (0, 0)
    return {
        "format": "print",
        "exported_at": now,
        "exported_by": str(ctx.user_id),
        "quiz": {
            "id": str(quiz.id),
            "title": quiz.title,
            "created_at": created_at,
            "total": total,
        },
        "paper": {
            "title": quiz.title or "练习卷",
            "instructions": f"共 {total} 题, 满分 100 分。请将答案写在答题区。",
            "items": [
                {
                    "index": i,
                    "type": q.type,
                    "type_label": _type_label(q.type),
                    "stem": q.stem,
                    "options": q.options_json,
                    "difficulty": q.difficulty,
                    "difficulty_label": _diff_label(q.difficulty),
                    "points": base + (1 if i <= extra else 0),
                }
                for i, q in enumerate(questions, start=1)
            ],
        },
        "answer_key": {
            "title": f"{quiz.title or '练习卷'} · 参考答案与解析",
            "items": [
                {"index": i, "answer": q.answer, "explanation": q.explanation}
                for i, q in enumerate(questions, start=1)
            ],
        },
    }
