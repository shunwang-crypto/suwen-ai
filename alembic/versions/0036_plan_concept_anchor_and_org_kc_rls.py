"""Persist plan concept anchors and extend org read access to KC graph rows.

Revision ID: 0036
Revises: 0035

``plan_concepts.kc_id`` is a nullable, auditable link created only after a
provisional plan concept uniquely matches a current semantic KC.  It is not a
mastery/evidence relationship.

Institution members already have read access to their shared KB documents and
chunks.  The KC graph is part of the same course content, so its two tables
must use the same org-member read boundary while writes remain owner-only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_ORG_ARR = "string_to_array(NULLIF(current_setting('app.org_ids', true), ''), ',')"
_ORG_READ_TABLES = ("knowledge_components", "kc_edges")


def upgrade() -> None:
    op.add_column(
        "plan_concepts",
        sa.Column(
            "kc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_plan_concepts_kc_id", "plan_concepts", ["kc_id"])
    for table in _ORG_READ_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_read ON {table} FOR SELECT "
            f"USING (tenant_id = {_GUC_TENANT} OR tenant_id::text = ANY ({_ORG_ARR}))"
        )
        op.execute(
            f"CREATE POLICY tenant_write ON {table} FOR ALL "
            f"USING (tenant_id = {_GUC_TENANT}) "
            f"WITH CHECK (tenant_id = {_GUC_TENANT})"
        )


def downgrade() -> None:
    # ``promoted`` existed before this revision.  Without a per-row history
    # marker, a downgrade cannot distinguish historical rows from rows anchored
    # after 0036, so changing status here would silently destroy plan history.
    for table in _ORG_READ_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_read ON {table}")
        op.execute(f"DROP POLICY IF EXISTS tenant_write ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {_GUC_TENANT}) "
            f"WITH CHECK (tenant_id = {_GUC_TENANT})"
        )
    op.drop_index("ix_plan_concepts_kc_id", table_name="plan_concepts")
    op.drop_column("plan_concepts", "kc_id")
