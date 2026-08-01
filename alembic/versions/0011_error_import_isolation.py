"""isolate imported wrong-question provenance and clean synthetic attempts

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "error_items",
        sa.Column("original_response", sa.Text(), nullable=True),
    )

    # 0010 的确认逻辑把导入答案写成了 Attempt。先把它转存到错题自己的
    # original_response，再删除这些平台作答证据；导入题从此不再污染报告。
    op.execute(
        sa.text(
            """
            UPDATE error_items AS e
            SET original_response = source.response
            FROM (
                SELECT DISTINCT ON (e2.id)
                    e2.id AS error_item_id,
                    a.response
                FROM error_items AS e2
                JOIN attempts AS a ON a.question_id = e2.question_id
                WHERE e2.source_import_id IS NOT NULL
                ORDER BY e2.id, a.at DESC, a.id DESC
            ) AS source
            WHERE e.id = source.error_item_id
              AND e.original_response IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM attempts AS a
            USING error_items AS e
            WHERE e.source_import_id IS NOT NULL
              AND a.question_id = e.question_id
            """
        )
    )

    # deleting 是对象存储清理的持久化中间态。它不是对用户可见的“已删除”，
    # 清理失败时可由 API/worker 重试，数据库行不会被错误地提前移除。
    op.drop_constraint("ck_error_import_status", "error_imports", type_="check")
    op.create_check_constraint(
        "ck_error_import_status",
        "error_imports",
        "status IN ('draft', 'confirmed', 'deleting')",
    )

    # 报告快照是派生数据；旧快照可能按 0010 的待核验口径包含错题统计，
    # 清空后由下一次读取按新口径重建。
    op.execute(sa.text("DELETE FROM learning_report_snapshots"))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM error_imports WHERE status = 'deleting'"
        )
    )
    op.drop_constraint("ck_error_import_status", "error_imports", type_="check")
    op.create_check_constraint(
        "ck_error_import_status",
        "error_imports",
        "status IN ('draft', 'confirmed')",
    )
    op.drop_column("error_items", "original_response")
