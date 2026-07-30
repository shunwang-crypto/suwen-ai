"""user display name: account identity independent from login email

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("display_name", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "display_name")
