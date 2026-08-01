"""require verified email ownership for public registration

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-01

The migration intentionally does not modify 0010, 0011, or 0012. Existing
accounts are treated as historically verified so a deployment does not
suddenly lock them out. New accounts receive ``email_verified_at`` only after
the registration code is consumed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0001 creates its frozen table subset from current ORM metadata. On a
    # fresh database that can already include this nullable column, while an
    # existing 0012 database does not. Keep 0013 valid for both paths.
    bind = op.get_bind()
    has_column = any(
        column["name"] == "email_verified_at"
        for column in sa.inspect(bind).get_columns("users")
    )
    if not has_column:
        op.add_column(
            "users",
            sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Never silently merge or delete accounts. This check runs before the
    # UPDATE, and PostgreSQL aborts the migration with the duplicate count.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE duplicate_count integer;
            BEGIN
                SELECT count(*) INTO duplicate_count
                FROM (
                    SELECT lower(btrim(email))
                    FROM users
                    GROUP BY lower(btrim(email))
                    HAVING count(*) > 1
                ) duplicates;
                IF duplicate_count > 0 THEN
                    RAISE EXCEPTION
                        '0013 cannot normalize users.email: % case-insensitive duplicate group(s)',
                        duplicate_count;
                END IF;
            END $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE users
            SET email = lower(btrim(email)),
                email_verified_at = COALESCE(email_verified_at, created_at)
            """
        )
    )
    has_index = any(
        index["name"] == "uq_users_email_lower"
        for index in sa.inspect(bind).get_indexes("users")
    )
    if not has_index:
        op.execute(sa.text("CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email))"))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS uq_users_email_lower"))
    op.drop_column("users", "email_verified_at")
