"""Add leases, retry scheduling and bounded failure details to cleanup outbox.

The original ``done`` flag remains the compatibility source of truth for old
rows and callers.  The worker uses the lease columns to claim rows atomically
and can reclaim a lease left behind by a crashed process.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = {
    "last_attempt_at": sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    "next_attempt_at": sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    "last_error_code": sa.Column("last_error_code", sa.String(64), nullable=True),
    "last_error_text": sa.Column("last_error_text", sa.String(512), nullable=True),
    "completed_at": sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    "locked_at": sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    "locked_by": sa.Column("locked_by", sa.String(128), nullable=True),
    "progress_json": sa.Column("progress_json", postgresql.JSONB(), nullable=True),
}

_INDEXES = {
    "ix_pending_cleanup_next_attempt_at": ("next_attempt_at",),
    "ix_pending_cleanup_last_error_code": ("last_error_code",),
    "ix_pending_cleanup_locked_at": ("locked_at",),
}


def _existing_columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("pending_cleanup")}


def _existing_indexes() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("pending_cleanup")}


def upgrade() -> None:
    for name, column in _COLUMNS.items():
        if name not in _existing_columns():
            op.add_column("pending_cleanup", column)

    for name, columns in _INDEXES.items():
        if name not in _existing_indexes():
            op.create_index(name, "pending_cleanup", list(columns))


def downgrade() -> None:
    for name in _INDEXES:
        if name in _existing_indexes():
            op.drop_index(name, table_name="pending_cleanup")

    for name in reversed(tuple(_COLUMNS)):
        if name in _existing_columns():
            op.drop_column("pending_cleanup", name)
