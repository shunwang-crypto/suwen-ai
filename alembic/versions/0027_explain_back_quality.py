"""Add source linkage and shadow rubric data to ExplainBack.

Revision ID: 0027
Revises: 0025

The Learning Models workstream is offline/shadow-only and has no schema
revision, so revision 0026 is intentionally unused.  This revision follows
0025 directly and does not depend on a placeholder migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    op.add_column("explain_backs", sa.Column("source_attempt_id", uuid_type, nullable=True))
    op.add_column("explain_backs", sa.Column("question_id", uuid_type, nullable=True))
    op.add_column(
        "explain_backs",
        sa.Column(
            "rubric_version",
            sa.String(32),
            nullable=False,
            server_default="explain-back-rubric-v1",
        ),
    )
    op.add_column(
        "explain_backs",
        sa.Column(
            "score_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "explain_backs",
        sa.Column("quality_shadow", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_foreign_key(
        "fk_explain_backs_source_attempt_id",
        "explain_backs",
        "attempts",
        ["source_attempt_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_explain_backs_question_id",
        "explain_backs",
        "questions",
        ["question_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_explain_backs_source_attempt_id",
        "explain_backs",
        ["source_attempt_id"],
    )
    op.create_index("ix_explain_backs_question_id", "explain_backs", ["question_id"])
    op.create_check_constraint(
        "ck_explain_backs_score_json_object",
        "explain_backs",
        "jsonb_typeof(score_json) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_explain_backs_score_json_object", "explain_backs", type_="check"
    )
    op.drop_index("ix_explain_backs_question_id", table_name="explain_backs")
    op.drop_index("ix_explain_backs_source_attempt_id", table_name="explain_backs")
    op.drop_constraint("fk_explain_backs_question_id", "explain_backs", type_="foreignkey")
    op.drop_constraint(
        "fk_explain_backs_source_attempt_id", "explain_backs", type_="foreignkey"
    )
    op.drop_column("explain_backs", "quality_shadow")
    op.drop_column("explain_backs", "score_json")
    op.drop_column("explain_backs", "rubric_version")
    op.drop_column("explain_backs", "question_id")
    op.drop_column("explain_backs", "source_attempt_id")
