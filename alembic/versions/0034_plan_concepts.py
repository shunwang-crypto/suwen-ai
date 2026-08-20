"""Plan-scoped provisional concepts that must never touch mastery.

Revision ID: 0034
Revises: 0033

无资料（或资料尚未 enrich）的计划里，任务上的 ``kc_names`` 匹配不到任何正式
``knowledge_components``，于是掌握度、依赖视图、薄弱定向对这些概念全部空转。

这张表把"计划里的临时概念"和"正式知识点"**分层**，而不是把模型猜的概念直接写成
正式 KC：

- ``plan_concepts``：计划目标里的临时概念，只用于计划展示与后续归并候选；
- ``knowledge_components``：由资料抽取或人工确认后的正式 KC，才是 BKT / 证据 /
  推荐 / 成长报告的外键。

为什么必须分层（评审意见，且与既有纪律一致）：计划里的自然语言概念不一定是稳定
知识点、可能与之后上传资料抽出的 KC 重复、其先修关系是模型猜测。混进正式 KC 会
污染掌握度与报告，也会绕过 ``knowledge_components`` 的 ``(kb_id, stable_key)``
幂等边界（临时概念根本没有 kb）。

因此本表**刻意不设** kc_id 外键、不参与 mastery/evidence 任何路径；升级为正式 KC
只能经资料归并（KcNormalizer 的别名/余弦归并）或用户确认，那是后续波次的事。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _rls() -> None:
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON plan_concepts TO edu_app")
    op.execute("ALTER TABLE plan_concepts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE plan_concepts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON plan_concepts "
        f"USING (tenant_id = {_GUC_TENANT}) "
        f"WITH CHECK (tenant_id = {_GUC_TENANT})"
    )


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "plan_concepts",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("tenant_id", uuid_type, nullable=False),
        sa.Column(
            "plan_id",
            uuid_type,
            sa.ForeignKey("plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(255), nullable=False),
        # 展示序 = 概念在计划里首次出现的顺序。不能靠 created_at: 同一事务批量
        # 插入时它们完全相同, 排序会退化成随机 UUID 序, 教学顺序就丢了。
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        # 归一化名只用于同一计划内去重与将来的归并候选匹配，不是稳定 KC 标识
        sa.Column("normalized_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="provisional"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("plan_id", "normalized_name", name="uq_plan_concepts_plan_name"),
        sa.CheckConstraint(
            "status IN ('provisional', 'promoted', 'dismissed')",
            name="ck_plan_concepts_status",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_plan_concepts_ordinal"),
    )
    op.create_index("ix_plan_concepts_tenant_id", "plan_concepts", ["tenant_id"])
    _rls()


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON plan_concepts")
    op.drop_index("ix_plan_concepts_tenant_id", table_name="plan_concepts")
    op.drop_table("plan_concepts")
