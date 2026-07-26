"""feedback channel table + RLS (P2-1)

Revision ID: 0004
Revises: 0002
Create Date: 2026-07-27

用户反馈通道 (docs/PRODUCTION_GAP_REVIEW.md P2-1):
- 手写 op.create_table (不走 ORM metadata): models.py 归并行的 P0 工作流所有,
  本迁移与 routers/feedback.py 的 text SQL 配套, 零 ORM 依赖。
- request_id 列用于把报障与结构化日志/usage_log 串起来 (前端取最近一次响应的
  X-Request-ID 一起提交)。
- RLS: 用户读写自己的行 (tenant_id = app.tenant_id GUC); admin 走事务级
  app.is_admin GUC 读全量并关闭工单 (惯例同 0001 的 usage_log / D7)。
  无 DELETE 策略 -> 应用角色默认拒绝; 留存清理 (worker/retention.py) 走
  migration 账户 (表 owner/超级用户), 不受此限制。

主线接线 (已完成): 0003 (数据生命周期/外键迁移) 落盘后, down_revision 已按
本文件原注释要求从 "0002" 改指 "0003", 链条 0001→0002→0003→0004 单头。
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 0001/0002 同一口径的 RLS 谓词片段
_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_IS_ADMIN = "current_setting('app.is_admin', true) = 'true'"


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False, index=True
        ),
        sa.Column(
            "user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False, index=True
        ),
        sa.Column("request_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("status", sa.String(8), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # 与 0003 数据生命周期口径一致: 注销删 users 时反馈随之级联清除 (个保法删除权)
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_feedback_user_id", ondelete="CASCADE"
        ),
    )
    op.create_check_constraint(
        "ck_feedback_category",
        "feedback",
        "category IN ('bug', 'answer_wrong', 'abuse', 'other')",
    )
    op.create_check_constraint(
        "ck_feedback_status", "feedback", "status IN ('open', 'closed')"
    )
    # admin 列表按状态筛 + 时间倒序
    op.create_index("ix_feedback_status_created", "feedback", ["status", "created_at"])

    # 0001 的 default privileges 覆盖迁移角色后建的表, 与 0002 同款显式补齐更稳
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON feedback TO edu_app")

    op.execute("ALTER TABLE feedback ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE feedback FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY feedback_select ON feedback FOR SELECT "
        f"USING (tenant_id = {_GUC_TENANT} OR {_IS_ADMIN})"
    )
    op.execute(
        "CREATE POLICY feedback_insert ON feedback FOR INSERT "
        f"WITH CHECK (tenant_id = {_GUC_TENANT})"
    )
    # 仅 admin 可改 (关闭工单); 用户侧无编辑功能, 不放行 UPDATE
    op.execute(
        "CREATE POLICY feedback_update ON feedback FOR UPDATE "
        f"USING ({_IS_ADMIN}) WITH CHECK ({_IS_ADMIN})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS feedback_select ON feedback")
    op.execute("DROP POLICY IF EXISTS feedback_insert ON feedback")
    op.execute("DROP POLICY IF EXISTS feedback_update ON feedback")
    op.drop_index("ix_feedback_status_created", table_name="feedback")
    op.drop_table("feedback")
