"""initial schema: all core tables + row-level security

Revision ID: 0001
Revises:
Create Date: 2026-07-26

初始迁移采用 Base.metadata.create_all 以保证与 ORM 模型零漂移
(决策记录: docs/decisions.md #D3)。后续迁移一律 autogenerate 增量。
RLS: 租户表 ENABLE+FORCE, 策略基于事务级 GUC app.tenant_id
(应用层通过 set_config('app.tenant_id', :tid, true) 注入, 见 apps/api db.py)。
"""
from collections.abc import Sequence

from eduagents_api.models import TENANT_TABLES, Base

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    # 应用角色: 非超级用户 (超级用户绕过 RLS, decisions.md D9)。
    # 开发默认密码 'edu', 生产部署后必须 ALTER ROLE edu_app PASSWORD ...
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'edu_app') THEN
                CREATE ROLE edu_app LOGIN PASSWORD 'edu' NOSUPERUSER NOBYPASSRLS;
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO edu_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO edu_app"
    )
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO edu_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO edu_app"
    )

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE: 表 owner (应用连接用户) 也受策略约束, 否则 RLS 形同虚设
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # NULLIF: 事务级 set_config 结束后 GUC 在连接上残留为空串 (非 NULL),
        # 直接 ::uuid 会炸; NULLIF 归一为 NULL -> 比较恒 false -> 默认拒绝
        guc = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
        if table == "usage_log":
            # 运营面板 (F10) 需要管理员跨租户聚合, 仅此表放行 is_admin
            op.execute(
                "CREATE POLICY tenant_isolation ON usage_log "
                f"USING (tenant_id = {guc} "
                "OR current_setting('app.is_admin', true) = 'true') "
                f"WITH CHECK (tenant_id = {guc})"
            )
        else:
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING (tenant_id = {guc}) "
                f"WITH CHECK (tenant_id = {guc})"
            )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    Base.metadata.drop_all(bind=op.get_bind())
