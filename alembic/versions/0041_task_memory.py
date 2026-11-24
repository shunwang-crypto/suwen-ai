"""Task memory provenance, user revisions and durable extraction watermarks."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade():
    # 0005 used the current ORM subset; a fresh replay may already have these
    # columns. Upgrades from an existing 0040 database still need the ALTERs.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("learner_insights")}
    additions = [
        sa.Column("source_message_id", UUID(as_uuid=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("user_corrected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    ]
    for column in additions:
        if column.name not in columns:
            op.add_column("learner_insights", column)
    op.execute("UPDATE learner_insights SET observed_at=created_at WHERE observed_at IS NULL")
    op.execute("UPDATE learner_insights SET expires_at=observed_at + interval '90 days' WHERE expires_at IS NULL AND NOT user_corrected")
    op.create_check_constraint("ck_insight_status", "learner_insights", "status IN ('active','obsolete')")
    op.create_table(
        "learner_memory_policies",
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extraction_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_fingerprints", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("ALTER TABLE learner_memory_policies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE learner_memory_policies FORCE ROW LEVEL SECURITY")
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid"
    op.execute(f"CREATE POLICY tenant_isolation ON learner_memory_policies USING ({predicate}) WITH CHECK ({predicate})")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON learner_memory_policies TO edu_app")


def downgrade():
    op.drop_table("learner_memory_policies")
    op.drop_constraint("ck_insight_status", "learner_insights")
    for column in ("status", "user_corrected", "revision", "updated_at", "expires_at", "observed_at", "source_message_id"):
        op.drop_column("learner_insights", column)
