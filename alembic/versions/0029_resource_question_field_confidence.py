"""Add bounded per-field confidence to resource questions."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("resource_questions")}
    if "field_confidence_json" not in columns:
        op.add_column(
            "resource_questions",
            sa.Column(
                "field_confidence_json",
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("resource_questions")}
    if "field_confidence_json" in columns:
        op.drop_column("resource_questions", "field_confidence_json")
