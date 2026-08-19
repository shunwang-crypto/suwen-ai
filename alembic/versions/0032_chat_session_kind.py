"""chat_sessions.kind: 会话类型列（UI 重构阶段 6 方案 A）

普通对话 kind='chat'（默认），辅导会话 kind='tutor'。辅导轮次按 thread_local_id
落一行 ChatSession（id=thread_local_id，与 LangGraph checkpoint 同键），使
/api/chat/sessions 能混排两类会话。存量 tutor thread 不回填——旧现场继续经
suwen:active-tutor-session 恢复。
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="chat"),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "kind")
