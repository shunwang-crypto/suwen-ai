"""Durable assessment replay and deferred grading; no historical evidence rewrite.

Revision ID: 0038
Revises: 0037
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "assessment_submissions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("tenant_id", uuid_type, nullable=False),
        sa.Column(
            "user_id", uuid_type, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("quiz_id", uuid_type, sa.ForeignKey("quizzes.id", ondelete="CASCADE")),
        sa.Column("error_item_id", uuid_type, sa.ForeignKey("error_items.id", ondelete="CASCADE")),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_json", postgresql.JSONB, nullable=False),
        sa.Column("result_json", postgresql.JSONB, nullable=False),
        sa.Column("grading_attempts", sa.Integer, nullable=False),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "user_id", "kind", "idempotency_key", name="uq_assessment_submission_key"
        ),
        sa.CheckConstraint(
            "(kind = 'quiz' AND quiz_id IS NOT NULL AND error_item_id IS NULL) OR "
            "(kind = 'review' AND error_item_id IS NOT NULL AND quiz_id IS NULL)",
            name="ck_assessment_submission_source",
        ),
        sa.CheckConstraint(
            "grading_attempts BETWEEN 1 AND 3", name="ck_assessment_grading_attempts"
        ),
    )
    for column in ["tenant_id", "quiz_id", "error_item_id"]:
        op.create_index(f"ix_assessment_submissions_{column}", "assessment_submissions", [column])
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON assessment_submissions TO edu_app")
    op.execute("ALTER TABLE assessment_submissions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE assessment_submissions FORCE ROW LEVEL SECURITY")
    tenant = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(
        "CREATE POLICY tenant_isolation ON assessment_submissions "
        f"USING (tenant_id = {tenant}) WITH CHECK (tenant_id = {tenant})"
    )


def downgrade() -> None:
    op.drop_table("assessment_submissions")
