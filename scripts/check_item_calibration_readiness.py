#!/usr/bin/env python3
"""B3 前置检查: question-level Elo 难度校准是否具备数据条件。

**为什么先跑这个**: Elo 校准的成立前提是「同一道题被多个不同学习者作答」——难度是
从「谁答对了、谁答错了」的对比中估出来的。若每道题只有一个人答过, item rating 与
该用户的 ability 完全共线, 估不出难度, 只会把噪声当信号。

判定口径 (Wauters et al. 2012, Computers & Education 58(4)):
- N<200 时 proportion correct 等简易方法已足够可靠且与 IRT 高度相关;
- question-level Elo 需要每题有足够的**独立用户**曝光, 不是足够的作答次数。

只读, 不修改任何数据。退出码: 0 = 具备条件; 1 = 不具备; 2 = 查询出错。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from eduagents_api.db import get_sessionmaker, tenant_session
from sqlalchemy import text

# 经验门槛: 低于此值时 question-level Elo 的难度估计不可信。
MIN_UNIQUE_USERS_PER_ITEM = 10
# 至少这么多题达标, 才值得为 Elo 建表建管线。
MIN_QUALIFIED_ITEMS = 30

_SQL_UNIQUE_USERS = text("""
    SELECT unique_users, COUNT(*) AS question_count
    FROM (
        SELECT question_id, COUNT(DISTINCT user_id) AS unique_users
        FROM attempts
        GROUP BY question_id
    ) t
    GROUP BY unique_users
    ORDER BY unique_users
""")

_SQL_TOTALS = text("""
    SELECT
        (SELECT COUNT(*) FROM questions) AS questions_total,
        (SELECT COUNT(*) FROM attempts) AS attempts_total,
        (SELECT COUNT(DISTINCT question_id) FROM attempts) AS questions_answered,
        (SELECT COUNT(DISTINCT user_id) FROM attempts) AS users_with_attempts
""")

# 题挂在 quiz 下, quiz 属于单个用户 -> 跨用户复用需要显式共享机制。
_SQL_SHARED_ITEMS = text("""
    SELECT COUNT(*) FROM (
        SELECT q.id
        FROM questions q
        JOIN quizzes z ON z.id = q.quiz_id
        GROUP BY q.id
        HAVING COUNT(DISTINCT z.user_id) > 1
    ) t
""")

# questions/attempts/quizzes 只有 tenant_isolation 策略, 没有 admin 放行 ——
# 不存在合法的全局聚合路径。因此逐租户开 session 统计后在进程内汇总, 与
# vector_reconcile 处理 chunks 的做法一致 (零迁移, 不新增跨租户读面)。
# 个人租户 tenant_id == users.id (D14); 机构库另有 owner_type='org', 但题目/作答
# 始终挂在个人租户下, 故这里以用户为准。
_SQL_TENANTS = text("SELECT id FROM users")


async def collect() -> dict:
    """逐租户聚合后在进程内汇总 (RLS 无全局路径, 见 _SQL_TENANTS 注释)。"""
    questions_total = attempts_total = questions_answered = 0
    users_with_attempts = 0
    shared = 0
    tenant_count = 0
    # question_id 全局唯一, 且一道题只属于一个租户 -> 分布可跨租户直接相加。
    dist_map: dict[int, int] = {}

    async with get_sessionmaker()() as session:
        tenant_ids = [row[0] for row in (await session.execute(_SQL_TENANTS)).all()]

    for tenant_id in tenant_ids:
        async with tenant_session(tenant_id) as session:
            totals = (await session.execute(_SQL_TOTALS)).one()
            if not (int(totals.questions_total) or int(totals.attempts_total)):
                continue
            tenant_count += 1
            questions_total += int(totals.questions_total)
            attempts_total += int(totals.attempts_total)
            questions_answered += int(totals.questions_answered)
            users_with_attempts += int(totals.users_with_attempts)
            shared += int(await session.scalar(_SQL_SHARED_ITEMS) or 0)
            for row in (await session.execute(_SQL_UNIQUE_USERS)).all():
                users = int(row.unique_users)
                dist_map[users] = dist_map.get(users, 0) + int(row.question_count)

    dist = sorted(dist_map.items())
    qualified = sum(count for users, count in dist if users >= MIN_UNIQUE_USERS_PER_ITEM)
    single_user = sum(count for users, count in dist if users == 1)
    answered = questions_answered
    return {
        "tenants_with_data": tenant_count,
        "questions_total": questions_total,
        "questions_answered": answered,
        "attempts_total": attempts_total,
        "users_with_attempts": users_with_attempts,
        "shared_items": shared,
        "unique_user_distribution": [
            {"unique_users": u, "questions": c} for u, c in dist
        ],
        "single_user_items": single_user,
        "single_user_ratio": round(single_user / answered, 4) if answered else None,
        "qualified_items": qualified,
        "min_unique_users_per_item": MIN_UNIQUE_USERS_PER_ITEM,
        "min_qualified_items": MIN_QUALIFIED_ITEMS,
        "elo_viable": qualified >= MIN_QUALIFIED_ITEMS and shared > 0,
    }


def render(report: dict) -> None:
    print("题库与作答规模")
    print(f"  有数据的租户数            : {report['tenants_with_data']}")
    print(f"  questions 总数            : {report['questions_total']}")
    print(f"  被作答过的题              : {report['questions_answered']}")
    print(f"  attempts 总数             : {report['attempts_total']}")
    print(f"  有作答的独立用户          : {report['users_with_attempts']}")
    print(f"  被多个用户共享的题        : {report['shared_items']}")
    print()
    print("每题独立用户数分布")
    for row in report["unique_user_distribution"]:
        print(f"  unique_users={row['unique_users']:<4}: {row['questions']} 题")
    print()
    ratio = report["single_user_ratio"]
    if ratio is not None:
        print(f"仅 1 个用户作答的题占比    : {ratio:.1%}")
    print(f"达标题数 (≥{report['min_unique_users_per_item']} 用户): "
          f"{report['qualified_items']} / 需要 {report['min_qualified_items']}")
    print()
    if report["elo_viable"]:
        print("结论: 具备 question-level Elo 的数据条件。")
        return
    print("结论: 不具备 question-level Elo 的数据条件。")
    if report["shared_items"] == 0:
        print("  根因: 题目不跨用户复用 —— 每道题挂在单个用户的 quiz 下, 是即时生成的。")
        print("  item rating 与该用户 ability 完全共线, 估不出难度。")
    print("  建议: 先做稳定题目身份 (canonical_item_id / 共享题库), 让同一道题能被")
    print("  多人作答; 在那之前用 KC + 题型 + 标注难度的层级先验, 不做 item-level Elo。")


async def _run(args: argparse.Namespace) -> int:
    try:
        report = await collect()
    except Exception as exc:  # noqa: BLE001
        print(f"查询失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        render(report)
    return 0 if report["elo_viable"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
