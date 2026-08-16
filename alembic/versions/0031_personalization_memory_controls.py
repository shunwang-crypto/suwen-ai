"""Personalization memory controls (ChatGPT-style user control plane).

Three additions, all in the personalization read/write control plane:

1) user_preferences.personalization_enabled (M1): master switch for the
   profile injection layer. Off = build_profile_context returns empty and
   weak_point_kcs returns []. The column defaults to TRUE so existing users
   keep the current behaviour. Safety carve-out (documented in code): the
   switch never affects minor-guard, moderation or mastery computation.

2) learner_insights.suppressed (M1): soft-suppression ("不要再提及") for a
   single insight. Suppressed rows stay on disk (the stumble point may become
   relevant again) but are excluded from prompt-side reads (next_actions
   resume cards). Deletion remains available for a real erase.

3) learner_insights CHECK relaxation for M3: new kind 'preference_signal'
   (source 'ai_inferred') carries the rating-derived style suggestions shown
   on the settings memory page. Suggestion rows are user-confirmable only —
   accepting one still goes through PUT /api/preferences.

4) answer_ratings (M2/M3): per-answer 👍/👎 telemetry with the profile
   fingerprint captured at rating time (weak_points from the answer's profile
   trace + the style/verbosity the server filled in). One partial unique
   index keeps "one rating per chat message" so changing your mind updates
   the row instead of stacking rows. This is telemetry, not a feedback
   ticket: the 工单 flow (0004 feedback) stays untouched.

RLS follows the 0001/0020 tenant_isolation shape (app.tenant_id GUC,
NULLIF empty-string guard, default deny). PIPL: user_id FK CASCADE wipes the
table with the account.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"

_INSIGHT_SOURCE_CHECK = (
    "source IN ('chat_reflection', 'tutor_summary', 'ai_inferred')"
)
_INSIGHT_KIND_CHECK = (
    "kind IN ('confused', 'understood', 'misconception', 'preference_signal')"
)
_RATING_CHECK = "rating IN ('good', 'bad')"
_REASON_CHECK = (
    "reason IS NULL OR reason IN ('too_long', 'too_short', 'confusing', 'off_topic')"
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    up_cols = {c["name"] for c in inspector.get_columns("user_preferences")}
    if "personalization_enabled" not in up_cols:
        op.add_column(
            "user_preferences",
            sa.Column(
                "personalization_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )

    li_cols = {c["name"] for c in inspector.get_columns("learner_insights")}
    if "suppressed" not in li_cols:
        op.add_column(
            "learner_insights",
            sa.Column(
                "suppressed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    # CHECK 白名单放宽: 先删旧约束再建新版 (约束名与 0005 一致, 幂等靠 hasattr)
    existing_checks = {
        c["name"] for c in inspector.get_check_constraints("learner_insights")
    }
    if "ck_learner_insights_source" in existing_checks:
        op.drop_constraint("ck_learner_insights_source", "learner_insights")
    op.create_check_constraint(
        "ck_learner_insights_source", "learner_insights", _INSIGHT_SOURCE_CHECK
    )
    if "ck_learner_insights_kind" in existing_checks:
        op.drop_constraint("ck_learner_insights_kind", "learner_insights")
    # 'preference_signal' 17 字符超出 0005 的 kind VARCHAR(16), 先放宽列宽
    kind_col = next(c for c in inspector.get_columns("learner_insights") if c["name"] == "kind")
    if kind_col.get("type").length is not None and kind_col["type"].length < 24:
        op.alter_column(
            "learner_insights", "kind", existing_nullable=False, type_=sa.String(24)
        )
    op.create_check_constraint(
        "ck_learner_insights_kind", "learner_insights", _INSIGHT_KIND_CHECK
    )

    if "answer_ratings" not in inspector.get_table_names():
        op.create_table(
            "answer_ratings",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column(
                "user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # 关联被评价的 AI 消息 (历史消息无 trace 时为 NULL, 纯态度反馈)
            sa.Column("message_id", UUID(as_uuid=True), nullable=True),
            sa.Column("rating", sa.String(8), nullable=False),
            sa.Column("reason", sa.String(16), nullable=True),
            # 评分时的画像指纹: {style, verbosity, weak_points, profile_used}
            sa.Column(
                "fingerprint", JSONB(), nullable=False, server_default=sa.text("'{}'")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(_RATING_CHECK, name="ck_answer_ratings_rating"),
            sa.CheckConstraint(_REASON_CHECK, name="ck_answer_ratings_reason"),
        )
        # 一条消息一条评价: 改主意走 UPDATE, 不堆行 (message_id 为 NULL 的不计)
        op.create_index(
            "uq_answer_ratings_user_message",
            "answer_ratings",
            ["user_id", "message_id"],
            unique=True,
            postgresql_where=sa.text("message_id IS NOT NULL"),
        )
        # M3 统计读路径: 本人最近 N 条按时间倒序
        op.create_index(
            "ix_answer_ratings_user_created",
            "answer_ratings",
            ["user_id", sa.text("created_at DESC")],
        )
        op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON answer_ratings TO edu_app")
        op.execute("ALTER TABLE answer_ratings ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE answer_ratings FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY tenant_isolation ON answer_ratings "
            f"USING (tenant_id = {_GUC_TENANT}) "
            f"WITH CHECK (tenant_id = {_GUC_TENANT})"
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "answer_ratings" in inspector.get_table_names():
        op.execute("DROP TABLE answer_ratings")
    existing_checks = {
        c["name"] for c in inspector.get_check_constraints("learner_insights")
    }
    if "ck_learner_insights_kind" in existing_checks:
        op.drop_constraint("ck_learner_insights_kind", "learner_insights")
    op.alter_column(
        "learner_insights", "kind", existing_nullable=False, type_=sa.String(16)
    )
    op.create_check_constraint(
        "ck_learner_insights_kind",
        "learner_insights",
        "kind IN ('confused', 'understood', 'misconception')",
    )
    if "ck_learner_insights_source" in existing_checks:
        op.drop_constraint("ck_learner_insights_source", "learner_insights")
    op.create_check_constraint(
        "ck_learner_insights_source",
        "learner_insights",
        "source IN ('chat_reflection', 'tutor_summary')",
    )
    li_cols = {c["name"] for c in inspector.get_columns("learner_insights")}
    if "suppressed" in li_cols:
        op.drop_column("learner_insights", "suppressed")
    up_cols = {c["name"] for c in inspector.get_columns("user_preferences")}
    if "personalization_enabled" in up_cols:
        op.drop_column("user_preferences", "personalization_enabled")
