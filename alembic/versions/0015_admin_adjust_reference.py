"""make administrator wallet adjustments idempotent per user and reference

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_wallet_ledger_admin_adjust_ref",
        "wallet_ledger",
        ["user_id", "ref"],
        unique=True,
        postgresql_where=sa.text("kind = 'admin_adjust' AND ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_wallet_ledger_admin_adjust_ref", table_name="wallet_ledger")
