"""Store native DSH sessions and task lifecycle instead of host execution phases."""

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE harness_sessions ADD COLUMN status text NOT NULL DEFAULT 'idle'")
    op.execute("ALTER TABLE harness_sessions ADD COLUMN native_session_id text NOT NULL DEFAULT ''")
    op.execute(
        "ALTER TABLE harness_sessions ADD COLUMN native_archive jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "UPDATE harness_sessions SET status=CASE WHEN pending IS NOT NULL THEN 'awaiting_confirmation' WHEN next_phase IS NULL THEN 'completed' ELSE 'idle' END"
    )
    op.execute("ALTER TABLE harness_sessions DROP COLUMN next_phase")
    op.execute(
        "ALTER TABLE harness_sessions ADD CONSTRAINT ck_harness_status CHECK (status IN ('idle','running','awaiting_confirmation','completed','failed','cancelled'))"
    )
    op.execute("ALTER TABLE harness_sessions DROP CONSTRAINT ck_harness_kind")
    op.execute(
        "ALTER TABLE harness_sessions ADD CONSTRAINT ck_harness_kind CHECK (kind IN ('qa','quiz','plan','tutor','chat'))"
    )


def downgrade():
    op.execute("DELETE FROM harness_sessions WHERE kind='chat'")
    op.execute("ALTER TABLE harness_sessions DROP CONSTRAINT ck_harness_kind")
    op.execute(
        "ALTER TABLE harness_sessions ADD CONSTRAINT ck_harness_kind CHECK (kind IN ('qa','quiz','plan','tutor'))"
    )
    op.execute("ALTER TABLE harness_sessions ADD COLUMN next_phase text")
    op.execute("ALTER TABLE harness_sessions DROP CONSTRAINT ck_harness_status")
    for column in ("native_archive", "native_session_id", "status"):
        op.execute(f"ALTER TABLE harness_sessions DROP COLUMN {column}")
