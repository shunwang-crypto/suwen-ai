"""Add the missing resource-question lookup index.

Revision ID: 0037
Revises: 0036

``ErrorItem.resource_question_id`` is indexed in the ORM, but the 0012 rebuild
did not recreate that index on historical local databases.  The questions
foreign key and its index are deliberately out of scope: 0001 already creates
them from the current ORM, and this migration owns only the missing lookup
index.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_error_items_resource_question_id",
        "error_items",
        ["resource_question_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_error_items_resource_question_id",
        table_name="error_items",
    )
