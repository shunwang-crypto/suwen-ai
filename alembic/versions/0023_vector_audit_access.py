"""Allow the protected vector-audit path to enumerate KB owners.

The exact audit needs the owner of every knowledge base before it can open a
tenant-scoped session for the corresponding chunks and documents.  Existing
knowledge-base policies intentionally expose only the current owner and org
members, so this narrowly named SELECT policy is enabled for the same
transaction-local ``app.is_admin`` convention used by protected admin
endpoints.  It grants no write access and does not widen child-table reads.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_policy p
                JOIN pg_class c ON c.oid = p.polrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE p.polname = 'vector_audit_admin_select'
                  AND c.relname = 'knowledge_bases'
                  AND n.nspname = current_schema()
            ) THEN
                CREATE POLICY vector_audit_admin_select ON knowledge_bases
                FOR SELECT
                USING (current_setting('app.is_admin', true) = 'true');
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS vector_audit_admin_select ON knowledge_bases")
