"""Tenant-scoped educational harness checkpoints; preserve legacy history.

Revision ID: 0039
Revises: 0038
"""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS harness_sessions (
            thread_id text PRIMARY KEY,
            tenant_id uuid NOT NULL,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind text NOT NULL CONSTRAINT ck_harness_kind CHECK (kind IN ('qa', 'quiz', 'plan', 'tutor')),
            state jsonb NOT NULL DEFAULT '{}',
            next_phase text,
            pending jsonb,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_harness_sessions_tenant ON harness_sessions (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_harness_sessions_updated ON harness_sessions (updated_at)"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON harness_sessions TO edu_app")
    op.execute("ALTER TABLE harness_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE harness_sessions FORCE ROW LEVEL SECURITY")
    tenant = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(
        "CREATE POLICY tenant_isolation ON harness_sessions "
        f"USING (tenant_id = {tenant}) WITH CHECK (tenant_id = {tenant})"
    )
    op.execute(
        "CREATE POLICY internal_cleanup ON harness_sessions "
        "USING (current_setting('app.internal_cleanup', true) = 'true') "
        "WITH CHECK (current_setting('app.internal_cleanup', true) = 'true')"
    )


def downgrade():
    op.drop_table("harness_sessions")
