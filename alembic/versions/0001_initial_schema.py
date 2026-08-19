"""initial schema: all core tables + row-level security

Revision ID: 0001
Revises:
Create Date: 2026-07-26

初始迁移采用 Base.metadata.create_all 以保证与 ORM 模型零漂移
后续迁移一律使用增量 revision。
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

# create_all 会跟着 ORM 漂移: 后续迁移新增的表若也被 0001 建出来, 从零重放时
# 0002 的 create_table 会撞 "already exists"。故把 0001 的建表范围冻结为
# M0 契约当时的 21 张表 (历史事实, 只读不改), 之后一律增量迁移 (D3)。
_TABLES_AT_0001: tuple[str, ...] = (
    "organizations",
    "users",
    "refresh_tokens",
    "knowledge_bases",
    "documents",
    "chunks",
    "knowledge_components",
    "kc_edges",
    "mastery_records",
    "mastery_log",
    "quizzes",
    "questions",
    "attempts",
    "error_items",
    "plans",
    "plan_adjustments",
    "chat_sessions",
    "chat_messages",
    "usage_log",
    "eval_runs",
)


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[n] for n in _TABLES_AT_0001])
    # 0001 之后新增的列在此显式回退为"当时不存在", 保证从零重放时 0002 的
    # add_column 不会撞 "already exists" (create_all 用的是当前 ORM 定义)。
    op.execute("ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS owner_type")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS uploaded_by")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS display_name")
    # These columns were introduced by later migrations but the frozen 0001
    # table list is built from the current ORM metadata.  Drop them here so
    # 0010/0011 can replay their historical ADD COLUMN operations on an empty
    # database without changing those already-released migrations.
    op.execute("ALTER TABLE questions DROP COLUMN IF EXISTS source_type")
    op.execute("ALTER TABLE questions DROP COLUMN IF EXISTS source_meta_json")
    op.execute("ALTER TABLE error_items DROP COLUMN IF EXISTS verification_status")
    op.execute("ALTER TABLE error_items DROP COLUMN IF EXISTS source_import_id")
    op.execute("ALTER TABLE error_items DROP COLUMN IF EXISTS resource_question_id")
    op.execute("ALTER TABLE error_items DROP COLUMN IF EXISTS assessment_answer_id")
    op.execute("ALTER TABLE error_items DROP COLUMN IF EXISTS original_response")
    # 同理: chat_sessions.kind 由 0032 引入, 从零重放时必须先回退为"当时不存在",
    # 否则 0032 的 add_column 撞 already exists (整套集成测试与新库首次部署都会挂)。
    op.execute("ALTER TABLE chat_sessions DROP COLUMN IF EXISTS kind")

    # 应用角色: 非超级用户 (超级用户绕过 RLS, decisions.md D9)。
    # 本地开发默认密码为 'edu'；应用连接与 migration 连接仍使用不同角色。
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
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO edu_app")
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
