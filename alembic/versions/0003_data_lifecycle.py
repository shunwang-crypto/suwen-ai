"""data lifecycle: user FKs + account status/minor/plan cols + pending_cleanup outbox + org policies

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-27

数据生命周期与合规线 (docs/PRODUCTION_GAP_REVIEW.md P0-3/P0-4/P1-3/P1-4/P1-5):

1) user_id 外键 (P0-4 注销可物理执行):
   - 8 张表 FK(users.id) ON DELETE CASCADE:
     attempts / chat_sessions / error_items / mastery_log / mastery_records /
     plans / quizzes / org_members — 注销 = DELETE users 一行, 学情/会话/成员关系级联清空。
   - usage_log 唯一决定: FK(users.id) ON DELETE SET NULL (user_id 改可空), 不用 CASCADE。
     理由: usage_log 是成本对账/审计数据 (网络安全法日志留存 + 平台账单必须能与上游
     供应商账单对得上), CASCADE 会把已发生的成本随注销一起抹掉, 全站成本核算失真;
     SET NULL 在删号事务内自动断开用户关联 (user_id 置空 = 去标识化), users 行删除后
     email 等身份信息不复存在, 残留的 tenant_id UUID 无法回指到自然人, 满足个保法
     删除权"不再可关联"的口径, 同时保留行级 token/费用数据供对账与看板。

2) users 生命周期列: status ('active'|'disabled', P1-3 封禁) + birth_year/guardian_consent
   (P0-5 未成年线消费) + plan/daily_budget_cny_override (P2-14 套餐骨架, 预算线消费)。

3) pending_cleanup 删除 outbox (P1-5): 删除事务内写行替代"先 COMMIT 后 enqueue",
   worker 每分钟消费 (process_pending_cleanup)。无 RLS: 内部运维表, 无用户读路径。

4) org_members 补 UPDATE/DELETE 策略 (P1-4, 0002 自认的欠账): 成员可删自己行 (leave),
   org admin 走事务级 app.is_admin (D7 惯例, service 层先校验 admin 身份) 删本 org 行;
   orgs 补 UPDATE 策略 (邀请码轮换)。

从零重放安全性: 0001 用 create_all(当前 ORM 元数据) 建表, 因此新列在从零重放时已随
0001 建出 —— users 列全部用 ADD COLUMN IF NOT EXISTS 双路径幂等; user_id 外键不进
ORM 声明 (models.py 注释), 由本迁移唯一创建, 两条路径都不会撞 already exists;
pending_cleanup 用 ORM 子集 create_all (checkfirst) 零漂移。
"""
from collections.abc import Sequence

from eduagents_api.models import Base

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RLS 谓词片段 (与 0001/0002 同一口径)
_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_IS_ADMIN = "current_setting('app.is_admin', true) = 'true'"

# FK CASCADE 的 8 张表 (P0-4)
_CASCADE_TABLES = (
    "attempts",
    "chat_sessions",
    "error_items",
    "mastery_log",
    "mastery_records",
    "plans",
    "quizzes",
    "org_members",
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1) users 生命周期/未成年/套餐列 (IF NOT EXISTS: 从零重放时 0001 已按当前 ORM 建出)
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "status varchar(16) NOT NULL DEFAULT 'active'"
    )
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS birth_year integer")
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "guardian_consent boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan varchar(16) NOT NULL DEFAULT 'free'"
    )
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_budget_cny_override double precision"
    )

    # 2) user_id 外键。存量库历史上无外键, 可能已有孤儿行 (用户被手工删过),
    #    加约束前先清掉 (迁移账户为超级用户, 不受 RLS 限制)。
    for table in _CASCADE_TABLES:
        op.execute(f"DELETE FROM {table} WHERE user_id NOT IN (SELECT id FROM users)")
        op.create_foreign_key(
            f"fk_{table}_user_id", table, "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
    # usage_log: SET NULL 留档 (决定与理由见文件头注释)
    op.execute("ALTER TABLE usage_log ALTER COLUMN user_id DROP NOT NULL")
    op.execute(
        "UPDATE usage_log SET user_id = NULL "
        "WHERE user_id IS NOT NULL AND user_id NOT IN (SELECT id FROM users)"
    )
    op.create_foreign_key(
        "fk_usage_log_user_id", "usage_log", "users", ["user_id"], ["id"], ondelete="SET NULL"
    )

    # 3) pending_cleanup outbox (ORM 子集建表零漂移; checkfirst 幂等)
    Base.metadata.create_all(
        bind=bind, tables=[Base.metadata.tables["pending_cleanup"]]
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON pending_cleanup TO edu_app")

    # 4) org_members UPDATE/DELETE 策略 + orgs UPDATE 策略 (P1-4)
    #    - 成员可删自己的成员行 (leave)
    #    - org admin 的踢人/改角色/轮换邀请码: service 层校验 admin 后以事务级
    #      app.is_admin 放行 (同一表自引用子查询会触发 RLS 递归, 沿用 D7 惯例)
    op.execute(
        "CREATE POLICY org_members_update ON org_members FOR UPDATE "
        f"USING ({_IS_ADMIN}) WITH CHECK ({_IS_ADMIN})"
    )
    op.execute(
        "CREATE POLICY org_members_delete ON org_members FOR DELETE "
        f"USING (user_id = {_GUC_TENANT} OR {_IS_ADMIN})"
    )
    op.execute(
        "CREATE POLICY orgs_update ON orgs FOR UPDATE "
        f"USING (created_by = {_GUC_TENANT} OR {_IS_ADMIN}) "
        f"WITH CHECK (created_by = {_GUC_TENANT} OR {_IS_ADMIN})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS orgs_update ON orgs")
    op.execute("DROP POLICY IF EXISTS org_members_delete ON org_members")
    op.execute("DROP POLICY IF EXISTS org_members_update ON org_members")
    op.execute("DROP TABLE IF EXISTS pending_cleanup")
    op.drop_constraint("fk_usage_log_user_id", "usage_log", type_="foreignkey")
    # 回退可空前先把 NULL 行补回 tenant_id (个人语义下 tenant_id 即 user_id)
    op.execute("UPDATE usage_log SET user_id = tenant_id WHERE user_id IS NULL")
    op.execute("ALTER TABLE usage_log ALTER COLUMN user_id SET NOT NULL")
    for table in reversed(_CASCADE_TABLES):
        op.drop_constraint(f"fk_{table}_user_id", table, type_="foreignkey")
    for col in ("daily_budget_cny_override", "plan", "guardian_consent", "birth_year", "status"):
        op.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS {col}")
