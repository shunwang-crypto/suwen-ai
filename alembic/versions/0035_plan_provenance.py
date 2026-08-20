"""Persist plan mode and grounding provenance.

Revision ID: 0035
Revises: 0034

``kb_id`` is nullable and uses ``ON DELETE SET NULL``.  It therefore cannot
distinguish a goal-only plan from a formerly course-bound plan whose source
course was deleted.  Persist the creation mode and current grounding status so
the API and UI can keep that boundary explicit.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("plan_mode", sa.String(16), nullable=True),
    )
    op.add_column(
        "plans",
        sa.Column("grounding_status", sa.String(20), nullable=True),
    )
    # Existing plans with a surviving KB were course-bound.  Existing plans
    # without one remain goal-only; web-only provenance was not persisted.
    op.execute(
        "UPDATE plans SET plan_mode = 'course_bound', grounding_status = 'kb_grounded' "
        "WHERE kb_id IS NOT NULL"
    )
    op.execute(
        "UPDATE plans SET plan_mode = 'goal_only', grounding_status = 'unreferenced' "
        "WHERE kb_id IS NULL"
    )
    op.alter_column("plans", "plan_mode", nullable=False)
    op.alter_column("plans", "grounding_status", nullable=False)
    op.create_check_constraint(
        "ck_plans_mode", "plans", "plan_mode IN ('course_bound', 'goal_only')"
    )
    op.create_check_constraint(
        "ck_plans_grounding_status",
        "plans",
        "grounding_status IN ('kb_grounded', 'web_assisted', 'unreferenced')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_plans_grounding_status", "plans", type_="check")
    op.drop_constraint("ck_plans_mode", "plans", type_="check")
    op.drop_column("plans", "grounding_status")
    op.drop_column("plans", "plan_mode")
