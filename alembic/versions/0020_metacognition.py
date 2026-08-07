"""Add observation-only metacognition and explain-back records.

The tables are user-owned, tenant-isolated, and intentionally do not feed the
mastery update path.  Calibration is computed on demand, so no mutable
aggregate row can outlive the underlying user's observations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_TABLES = ("metacognition_predictions", "explain_backs")


def _rls(table: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edu_app")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {_GUC_TENANT}) "
        f"WITH CHECK (tenant_id = {_GUC_TENANT})"
    )


def upgrade() -> None:
    op.create_table(
        "metacognition_predictions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "question_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("predicted_confidence", sa.SmallInteger(), nullable=True),
        sa.Column("predicted_correct", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "attempt_id",
            "question_id",
            name="uq_metacognition_prediction_attempt_question",
        ),
        sa.CheckConstraint(
            "predicted_confidence IS NULL OR predicted_confidence BETWEEN 1 AND 5",
            name="ck_metacognition_prediction_confidence",
        ),
    )
    op.create_index(
        "ix_metacognition_predictions_tenant_id",
        "metacognition_predictions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_metacognition_predictions_user_created",
        "metacognition_predictions",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_metacognition_predictions_attempt",
        "metacognition_predictions",
        ["attempt_id"],
    )

    op.create_table(
        "explain_backs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("prompt_span_ref", sa.String(256), nullable=True),
        sa.Column("student_text", sa.Text(), nullable=False),
        sa.Column("understanding_score", sa.Float(), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=False, server_default=""),
        sa.Column("gap", sa.Text(), nullable=False, server_default=""),
        sa.Column("retry_guidance", sa.Text(), nullable=False, server_default=""),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "understanding_score IS NULL OR understanding_score BETWEEN 0 AND 1",
            name="ck_explain_back_score",
        ),
    )
    op.create_index(
        "ix_explain_backs_tenant_id", "explain_backs", ["tenant_id"]
    )
    op.create_index(
        "ix_explain_backs_user_created", "explain_backs", ["user_id", "created_at"]
    )

    for table in _TABLES:
        _rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("ix_explain_backs_user_created", table_name="explain_backs")
    op.drop_index("ix_explain_backs_tenant_id", table_name="explain_backs")
    op.drop_index(
        "ix_metacognition_predictions_attempt",
        table_name="metacognition_predictions",
    )
    op.drop_index(
        "ix_metacognition_predictions_user_created",
        table_name="metacognition_predictions",
    )
    op.drop_index(
        "ix_metacognition_predictions_tenant_id",
        table_name="metacognition_predictions",
    )
    op.drop_table("explain_backs")
    op.drop_table("metacognition_predictions")
