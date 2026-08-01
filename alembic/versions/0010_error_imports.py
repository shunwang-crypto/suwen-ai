"""persistent wrong-question imports and source provenance

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column("source_type", sa.String(length=16), nullable=False, server_default="generated"),
    )
    op.add_column(
        "questions",
        sa.Column("source_meta_json", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "error_items",
        sa.Column(
            "verification_status", sa.String(length=16), nullable=False, server_default="verified"
        ),
    )
    op.add_column(
        "error_items",
        sa.Column("source_import_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_questions_source_type",
        "questions",
        "source_type IN ('generated', 'manual', 'image', 'document')",
    )
    op.create_check_constraint(
        "ck_error_verification_status",
        "error_items",
        "verification_status IN ('pending', 'verified')",
    )

    op.create_table(
        "error_imports",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(length=768), nullable=True),
        sa.Column(
            "draft_json", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("recognition_warning", sa.Text(), nullable=True),
        sa.Column(
            "confirmed_error_item_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_error_imports_user", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "source_type IN ('manual', 'image', 'document')", name="ck_error_import_source_type"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed')", name="ck_error_import_status"
        ),
    )
    op.create_index("ix_error_imports_tenant_id", "error_imports", ["tenant_id"])
    op.create_index("ix_error_imports_user_id", "error_imports", ["user_id"])
    op.create_index("ix_error_items_source_import_id", "error_items", ["source_import_id"])
    op.create_foreign_key(
        "fk_error_items_source_import",
        "error_items",
        "error_imports",
        ["source_import_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON error_imports TO edu_app")
    op.execute("ALTER TABLE error_imports ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE error_imports FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON error_imports "
        f"USING (tenant_id = {_GUC_TENANT}) "
        f"WITH CHECK (tenant_id = {_GUC_TENANT})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON error_imports")
    op.drop_constraint("fk_error_items_source_import", "error_items", type_="foreignkey")
    op.drop_index("ix_error_items_source_import_id", table_name="error_items")
    op.drop_table("error_imports")
    op.drop_constraint("ck_error_verification_status", "error_items", type_="check")
    op.drop_constraint("ck_questions_source_type", "questions", type_="check")
    op.drop_column("error_items", "source_import_id")
    op.drop_column("error_items", "verification_status")
    op.drop_column("questions", "source_meta_json")
    op.drop_column("questions", "source_type")
