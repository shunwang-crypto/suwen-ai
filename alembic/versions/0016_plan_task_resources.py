"""persist executable plan task resources and optional knowledge-base binding

Revision ID: 0016
Revises: 0015

The migration is deliberately additive.  Existing plans keep their JSONB
structure and legacy resource_ref values; no attempt is made to infer a source
row from an old array index.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    uuid_type = postgresql.UUID(as_uuid=True)

    if "kb_id" not in {c["name"] for c in inspector.get_columns("plans")}:
        op.add_column("plans", sa.Column("kb_id", uuid_type, nullable=True))
    if not any(fk.get("name") == "fk_plans_kb_id" for fk in inspector.get_foreign_keys("plans")):
        op.create_foreign_key(
            "fk_plans_kb_id", "plans", "knowledge_bases", ["kb_id"], ["id"], ondelete="SET NULL"
        )
    if "ix_plans_kb_id" not in {index["name"] for index in inspector.get_indexes("plans")}:
        op.create_index("ix_plans_kb_id", "plans", ["kb_id"])
    if not inspector.has_table("plan_resources"):
        op.create_table(
            "plan_resources",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("tenant_id", uuid_type, nullable=False),
            sa.Column("plan_id", uuid_type, nullable=False),
            sa.Column("source_type", sa.String(16), nullable=False),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("kb_id", uuid_type, nullable=True),
            sa.Column("doc_id", uuid_type, nullable=True),
            sa.Column("chunk_id", uuid_type, nullable=True),
            sa.Column("url", sa.String(2048), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("source_type IN ('kb_chunk', 'web')", name="ck_plan_resources_source_type"),
            sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_plan_resources_plan", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], name="fk_plan_resources_kb", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["doc_id"], ["documents.id"], name="fk_plan_resources_doc", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], name="fk_plan_resources_chunk", ondelete="SET NULL"),
        )
        op.create_index("ix_plan_resources_plan_id", "plan_resources", ["plan_id"])
        op.create_index("ix_plan_resources_kb_id", "plan_resources", ["kb_id"])
        op.create_index("ix_plan_resources_doc_id", "plan_resources", ["doc_id"])
        op.create_index("ix_plan_resources_chunk_id", "plan_resources", ["chunk_id"])

    # Plan rows are already tenant scoped.  Keep the resource table under the
    # same RLS boundary; the application role never receives a broad bypass.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON plan_resources TO edu_app")
    op.execute("ALTER TABLE plan_resources ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE plan_resources FORCE ROW LEVEL SECURITY")
    policy_exists = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pg_policies "
            "WHERE schemaname = current_schema() AND tablename = 'plan_resources' "
            "AND policyname = 'tenant_isolation')"
        )
    ).scalar()
    if not policy_exists:
        op.execute(
            "CREATE POLICY tenant_isolation ON plan_resources "
            f"USING (tenant_id = {_GUC_TENANT}) WITH CHECK (tenant_id = {_GUC_TENANT})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("plan_resources"):
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON plan_resources")
        op.drop_index("ix_plan_resources_chunk_id", table_name="plan_resources")
        op.drop_index("ix_plan_resources_doc_id", table_name="plan_resources")
        op.drop_index("ix_plan_resources_kb_id", table_name="plan_resources")
        op.drop_index("ix_plan_resources_plan_id", table_name="plan_resources")
        op.drop_table("plan_resources")
    if any(fk.get("name") == "fk_plans_kb_id" for fk in sa.inspect(bind).get_foreign_keys("plans")):
        op.drop_constraint("fk_plans_kb_id", "plans", type_="foreignkey")
    if "ix_plans_kb_id" in {index["name"] for index in sa.inspect(bind).get_indexes("plans")}:
        op.drop_index("ix_plans_kb_id", table_name="plans")
    if "kb_id" in {c["name"] for c in sa.inspect(bind).get_columns("plans")}:
        op.drop_column("plans", "kb_id")
