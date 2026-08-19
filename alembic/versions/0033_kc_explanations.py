"""Per-KC structured explanations bound to the learner's own material.

Revision ID: 0033
Revises: 0032

讲解是"按知识点的结构化教学文档"，不是又一套自由生成的教材：每个 block 必须
引用本人资料里的真实 chunk，且整份讲解按 (kb, kc, 掌握档) 缓存。缓存失效由
``generator_version``（教学契约变更）与 ``context_hash``（资料/上下文变更）共同
决定 —— 版本或上下文一变，读接口如实标记 stale 并给重新生成入口，绝不静默把
旧内容当新内容用。

生成失败不写行：失败时保留已有内容并由接口报错，界面显示"暂不可用"，
不允许用一次失败把学生正在读的讲解清空。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _rls() -> None:
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON kc_explanations TO edu_app")
    op.execute("ALTER TABLE kc_explanations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kc_explanations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON kc_explanations "
        f"USING (tenant_id = {_GUC_TENANT}) "
        f"WITH CHECK (tenant_id = {_GUC_TENANT})"
    )


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()
    op.create_table(
        "kc_explanations",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("tenant_id", uuid_type, nullable=False),
        sa.Column(
            "kb_id",
            uuid_type,
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kc_id",
            uuid_type,
            sa.ForeignKey("knowledge_components.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 掌握档是讲解的适配维度 (讲法随"会不会"变), 因此进唯一键。
        sa.Column("band", sa.String(16), nullable=False),
        sa.Column("generator_version", sa.String(64), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column(
            "blocks_json", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "source_refs_json", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "kb_id", "kc_id", "band", name="uq_kc_explanations_scope"
        ),
        sa.CheckConstraint(
            "band IN ('unobserved', 'weak', 'reinforce', 'near', 'mastered')",
            name="ck_kc_explanations_band",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(blocks_json) = 'array' AND jsonb_array_length(blocks_json) > 0",
            name="ck_kc_explanations_blocks_non_empty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_refs_json) = 'array'",
            name="ck_kc_explanations_source_refs_array",
        ),
    )
    op.create_index("ix_kc_explanations_tenant_id", "kc_explanations", ["tenant_id"])
    op.create_index("ix_kc_explanations_kb_kc", "kc_explanations", ["kb_id", "kc_id"])
    _rls()


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON kc_explanations")
    op.drop_index("ix_kc_explanations_kb_kc", table_name="kc_explanations")
    op.drop_index("ix_kc_explanations_tenant_id", table_name="kc_explanations")
    op.drop_table("kc_explanations")
