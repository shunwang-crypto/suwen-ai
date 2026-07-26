"""learner memory: learner_insights + user_preferences tables + RLS (Wave-P2, P2-1)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-27

学习者长期记忆与偏好地基 (docs/research/brief-5-个性化.md §4 Wave-P2):

1) learner_insights (G10): Reflection 学生原话证据 + tutor misconceptions 落库,
   补上"他在什么语境下困惑" — F8-A2"新会话主动关联历史卡点"的地基。
   - 建表走 ORM 子集 create_all (同 0003 pending_cleanup, 零漂移);
     user_id FK(users.id) CASCADE / kc_id FK(knowledge_components.id) SET NULL
     已在 ORM 声明 (新表不受 0003 头注释"0001 重复建约束"限制: 0001 建表
     范围已冻结, 本表只由本迁移建出)。
   - 索引 (user_id, created_at DESC): 读路径 = 本人最近 N 条; 前缀即覆盖
     user_id 等值查询, 不再建单列索引。
   - CHECK: source/kind 枚举白名单 (同 0004 feedback 的 category/status 写法)。

2) user_preferences (G8): 讲解风格/详略/兴趣/每日时长/目标, 一人一行
   (user_id 主键 + FK CASCADE)。explanation_style 是内容组织维度枚举
   (rigorous|colloquial|analogy), 属内容组织维度, 不含任何感官类型分类 (该类说法已被证伪, 全仓禁用)。

3) RLS: 两表 ENABLE + FORCE, 策略照抄 0001 的 tenant_isolation 形状
   (app.tenant_id GUC + NULLIF 空串兜底, 默认拒绝; 不放行 is_admin/org_ids —
   纯个人数据, 无任何跨租户读路径)。GRANT 显式补齐 (同 0002/0004:
   0001 的 default privileges 只覆盖迁移角色后建的表, 显式更稳)。

4) PIPL: 注销 = DELETE users 行 -> 两表随 FK 级联清空 (routers/account.py);
   导出纳入 worker/export.py 的表清单。

从零重放安全性: 0001 的建表清单已冻结 (_TABLES_AT_0001), 不含这两张表;
TENANT_TABLES 也不追加 (models.py 注释), 0001 的策略循环不会撞"表不存在"。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from eduagents_api.models import Base

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 0001/0002/0003/0004 同一口径的 RLS 谓词片段 (NULLIF 空串兜底)
_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"

_NEW_TABLES = ("learner_insights", "user_preferences")


def upgrade() -> None:
    bind = op.get_bind()

    # 1) 建表: ORM 子集 create_all (checkfirst 幂等, 含两表的 FK 约束)
    Base.metadata.create_all(
        bind=bind, tables=[Base.metadata.tables[n] for n in _NEW_TABLES]
    )

    # 2) learner_insights 读路径索引: 本人最近 N 条 (user_id 等值 + created_at 倒序)
    op.create_index(
        "ix_learner_insights_user_created",
        "learner_insights",
        ["user_id", sa.text("created_at DESC")],
    )

    # 3) 枚举白名单 CHECK (可空列: NULL 不触发 IN 判定, 天然放行)
    op.create_check_constraint(
        "ck_learner_insights_source",
        "learner_insights",
        "source IN ('chat_reflection', 'tutor_summary')",
    )
    op.create_check_constraint(
        "ck_learner_insights_kind",
        "learner_insights",
        "kind IN ('confused', 'understood', 'misconception')",
    )
    op.create_check_constraint(
        "ck_user_preferences_style",
        "user_preferences",
        "explanation_style IN ('rigorous', 'colloquial', 'analogy')",
    )
    op.create_check_constraint(
        "ck_user_preferences_verbosity",
        "user_preferences",
        "verbosity IN ('concise', 'detailed')",
    )

    # 4) GRANT + RLS (ENABLE+FORCE + tenant_isolation, 逐条对照 0001 既有表)
    for table in _NEW_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edu_app")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE: 表 owner (应用连接用户) 也受策略约束, 否则 RLS 形同虚设 (0001 同款)
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {_GUC_TENANT}) "
            f"WITH CHECK (tenant_id = {_GUC_TENANT})"
        )


def downgrade() -> None:
    for table in reversed(_NEW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("ix_learner_insights_user_created", table_name="learner_insights")
    op.execute("DROP TABLE IF EXISTS user_preferences")
    op.execute("DROP TABLE IF EXISTS learner_insights")
