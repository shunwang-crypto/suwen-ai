"""bounded page recognition metadata for knowledge-base documents

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("documents")}
    if "recognition_meta_json" not in columns:
        op.add_column(
            "documents",
            sa.Column(
                "recognition_meta_json",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
    else:
        # 0001 builds its frozen table list from current ORM metadata, so a
        # from-zero replay can see this future column before revision 0019.
        # Normalize the database default to match the real 0018 -> 0019 path.
        op.alter_column(
            "documents",
            "recognition_meta_json",
            existing_type=postgresql.JSONB(),
            type_=postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("documents")}
    if "recognition_meta_json" in columns:
        op.drop_column("documents", "recognition_meta_json")
