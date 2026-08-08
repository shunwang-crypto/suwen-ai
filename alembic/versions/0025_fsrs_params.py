"""Versioned user-scoped FSRS-6 parameter artifacts.

Revision ID: 0025
Revises: 0024

The table is observation/shadow-only until the explicit personalization flag
and activation gate are enabled.  It stores exactly 21 FSRS-6 parameters and
keeps user data inside the normal tenant RLS/export/deletion/retention paths.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _rls() -> None:
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON fsrs_params TO edu_app")
    op.execute("ALTER TABLE fsrs_params ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE fsrs_params FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON fsrs_params "
        f"USING (tenant_id = {_GUC_TENANT}) "
        f"WITH CHECK (tenant_id = {_GUC_TENANT})"
    )


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()
    op.create_table(
        "fsrs_params",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("tenant_id", uuid_type, nullable=False),
        sa.Column(
            "user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("algorithm", sa.String(16), nullable=False, server_default="fsrs-6"),
        sa.Column("params_version", sa.String(64), nullable=False),
        sa.Column(
            "params_json",
            json_type,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("training_review_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "valid_cross_day_review_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "optimizer_version",
            sa.String(64),
            nullable=False,
            server_default="fsrs-6-fit-skeleton-v1",
        ),
        sa.Column(
            "fit_metrics_json",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="shadow"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "params_version", name="uq_fsrs_params_user_version"),
        sa.CheckConstraint("algorithm = 'fsrs-6'", name="ck_fsrs_params_algorithm"),
        sa.CheckConstraint(
            "jsonb_typeof(params_json) = 'array' AND jsonb_array_length(params_json) = 21",
            name="ck_fsrs_params_count",
        ),
        sa.CheckConstraint(
            "status IN ('shadow', 'active', 'retired', 'rejected')",
            name="ck_fsrs_params_status",
        ),
        sa.CheckConstraint(
            "training_review_count >= 0 AND valid_cross_day_review_count >= 0",
            name="ck_fsrs_params_counts",
        ),
    )
    op.create_index("ix_fsrs_params_tenant_id", "fsrs_params", ["tenant_id"])
    op.create_index("ix_fsrs_params_user_status", "fsrs_params", ["user_id", "status"])
    op.create_index(
        "uq_fsrs_params_active_user",
        "fsrs_params",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    _rls()


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON fsrs_params")
    op.drop_index("uq_fsrs_params_active_user", table_name="fsrs_params")
    op.drop_index("ix_fsrs_params_user_status", table_name="fsrs_params")
    op.drop_index("ix_fsrs_params_tenant_id", table_name="fsrs_params")
    op.drop_table("fsrs_params")
