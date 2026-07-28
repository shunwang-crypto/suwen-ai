"""learning report snapshots: non-blocking report page + persisted AI diagnosis

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from eduagents_api.models import Base

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "learning_report_snapshots"
_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[_TABLE]])
    op.create_index(
        "ix_learning_report_user_updated",
        _TABLE,
        ["user_id", sa.text("updated_at DESC")],
    )
    op.create_check_constraint(
        "ck_learning_report_status",
        _TABLE,
        "status IN ('pending', 'generating', 'completed', 'unavailable', 'failed')",
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO edu_app"
    )
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY learning_report_owner ON {_TABLE} FOR ALL "
        f"USING (user_id = {_GUC_TENANT}) "
        f"WITH CHECK (user_id = {_GUC_TENANT} AND tenant_id = {_GUC_TENANT})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS learning_report_owner ON {_TABLE}")
    op.drop_index("ix_learning_report_user_updated", table_name=_TABLE)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
