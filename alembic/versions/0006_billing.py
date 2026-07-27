"""billing: wallet_accounts / wallet_ledger / payment_orders / password_reset_tokens + RLS

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-27

预付额度计费地基 (扣费 = 模型真实成本 × billing_markup, 服务层见 apps/api wallet.py):

1) 建表走 ORM 子集 create_all (同 0003/0005, 零漂移); 四表 FK(users.id) CASCADE
   已在 ORM 声明 (0001 建表清单已冻结, 新表不受"重复建约束"限制, 同 0005 头注释)。
   金额列一律 NUMERIC(12,4) — 应用层配套 Decimal, 禁 float 参与账目。

2) 幂等唯一部分索引 (计费正确性的 DB 层兜底):
   - (user_id) WHERE kind='signup_grant': 注册赠送只发一次 (注册重试/重放安全);
   - (user_id, ref) WHERE kind='consume': 同 request_id 的扣费重试不双扣
     (ON CONFLICT DO NOTHING, WalletService.consume 靠 rowcount 判重)。

3) CHECK 白名单 (同 0004/0005 写法): wallet_ledger.kind 五枚举;
   payment_orders.status 四态状态机。

4) RLS: 四表 ENABLE + FORCE。谓词沿用 0001 口径 (NULLIF 空串兜底) 再加一个
   服务层标记 GUC — app.billing_service='true' (WalletService 在事务内
   set_config(is_local=true) 注入, 与 app.is_admin 的 D7 惯例同形):
   - wallet_accounts / wallet_ledger / payment_orders: 本人可读 (user_id =
     app.tenant_id GUC), 写仅服务层 (INSERT/UPDATE 只有 billing_service 策略);
   - wallet_ledger 是 append-only: 不建 UPDATE/DELETE 策略, 并 REVOKE
     UPDATE/DELETE (0001 default privileges 会把全 DML 授给 edu_app, 必须显式收回);
   - password_reset_tokens: 仅服务层可读写 (无任何用户直读路径, C 路密码找回用)。

5) PIPL: 注销 = DELETE users 行 -> 四表随 FK 级联清空 (FK 级联是引用完整性内部
   操作, 不受 RLS 约束, 无 DELETE 策略也能级联)。

从零重放安全性: 0001 的 _TABLES_AT_0001 与 TENANT_TABLES 均不含这四张表 (models.py
注释), 0001 的策略循环不会撞"表不存在"。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from eduagents_api.models import Base

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 0001..0005 同一口径的 RLS 谓词片段 (NULLIF 空串兜底)
_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
# 服务层标记 (wallet.py 在事务内 set_config 注入; 缺省 NULL != 'true' -> 默认拒绝)
_GUC_SERVICE = "current_setting('app.billing_service', true) = 'true'"

_NEW_TABLES = (
    "wallet_accounts",
    "wallet_ledger",
    "payment_orders",
    "password_reset_tokens",
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1) 建表: ORM 子集 create_all (checkfirst 幂等, 含 FK 与 token_hash 唯一约束)
    Base.metadata.create_all(
        bind=bind, tables=[Base.metadata.tables[n] for n in _NEW_TABLES]
    )

    # 2) 幂等唯一部分索引 + 流水读路径索引
    op.create_index(
        "uq_wallet_ledger_signup_once",
        "wallet_ledger",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'signup_grant'"),
    )
    op.create_index(
        "uq_wallet_ledger_consume_ref",
        "wallet_ledger",
        ["user_id", "ref"],
        unique=True,
        postgresql_where=sa.text("kind = 'consume'"),
    )
    # 本人最近 N 条 (前缀覆盖 user_id 等值, 不另建单列索引, 同 0005)
    op.create_index(
        "ix_wallet_ledger_user_created",
        "wallet_ledger",
        ["user_id", sa.text("created_at DESC")],
    )

    # 3) 枚举白名单 CHECK
    op.create_check_constraint(
        "ck_wallet_ledger_kind",
        "wallet_ledger",
        "kind IN ('signup_grant', 'consume', 'recharge', 'refund', 'admin_adjust')",
    )
    op.create_check_constraint(
        "ck_payment_orders_status",
        "payment_orders",
        "status IN ('pending', 'paid', 'failed', 'expired')",
    )

    # 4) GRANT (显式, 同 0002/0004/0005) + append-only 收权 + RLS
    op.execute("GRANT SELECT, INSERT, UPDATE ON wallet_accounts TO edu_app")
    op.execute("REVOKE DELETE ON wallet_accounts FROM edu_app")
    op.execute("GRANT SELECT, INSERT ON wallet_ledger TO edu_app")
    # 0001 的 default privileges 已把全 DML 授给 edu_app, append-only 必须显式收回
    op.execute("REVOKE UPDATE, DELETE ON wallet_ledger FROM edu_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON payment_orders TO edu_app")
    op.execute("REVOKE DELETE ON payment_orders FROM edu_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON password_reset_tokens TO edu_app"
    )

    for table in _NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE: 表 owner 也受策略约束 (0001 同款)
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # wallet_accounts: 本人自读; 写仅服务层
    op.execute(
        "CREATE POLICY wallet_owner_read ON wallet_accounts FOR SELECT "
        f"USING (user_id = {_GUC_TENANT} OR {_GUC_SERVICE})"
    )
    op.execute(
        "CREATE POLICY wallet_service_insert ON wallet_accounts FOR INSERT "
        f"WITH CHECK ({_GUC_SERVICE})"
    )
    op.execute(
        "CREATE POLICY wallet_service_update ON wallet_accounts FOR UPDATE "
        f"USING ({_GUC_SERVICE}) WITH CHECK ({_GUC_SERVICE})"
    )

    # wallet_ledger: 本人自读; 仅服务层 INSERT; 无 UPDATE/DELETE 策略 = append-only
    op.execute(
        "CREATE POLICY ledger_owner_read ON wallet_ledger FOR SELECT "
        f"USING (user_id = {_GUC_TENANT} OR {_GUC_SERVICE})"
    )
    op.execute(
        "CREATE POLICY ledger_service_insert ON wallet_ledger FOR INSERT "
        f"WITH CHECK ({_GUC_SERVICE})"
    )

    # payment_orders: 本人自读; 建单/状态过渡仅服务层
    op.execute(
        "CREATE POLICY orders_owner_read ON payment_orders FOR SELECT "
        f"USING (user_id = {_GUC_TENANT} OR {_GUC_SERVICE})"
    )
    op.execute(
        "CREATE POLICY orders_service_insert ON payment_orders FOR INSERT "
        f"WITH CHECK ({_GUC_SERVICE})"
    )
    op.execute(
        "CREATE POLICY orders_service_update ON payment_orders FOR UPDATE "
        f"USING ({_GUC_SERVICE}) WITH CHECK ({_GUC_SERVICE})"
    )

    # password_reset_tokens: 仅服务层 (用户侧零直读)
    op.execute(
        "CREATE POLICY prt_service_all ON password_reset_tokens FOR ALL "
        f"USING ({_GUC_SERVICE}) WITH CHECK ({_GUC_SERVICE})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS prt_service_all ON password_reset_tokens")
    op.execute("DROP POLICY IF EXISTS orders_service_update ON payment_orders")
    op.execute("DROP POLICY IF EXISTS orders_service_insert ON payment_orders")
    op.execute("DROP POLICY IF EXISTS orders_owner_read ON payment_orders")
    op.execute("DROP POLICY IF EXISTS ledger_service_insert ON wallet_ledger")
    op.execute("DROP POLICY IF EXISTS ledger_owner_read ON wallet_ledger")
    op.execute("DROP POLICY IF EXISTS wallet_service_update ON wallet_accounts")
    op.execute("DROP POLICY IF EXISTS wallet_service_insert ON wallet_accounts")
    op.execute("DROP POLICY IF EXISTS wallet_owner_read ON wallet_accounts")
    op.drop_index("ix_wallet_ledger_user_created", table_name="wallet_ledger")
    op.drop_index("uq_wallet_ledger_consume_ref", table_name="wallet_ledger")
    op.drop_index("uq_wallet_ledger_signup_once", table_name="wallet_ledger")
    op.execute("DROP TABLE IF EXISTS password_reset_tokens")
    op.execute("DROP TABLE IF EXISTS payment_orders")
    op.execute("DROP TABLE IF EXISTS wallet_ledger")
    op.execute("DROP TABLE IF EXISTS wallet_accounts")
