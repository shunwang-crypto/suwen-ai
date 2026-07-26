"""org shared knowledge base: orgs/org_members + kb.owner_type + RLS org read policies

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-27

Wave3 机构共享知识库 (docs/research/brief-2-企业KB架构.md, decisions.md D14):
- 新表 orgs / org_members (建表走 ORM metadata 子集, 与模型零漂移)
- knowledge_bases.owner_type 判别列 (default 'user', 存量行零回填);
  tenant_id 列名不改, 语义泛化为 owner_id (user_id 或 org_id)
- documents.uploaded_by 最小审计列 (存量行 NULL)
- RLS 三 GUC: app.tenant_id + app.org_ids (成员机构逗号表, 只授"读") + app.is_admin。
  kb/documents/chunks 策略拆分: 读 = owner 匹配 OR owner ∈ org_ids; 写 = 仅 owner 匹配
  (org 库写路径由 service 层校验 org admin 后以 tenant_session(org_id) 开事务,
  与 ARQ worker 摄取同构 — 成员自己的会话对 org 行写操作被 RLS 拦下)。
- orgs/org_members 启用 ENABLE+FORCE RLS; join-by-code 的越权查询复用
  app.is_admin 事务级 GUC 惯例 (同 ops.py D7)。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from eduagents_api.models import Base

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RLS 谓词片段 (与 0001 同一口径: NULLIF 兜 GUC 残留空串)
_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_ORG_ARR = "string_to_array(NULLIF(current_setting('app.org_ids', true), ''), ',')"
_IS_ADMIN = "current_setting('app.is_admin', true) = 'true'"

# 读策略扩展到 org 成员的表 (定案范围: 机构库及其下游两张内容表)
_ORG_READ_TABLES = ("knowledge_bases", "documents", "chunks")


def upgrade() -> None:
    bind = op.get_bind()

    # 1) 新表 (ORM 子集建表, 零漂移)
    Base.metadata.create_all(
        bind=bind,
        tables=[Base.metadata.tables["orgs"], Base.metadata.tables["org_members"]],
    )

    # 2) 存量表增列 (server_default 'user' -> 现有行零回填即语义正确)
    op.add_column(
        "knowledge_bases",
        sa.Column("owner_type", sa.String(8), nullable=False, server_default="user"),
    )
    op.create_check_constraint(
        "ck_kb_owner_type", "knowledge_bases", "owner_type IN ('user', 'org')"
    )
    op.add_column(
        "documents",
        sa.Column("uploaded_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )

    # 3) edu_app 授权新表 (0001 的 default privileges 只覆盖迁移角色后建对象, 显式补齐)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON orgs, org_members TO edu_app")

    # 4) kb/documents/chunks: FOR ALL 单策略拆为 读(含 org 成员) + 写(仅 owner)
    for table in _ORG_READ_TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_read ON {table} FOR SELECT "
            f"USING (tenant_id = {_GUC_TENANT} OR tenant_id::text = ANY ({_ORG_ARR}))"
        )
        op.execute(
            f"CREATE POLICY tenant_write ON {table} FOR ALL "
            f"USING (tenant_id = {_GUC_TENANT}) "
            f"WITH CHECK (tenant_id = {_GUC_TENANT})"
        )

    # 5) orgs / org_members 自身的 RLS
    for table in ("orgs", "org_members"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    # orgs: 成员可读自己所在机构; 创建者当事务可读回 (org_ids 缓存尚未含新 org);
    # join-by-code 查询在事务内提升 app.is_admin (惯例同 usage_log/D7, 不泄邀请码)
    op.execute(
        "CREATE POLICY orgs_select ON orgs FOR SELECT USING ("
        f"id::text = ANY ({_ORG_ARR}) OR created_by = {_GUC_TENANT} OR {_IS_ADMIN})"
    )
    op.execute(
        f"CREATE POLICY orgs_insert ON orgs FOR INSERT WITH CHECK (created_by = {_GUC_TENANT})"
    )
    # org_members: 看得到自己的成员关系 + 同机构名单; 只允许写入自己 (建org自任admin/凭码join);
    # 无 UPDATE/DELETE 策略 -> 默认拒绝 (成员移除/改角色是 P1)
    op.execute(
        "CREATE POLICY org_members_select ON org_members FOR SELECT USING ("
        f"user_id = {_GUC_TENANT} OR org_id::text = ANY ({_ORG_ARR}))"
    )
    op.execute(
        "CREATE POLICY org_members_insert ON org_members FOR INSERT "
        f"WITH CHECK (user_id = {_GUC_TENANT})"
    )


def downgrade() -> None:
    for table in ("orgs", "org_members"):
        op.execute(f"DROP POLICY IF EXISTS {table}_select ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_insert ON {table}")
    for table in _ORG_READ_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_read ON {table}")
        op.execute(f"DROP POLICY IF EXISTS tenant_write ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {_GUC_TENANT}) "
            f"WITH CHECK (tenant_id = {_GUC_TENANT})"
        )
    op.drop_column("documents", "uploaded_by")
    op.drop_constraint("ck_kb_owner_type", "knowledge_bases")
    op.drop_column("knowledge_bases", "owner_type")
    op.drop_table("org_members")
    op.drop_table("orgs")
