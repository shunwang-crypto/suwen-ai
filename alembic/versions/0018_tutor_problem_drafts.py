"""reviewed tutor problem drafts backed by learning resources

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_GUC_CLEANUP = "current_setting('app.internal_cleanup', true) = 'true'"


def upgrade() -> None:
    op.create_table(
        "tutor_problem_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("selected_question_index", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="uploaded"),
        sa.Column("selected_pages_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("recognized_questions_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("draft_stem", sa.Text(), nullable=False, server_default=""),
        sa.Column("draft_options_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("known_conditions", sa.Text(), nullable=False, server_default=""),
        sa.Column("current_thought", sa.Text(), nullable=False, server_default=""),
        sa.Column("confirmed_stem", sa.Text(), nullable=False, server_default=""),
        sa.Column("confirmed_options_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("confirmed_problem_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("confirmed_page_no", sa.Integer(), nullable=True),
        sa.Column("thread_local_id", sa.String(64), nullable=True),
        sa.Column("warnings_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("recognition_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_tutor_drafts_user", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["resource_id"], ["learning_resources.id"], name="fk_tutor_drafts_resource", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("user_id", "thread_local_id", name="uq_tutor_drafts_user_thread"),
        sa.CheckConstraint(
            "status IN ('uploaded', 'recognizing', 'review', 'confirmed', 'failed')",
            name="ck_tutor_drafts_status",
        ),
        sa.CheckConstraint("recognition_attempts >= 0", name="ck_tutor_drafts_attempts"),
        sa.CheckConstraint(
            "selected_question_index IS NULL OR selected_question_index >= 0",
            name="ck_tutor_drafts_selected_question_index",
        ),
    )
    op.create_index("ix_tutor_problem_drafts_tenant_id", "tutor_problem_drafts", ["tenant_id"])
    op.create_index("ix_tutor_problem_drafts_user_id", "tutor_problem_drafts", ["user_id"])
    op.create_index("ix_tutor_problem_drafts_resource_id", "tutor_problem_drafts", ["resource_id"])
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_problem_drafts TO edu_app")
    op.execute("ALTER TABLE tutor_problem_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tutor_problem_drafts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tutor_problem_drafts "
        f"USING (tenant_id = {_GUC_TENANT}) WITH CHECK (tenant_id = {_GUC_TENANT})"
    )
    op.execute(
        "CREATE POLICY internal_cleanup_manage ON tutor_problem_drafts "
        f"USING ({_GUC_CLEANUP}) WITH CHECK ({_GUC_CLEANUP})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS internal_cleanup_manage ON tutor_problem_drafts")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tutor_problem_drafts")
    op.drop_table("tutor_problem_drafts")
