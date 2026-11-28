"""Durable education collaborators and their parent task identities."""

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE harness_sessions ADD COLUMN parent_thread_id text")
    op.execute("ALTER TABLE harness_sessions ADD COLUMN agent_name text NOT NULL DEFAULT ''")
    op.execute("CREATE INDEX ix_harness_sessions_parent_thread_id ON harness_sessions(parent_thread_id)")
    op.execute("ALTER TABLE harness_sessions DROP CONSTRAINT ck_harness_kind")
    op.execute("ALTER TABLE harness_sessions ADD CONSTRAINT ck_harness_kind CHECK (kind IN ('qa','quiz','plan','tutor','chat','researcher','planner','reviewer'))")


def downgrade():
    # Do not silently discard durable collaborator histories.
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM harness_sessions WHERE kind IN ('researcher','planner','reviewer')) THEN RAISE EXCEPTION 'remove collaborator histories before downgrade'; END IF; END $$")
    op.execute("ALTER TABLE harness_sessions DROP CONSTRAINT ck_harness_kind")
    op.execute("ALTER TABLE harness_sessions ADD CONSTRAINT ck_harness_kind CHECK (kind IN ('qa','quiz','plan','tutor','chat'))")
    op.execute("DROP INDEX ix_harness_sessions_parent_thread_id")
    op.execute("ALTER TABLE harness_sessions DROP COLUMN agent_name, DROP COLUMN parent_thread_id")
