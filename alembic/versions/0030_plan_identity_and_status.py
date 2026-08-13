"""Add explicit plan identity/lifecycle and evidence idempotency backstops."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("plans")}
    if "status" not in columns:
        op.add_column(
            "plans",
            sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        )

    # This project has no real learner data yet. Retired plan snapshots use a
    # different task identity/status contract and are deliberately discarded
    # instead of carrying a permanent compatibility reader into the runtime.
    op.execute("DELETE FROM plan_adjustments")
    op.execute("DELETE FROM plan_resources")
    op.execute("DELETE FROM plans")

    constraints = {item["name"] for item in inspector.get_check_constraints("plans")}
    if "ck_plans_status" not in constraints:
        op.create_check_constraint(
            "ck_plans_status",
            "plans",
            "status IN ('active', 'completed', 'archived')",
        )
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("plans")}
    if "uq_plans_one_active_per_user" not in indexes:
        op.create_index(
            "uq_plans_one_active_per_user",
            "plans",
            ["tenant_id", "user_id"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        )

    # Keep the earliest audit row for an already duplicated evidence identity,
    # then make concurrent retries impossible at the database boundary.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY tenant_id, user_id, kc_id, evidence_type, evidence_id
                ORDER BY at ASC, id ASC
            ) AS position
            FROM mastery_log
            WHERE evidence_id IS NOT NULL
        )
        DELETE FROM mastery_log
        USING ranked
        WHERE mastery_log.id = ranked.id AND ranked.position > 1
        """
    )
    mastery_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("mastery_log")}
    if "uq_mastery_log_evidence" not in mastery_indexes:
        op.create_index(
            "uq_mastery_log_evidence",
            "mastery_log",
            ["tenant_id", "user_id", "kc_id", "evidence_type", "evidence_id"],
            unique=True,
            postgresql_where=sa.text("evidence_id IS NOT NULL"),
        )

    # Report payload shape deliberately dropped its duplicated action cards.
    # Local snapshots are disposable and regenerated from source evidence.
    op.execute("DELETE FROM learning_report_snapshots")


def downgrade() -> None:
    bind = op.get_bind()
    mastery_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("mastery_log")}
    if "uq_mastery_log_evidence" in mastery_indexes:
        op.drop_index("uq_mastery_log_evidence", table_name="mastery_log")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("plans")}
    if "uq_plans_one_active_per_user" in indexes:
        op.drop_index("uq_plans_one_active_per_user", table_name="plans")
    constraints = {item["name"] for item in sa.inspect(bind).get_check_constraints("plans")}
    if "ck_plans_status" in constraints:
        op.drop_constraint("ck_plans_status", "plans", type_="check")
    if "status" in {column["name"] for column in sa.inspect(bind).get_columns("plans")}:
        op.drop_column("plans", "status")
