"""Persist FSRS ReviewLog snapshots and unified learning evidence.

Revision ID: 0022
Revises: 0021 (alembic/versions/0021_pending_cleanup_observability.py)

This revision intentionally has no data backfill.  Existing review rows do not
contain an exact historical ReviewLog, so inferring one from today's card would
create fabricated learning evidence.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_TABLES = ("fsrs_review_events", "learning_events")


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
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()

    op.create_table(
        "fsrs_review_events",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("tenant_id", uuid_type, nullable=False),
        sa.Column(
            "user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "error_item_id",
            uuid_type,
            sa.ForeignKey("error_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "card_before_json",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "card_after_json",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "review_log_json",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "params_version",
            sa.String(32),
            nullable=False,
            server_default="fsrs-6-default",
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_fsrs_review_events_user_idempotency",
        ),
        sa.CheckConstraint("rating BETWEEN 1 AND 4", name="ck_fsrs_review_events_rating"),
        sa.CheckConstraint(
            "review_duration_ms IS NULL OR review_duration_ms >= 0",
            name="ck_fsrs_review_events_duration",
        ),
    )
    op.create_index(
        "ix_fsrs_review_events_tenant_id",
        "fsrs_review_events",
        ["tenant_id"],
    )
    op.create_index(
        "ix_fsrs_review_events_error_reviewed",
        "fsrs_review_events",
        ["error_item_id", "reviewed_at"],
    )
    op.create_index(
        "ix_fsrs_review_events_user_reviewed",
        "fsrs_review_events",
        ["user_id", "reviewed_at"],
    )

    op.create_table(
        "learning_events",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("tenant_id", uuid_type, nullable=False),
        sa.Column(
            "user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column(
            "question_id",
            uuid_type,
            sa.ForeignKey("questions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("item_key", sa.String(128), nullable=True),
        sa.Column(
            "kc_ids_json",
            json_type,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("difficulty_label", sa.String(16), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "user_id",
            "source_type",
            "source_id",
            "idempotency_key",
            name="uq_learning_events_source_idempotency",
        ),
        sa.CheckConstraint(
            "source_type IN ('quiz_attempt', 'review', 'assessment')",
            name="ck_learning_events_source_type",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_learning_events_score",
        ),
        sa.CheckConstraint(
            "rating IS NULL OR rating BETWEEN 1 AND 4",
            name="ck_learning_events_rating",
        ),
    )
    op.create_index("ix_learning_events_tenant_id", "learning_events", ["tenant_id"])
    op.create_index(
        "ix_learning_events_user_occurred",
        "learning_events",
        ["user_id", "occurred_at"],
    )
    op.create_index(
        "ix_learning_events_source",
        "learning_events",
        ["source_type", "source_id"],
    )

    for table in _TABLES:
        _rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("ix_learning_events_source", table_name="learning_events")
    op.drop_index("ix_learning_events_user_occurred", table_name="learning_events")
    op.drop_index("ix_learning_events_tenant_id", table_name="learning_events")
    op.drop_index(
        "ix_fsrs_review_events_user_reviewed",
        table_name="fsrs_review_events",
    )
    op.drop_index(
        "ix_fsrs_review_events_error_reviewed",
        table_name="fsrs_review_events",
    )
    op.drop_index("ix_fsrs_review_events_tenant_id", table_name="fsrs_review_events")
    op.drop_table("learning_events")
    op.drop_table("fsrs_review_events")
