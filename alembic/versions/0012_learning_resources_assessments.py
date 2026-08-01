"""unify question resources and real assessment records

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-01

This migration deliberately leaves 0010 and 0011 untouched.  It is safe to run
against an empty database, a database stopped at 0010, or one already at 0011.
The backfill is intentionally conservative: it creates a resource for every
legacy ErrorImport/hidden error-import quiz, detaches questions from the hidden
quiz, and keeps ErrorItem, Attempt, MasteryLog and FSRS rows intact.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUC_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _has_fk(bind, table: str, name: str) -> bool:
    return any(fk.get("name") == name for fk in sa.inspect(bind).get_foreign_keys(table))


def _has_index(bind, table: str, name: str) -> bool:
    return any(index.get("name") == name for index in sa.inspect(bind).get_indexes(table))


def _has_unique_constraint(bind, table: str, name: str) -> bool:
    return any(
        constraint.get("name") == name
        for constraint in sa.inspect(bind).get_unique_constraints(table)
    )


def _create_rls(table: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edu_app")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {_GUC_TENANT}) "
        f"WITH CHECK (tenant_id = {_GUC_TENANT})"
    )


def _create_table_if_missing(bind, name: str, *columns, **kwargs) -> None:
    if not _has_table(bind, name):
        op.create_table(name, *columns, **kwargs)


def upgrade() -> None:
    bind = op.get_bind()
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()

    # 0010/0011 may already have these columns.  Conditional additions also
    # make a manually resumed migration harmless.
    if not _has_column(bind, "questions", "quiz_id"):
        # This branch is only relevant to very old custom databases; the normal
        # 0001 schema always has quiz_id.
        op.add_column(
            "questions",
            sa.Column("quiz_id", uuid_type, nullable=True),
        )
    else:
        op.alter_column("questions", "quiz_id", existing_type=uuid_type, nullable=True)

    for table, column in (
        ("error_items", "resource_question_id"),
        ("error_items", "assessment_answer_id"),
        ("error_imports", "resource_id"),
    ):
        if not _has_column(bind, table, column):
            op.add_column(table, sa.Column(column, uuid_type, nullable=True))

    _create_table_if_missing(
        bind,
        "learning_resources",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("tenant_id", uuid_type, nullable=False),
        sa.Column("owner_type", sa.String(16), nullable=False, server_default="personal"),
        sa.Column("owner_id", uuid_type, nullable=False),
        sa.Column("resource_type", sa.String(24), nullable=False, server_default="other"),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("subject", sa.String(64), nullable=False, server_default="general"),
        sa.Column("grade", sa.String(32), nullable=True),
        sa.Column("term", sa.String(32), nullable=True),
        sa.Column("source_filename", sa.String(255), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(768), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("metadata_json", json_type, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="uploaded"),
        sa.Column("retrieval_policy", sa.String(16), nullable=False, server_default="questions_only"),
        sa.Column("linked_kb_id", uuid_type, nullable=True),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("owner_type IN ('personal', 'organization')", name="ck_learning_resource_owner_type"),
        sa.CheckConstraint(
            "resource_type IN ('textbook', 'lecture', 'notes', 'exam_paper', 'mock_paper', 'workbook', 'worksheet', 'homework', 'single_question', 'other')",
            name="ck_learning_resource_type",
        ),
        sa.CheckConstraint(
            "status IN ('uploaded', 'extracting', 'reviewing', 'ready', 'failed', 'archived')",
            name="ck_learning_resource_status",
        ),
        sa.CheckConstraint(
            "retrieval_policy IN ('none', 'reference', 'questions_only')",
            name="ck_learning_resource_retrieval_policy",
        ),
    )
    _create_table_if_missing(
        bind,
        "resource_questions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("tenant_id", uuid_type, nullable=False),
        sa.Column("resource_id", uuid_type, nullable=False),
        sa.Column("question_id", uuid_type, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("question_no", sa.String(64), nullable=False, server_default=""),
        sa.Column("section_path", sa.String(255), nullable=False, server_default=""),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("source_region_json", json_type, nullable=False, server_default="{}"),
        sa.Column("points", sa.Float(), nullable=True),
        sa.Column("extraction_confidence", sa.Float(), nullable=True),
        sa.Column("verification_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("resource_id", "question_id", name="uq_resource_questions_resource_question"),
        sa.UniqueConstraint("question_id", name="uq_resource_questions_question"),
        sa.ForeignKeyConstraint(["resource_id"], ["learning_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "verification_status IN ('pending', 'verified', 'rejected')",
            name="ck_resource_questions_verification_status",
        ),
    )
    _create_table_if_missing(
        bind,
        "assessment_attempts",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("tenant_id", uuid_type, nullable=False),
        sa.Column("resource_id", uuid_type, nullable=True),
        sa.Column("student_id", uuid_type, nullable=False),
        sa.Column("attempt_type", sa.String(24), nullable=False, server_default="practice"),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("total_score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("grader_id", uuid_type, nullable=True),
        sa.Column("metadata_json", json_type, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["resource_id"], ["learning_resources.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["grader_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "attempt_type IN ('formal_exam', 'mock_exam', 'homework', 'practice', 'imported_record')",
            name="ck_assessment_attempt_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'graded')",
            name="ck_assessment_attempt_status",
        ),
    )
    # A Question can be referenced by more than one legacy ErrorItem and may
    # also be reused by more than one resource.  The resource-local pair is
    # unique; a global question_id uniqueness rule is too strong and made the
    # historical backfill fail for legitimate shared questions.
    if _has_unique_constraint(bind, "resource_questions", "uq_resource_questions_question"):
        op.drop_constraint(
            "uq_resource_questions_question", "resource_questions", type_="unique"
        )
    _create_table_if_missing(
        bind,
        "assessment_answers",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("tenant_id", uuid_type, nullable=False),
        sa.Column("attempt_id", uuid_type, nullable=False),
        sa.Column("resource_question_id", uuid_type, nullable=False),
        sa.Column("response", sa.Text(), nullable=False, server_default=""),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("max_score", sa.Float(), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("grading_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("teacher_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["attempt_id"], ["assessment_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resource_question_id"], ["resource_questions.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "grading_status IN ('pending', 'graded', 'verified')",
            name="ck_assessment_answer_grading_status",
        ),
    )

    # Explicit foreign keys are added after the tables exist.  The conditional
    # checks keep a resumed migration from failing on already-added constraints.
    if not _has_fk(bind, "error_items", "fk_error_items_resource_question"):
        op.create_foreign_key(
            "fk_error_items_resource_question",
            "error_items",
            "resource_questions",
            ["resource_question_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _has_fk(bind, "error_items", "fk_error_items_assessment_answer"):
        op.create_foreign_key(
            "fk_error_items_assessment_answer",
            "error_items",
            "assessment_answers",
            ["assessment_answer_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _has_fk(bind, "error_imports", "fk_error_imports_resource"):
        op.create_foreign_key(
            "fk_error_imports_resource",
            "error_imports",
            "learning_resources",
            ["resource_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _has_fk(bind, "learning_resources", "fk_learning_resources_created_by"):
        op.create_foreign_key(
            "fk_learning_resources_created_by",
            "learning_resources",
            "users",
            ["created_by"],
            ["id"],
            ondelete="CASCADE",
        )
    if not _has_fk(bind, "learning_resources", "fk_learning_resources_owner"):
        op.create_foreign_key(
            "fk_learning_resources_owner",
            "learning_resources",
            "users",
            ["owner_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if not _has_fk(bind, "learning_resources", "fk_learning_resources_linked_kb"):
        op.create_foreign_key(
            "fk_learning_resources_linked_kb",
            "learning_resources",
            "knowledge_bases",
            ["linked_kb_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _has_fk(bind, "assessment_attempts", "fk_assessment_attempts_student"):
        op.create_foreign_key(
            "fk_assessment_attempts_student",
            "assessment_attempts",
            "users",
            ["student_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if not _has_fk(bind, "assessment_attempts", "fk_assessment_attempts_grader"):
        op.create_foreign_key(
            "fk_assessment_attempts_grader",
            "assessment_attempts",
            "users",
            ["grader_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _has_index(bind, "resource_questions", "ix_resource_questions_resource_id"):
        op.create_index("ix_resource_questions_resource_id", "resource_questions", ["resource_id"])
    if not _has_index(bind, "resource_questions", "ix_resource_questions_question_id"):
        op.create_index("ix_resource_questions_question_id", "resource_questions", ["question_id"])
    if not _has_index(bind, "assessment_attempts", "ix_assessment_attempts_resource_id"):
        op.create_index("ix_assessment_attempts_resource_id", "assessment_attempts", ["resource_id"])
    if not _has_index(bind, "assessment_attempts", "ix_assessment_attempts_student_id"):
        op.create_index("ix_assessment_attempts_student_id", "assessment_attempts", ["student_id"])
    if not _has_index(bind, "assessment_answers", "ix_assessment_answers_attempt_id"):
        op.create_index("ix_assessment_answers_attempt_id", "assessment_answers", ["attempt_id"])
    if not _has_index(bind, "assessment_answers", "ix_assessment_answers_resource_question_id"):
        op.create_index(
            "ix_assessment_answers_resource_question_id",
            "assessment_answers",
            ["resource_question_id"],
        )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_error_items_assessment_answer "
        "ON error_items (assessment_answer_id) WHERE assessment_answer_id IS NOT NULL"
    )
    if not _has_unique_constraint(bind, "assessment_answers", "uq_assessment_answers_attempt_question"):
        op.create_unique_constraint(
            "uq_assessment_answers_attempt_question",
            "assessment_answers",
            ["attempt_id", "resource_question_id"],
        )

    # Existing 0010/0011 hidden Quiz rows are converted before the rows are
    # deleted.  IDs are deterministic (legacy import/quiz ID) so the SQL is
    # repeatable and never creates duplicate resources.
    op.execute(
        sa.text(
            """
            INSERT INTO learning_resources
                (id, tenant_id, owner_type, owner_id, resource_type, title, subject,
                 source_filename, mime_type, size_bytes, storage_key, metadata_json,
                 status, retrieval_policy, created_by, created_at, updated_at)
            SELECT ei.id, ei.tenant_id, 'personal', ei.user_id, 'single_question',
                   COALESCE(NULLIF(ei.filename, ''), '错题导入'), 'general',
                   ei.filename, ei.mime_type, ei.size_bytes, ei.storage_key,
                   jsonb_build_object('legacy_error_import_id', ei.id::text),
                   CASE WHEN ei.status = 'confirmed' THEN 'ready' ELSE 'reviewing' END,
                   'questions_only', ei.user_id, ei.created_at, ei.updated_at
            FROM error_imports ei
            WHERE ei.resource_id IS NULL
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            "UPDATE error_imports SET resource_id = id WHERE resource_id IS NULL"
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO resource_questions
                (id, tenant_id, resource_id, question_id, ordinal, question_no,
                 section_path, page_no, source_region_json, extraction_confidence,
                 verification_status, created_at, updated_at)
            SELECT gen_random_uuid(), picked.tenant_id, picked.resource_id, picked.question_id, 0,
                   COALESCE(q.source_meta_json->>'question_no', ''), '',
                   CASE WHEN q.source_meta_json->>'page_no' ~ '^[0-9]+$'
                        THEN (q.source_meta_json->>'page_no')::integer END,
                   '{}', NULL,
                   picked.verification_status, picked.created_at, picked.updated_at
            FROM (
                SELECT DISTINCT ON (ei.resource_id, e.question_id)
                       e.tenant_id, ei.resource_id, e.question_id,
                       e.verification_status, e.created_at, e.updated_at
                FROM error_items e
                JOIN error_imports ei ON ei.id = e.source_import_id
                WHERE ei.resource_id IS NOT NULL
                -- Multiple historical ErrorItems can share a Question.  A
                -- pending row wins so migration never upgrades uncertainty.
                ORDER BY ei.resource_id, e.question_id,
                         CASE WHEN e.verification_status = 'pending' THEN 0 ELSE 1 END,
                         e.created_at ASC, e.id ASC
            ) picked
            JOIN questions q ON q.id = picked.question_id
            ON CONFLICT (resource_id, question_id) DO UPDATE
              SET verification_status = EXCLUDED.verification_status,
                  updated_at = EXCLUDED.updated_at
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE error_items e
            SET resource_question_id = rq.id
            FROM resource_questions rq
            JOIN error_imports ei ON ei.resource_id = rq.resource_id
            WHERE e.source_import_id = ei.id
              AND e.question_id = rq.question_id
              AND e.resource_question_id IS NULL
            """
        )
    )

    # Convert old hidden error-import quizzes that do not have an ErrorImport
    # row (for example data produced by an early local build).
    op.execute(
        sa.text(
            """
            INSERT INTO learning_resources
                (id, tenant_id, owner_type, owner_id, resource_type, title, subject,
                 metadata_json, status, retrieval_policy, created_by)
            SELECT qz.id, qz.tenant_id, 'personal', qz.user_id, 'single_question',
                   COALESCE(NULLIF(qz.title, ''), '历史错题导入'), 'general',
                   jsonb_build_object('legacy_hidden_quiz_id', qz.id::text),
                   'ready', 'questions_only', qz.user_id
            FROM quizzes qz
            WHERE qz.blueprint_json->>'kind' = 'error_import'
              AND NOT EXISTS (
                  SELECT 1
                  FROM questions linked_q
                  JOIN error_items linked_e ON linked_e.question_id = linked_q.id
                  WHERE linked_q.quiz_id = qz.id
                    AND linked_e.source_import_id IS NOT NULL
              )
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO resource_questions
                (id, tenant_id, resource_id, question_id, ordinal, question_no,
                 section_path, source_region_json, verification_status)
            SELECT gen_random_uuid(), q.tenant_id, q.quiz_id, q.id, 0, '', '', '{}',
                   COALESCE(e.verification_status, 'verified')
            FROM questions q
            JOIN quizzes qz ON qz.id = q.quiz_id
            LEFT JOIN error_items e ON e.question_id = q.id
            WHERE qz.blueprint_json->>'kind' = 'error_import'
              AND e.source_import_id IS NULL
            ON CONFLICT (resource_id, question_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE error_items e
            SET resource_question_id = rq.id
            FROM resource_questions rq
            WHERE rq.question_id = e.question_id
              AND e.resource_question_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE questions q
            SET quiz_id = NULL
            FROM quizzes qz
            WHERE q.quiz_id = qz.id
              AND qz.blueprint_json->>'kind' = 'error_import'
            """
        )
    )
    op.execute(
        sa.text("DELETE FROM quizzes WHERE blueprint_json->>'kind' = 'error_import'")
    )

    for table in (
        "learning_resources",
        "resource_questions",
        "assessment_attempts",
        "assessment_answers",
    ):
        # A partially applied migration may already have the policy; normal
        # Alembic execution reaches this once.  The DO block makes reruns safe.
        op.execute(
            sa.text(
                f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE schemaname = current_schema()
                          AND tablename = '{table}'
                          AND policyname = 'tenant_isolation'
                    ) THEN
                        CREATE POLICY tenant_isolation ON {table}
                        USING (tenant_id = {_GUC_TENANT})
                        WITH CHECK (tenant_id = {_GUC_TENANT});
                    END IF;
                END $$
                """
            )
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edu_app")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # Worker cleanup uses a transaction-scoped internal capability instead of
    # opening one tenant session per user.  It is not exposed through any API
    # dependency and is only set by the worker's plain internal session.
    for table in ("error_imports", "learning_resources", "resource_questions"):
        op.execute(
            sa.text(
                f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE schemaname = current_schema()
                          AND tablename = '{table}'
                          AND policyname = 'internal_cleanup'
                    ) THEN
                        CREATE POLICY internal_cleanup ON {table}
                        USING (current_setting('app.internal_cleanup', true) = 'true')
                        WITH CHECK (current_setting('app.internal_cleanup', true) = 'true');
                    END IF;
                END $$
                """
            )
        )


def downgrade() -> None:
    # Data backfill is deliberately not reversed: restoring hidden quizzes or
    # deleting resource/assessment history would be destructive.  The schema
    # can still be downgraded safely for an empty 0012 database.
    bind = op.get_bind()
    if _has_index(bind, "error_items", "uq_error_items_assessment_answer"):
        op.drop_index("uq_error_items_assessment_answer", table_name="error_items")
    for table, name in (
        ("error_items", "fk_error_items_assessment_answer"),
        ("error_items", "fk_error_items_resource_question"),
        ("error_imports", "fk_error_imports_resource"),
        ("learning_resources", "fk_learning_resources_created_by"),
        ("learning_resources", "fk_learning_resources_owner"),
        ("learning_resources", "fk_learning_resources_linked_kb"),
        ("assessment_attempts", "fk_assessment_attempts_student"),
        ("assessment_attempts", "fk_assessment_attempts_grader"),
    ):
        if _has_fk(bind, table, name):
            op.drop_constraint(name, table, type_="foreignkey")
    for table in ("error_imports", "learning_resources", "resource_questions"):
        if _has_table(bind, table):
            op.execute(f"DROP POLICY IF EXISTS internal_cleanup ON {table}")
    if _has_table(bind, "assessment_answers"):
        if _has_unique_constraint(bind, "assessment_answers", "uq_assessment_answers_attempt_question"):
            op.drop_constraint(
                "uq_assessment_answers_attempt_question",
                "assessment_answers",
                type_="unique",
            )
        op.execute("DROP TABLE assessment_answers")
    if _has_table(bind, "assessment_attempts"):
        op.execute("DROP TABLE assessment_attempts")
    if _has_table(bind, "resource_questions"):
        op.execute("DROP TABLE resource_questions")
    if _has_table(bind, "learning_resources"):
        op.execute("DROP TABLE learning_resources")
    for table, column in (
        ("error_imports", "resource_id"),
        ("error_items", "assessment_answer_id"),
        ("error_items", "resource_question_id"),
    ):
        if _has_column(bind, table, column):
            op.drop_column(table, column)
    # Keep quiz_id nullable on downgrade; it is the only representation that
    # can preserve detached imported questions without recreating hidden Quiz rows.
