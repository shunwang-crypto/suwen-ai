"""add hashed platform registration invites

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "registration_invites",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), server_default="1", nullable=False),
        sa.Column("used_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.UUID(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'used', 'revoked')", name="ck_registration_invites_status"
        ),
        sa.CheckConstraint("max_uses > 0", name="ck_registration_invites_max_uses_positive"),
        sa.CheckConstraint(
            "used_count >= 0 AND used_count <= max_uses",
            name="ck_registration_invites_used_count_range",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash", name="uq_registration_invites_code_hash"),
    )
    op.create_index(
        "ix_registration_invites_status_expires_at",
        "registration_invites",
        ["status", "expires_at"],
    )
    op.create_index("ix_registration_invites_created_by", "registration_invites", ["created_by"])
    op.create_index("ix_registration_invites_used_by", "registration_invites", ["used_by"])
    op.execute("GRANT SELECT, INSERT, UPDATE ON registration_invites TO edu_app")


def downgrade() -> None:
    op.drop_index("ix_registration_invites_used_by", table_name="registration_invites")
    op.drop_index("ix_registration_invites_created_by", table_name="registration_invites")
    op.drop_index("ix_registration_invites_status_expires_at", table_name="registration_invites")
    op.drop_table("registration_invites")
