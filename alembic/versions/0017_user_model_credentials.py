"""encrypted user model credentials and auditable credential-source usage

Revision ID: 0017
Revises: 0016

The table is intentionally additive.  No existing migration is edited and no
existing usage row is backfilled: historical calls remain platform calls.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_GUC_ADMIN = "current_setting('app.is_admin', true) = 'true'"


def upgrade() -> None:
    bind = op.get_bind()
    usage_columns = {column["name"] for column in sa.inspect(bind).get_columns("usage_log")}
    if "credential_source" not in usage_columns:
        op.add_column(
            "usage_log",
            sa.Column("credential_source", sa.String(16), nullable=False, server_default="platform"),
        )
    if "provider" not in usage_columns:
        op.add_column(
            "usage_log",
            sa.Column("provider", sa.String(32), nullable=False, server_default="platform"),
        )
    if "platform_billed_cost_cny" not in usage_columns:
        op.add_column(
            "usage_log",
            sa.Column("platform_billed_cost_cny", sa.Float(), nullable=False, server_default="0"),
        )
    if not sa.inspect(bind).has_table("user_model_credentials"):
        op.create_table(
            "user_model_credentials",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("provider", sa.String(32), nullable=False, server_default="deepseek"),
            sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=False),
            sa.Column("encryption_nonce", sa.LargeBinary(), nullable=False),
            sa.Column("encryption_key_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("key_last_four", sa.String(4), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("fallback_to_platform", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("normal_model", sa.String(128), nullable=False),
            sa.Column("reasoning_model", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_validation_status", sa.String(32), nullable=True),
            sa.Column("last_error_code", sa.String(64), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_model_credentials_user", ondelete="CASCADE"),
            sa.UniqueConstraint("user_id", "provider", name="uq_user_model_credentials_provider"),
            sa.CheckConstraint("provider IN ('deepseek')", name="ck_user_model_credentials_provider"),
            sa.CheckConstraint("length(key_last_four) = 4", name="ck_user_model_credentials_last_four"),
        )
        op.create_index("ix_user_model_credentials_user_id", "user_model_credentials", ["user_id"])
        op.create_index("ix_user_model_credentials_tenant_id", "user_model_credentials", ["tenant_id"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON user_model_credentials TO edu_app")
    op.execute("ALTER TABLE user_model_credentials ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_model_credentials FORCE ROW LEVEL SECURITY")
    policy_exists = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pg_policies "
            "WHERE schemaname = current_schema() AND tablename = 'user_model_credentials' "
            "AND policyname = 'credential_owner_all')"
        )
    ).scalar()
    if not policy_exists:
        op.execute(
            "CREATE POLICY credential_owner_all ON user_model_credentials "
            f"USING (user_id = {_GUC_TENANT}) WITH CHECK (user_id = {_GUC_TENANT})"
        )
    admin_policy_exists = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pg_policies "
            "WHERE schemaname = current_schema() AND tablename = 'user_model_credentials' "
            "AND policyname = 'credential_admin_read')"
        )
    ).scalar()
    if not admin_policy_exists:
        op.execute(
            "CREATE POLICY credential_admin_read ON user_model_credentials FOR SELECT "
            f"USING ({_GUC_ADMIN})"
        )
    # Admin dashboards only need aggregate counts.  The route selects counts
    # rather than row contents, and ordinary users retain the original
    # tenant_isolation policy.  These additive SELECT policies avoid N+1
    # cross-tenant scans while keeping writes tenant-bound.
    for table in ("knowledge_bases", "error_items", "plans"):
        policy_name = f"admin_count_read_{table}"
        exists = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_policies "
                "WHERE schemaname = current_schema() AND tablename = :table "
                "AND policyname = :policy)"
            ),
            {"table": table, "policy": policy_name},
        ).scalar()
        if not exists:
            op.execute(
                f"CREATE POLICY {policy_name} ON {table} FOR SELECT USING ({_GUC_ADMIN})"
            )


def downgrade() -> None:
    for table in ("knowledge_bases", "error_items", "plans"):
        op.execute(f"DROP POLICY IF EXISTS admin_count_read_{table} ON {table}")
    op.execute("DROP POLICY IF EXISTS credential_admin_read ON user_model_credentials")
    op.execute("DROP POLICY IF EXISTS credential_owner_all ON user_model_credentials")
    op.drop_index("ix_user_model_credentials_tenant_id", table_name="user_model_credentials")
    op.drop_index("ix_user_model_credentials_user_id", table_name="user_model_credentials")
    op.drop_table("user_model_credentials")
    usage_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("usage_log")}
    if "platform_billed_cost_cny" in usage_columns:
        op.drop_column("usage_log", "platform_billed_cost_cny")
    if "provider" in usage_columns:
        op.drop_column("usage_log", "provider")
    if "credential_source" in usage_columns:
        op.drop_column("usage_log", "credential_source")
