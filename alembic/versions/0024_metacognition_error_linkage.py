"""Link quiz errors to their immutable Attempt/confidence snapshot.

Revision ID: 0024
Revises: 0023

No historical confidence backfill is attempted: old ErrorItem rows do not have
an unambiguous quiz Attempt and must remain NULL rather than receiving a
fabricated calibration value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("error_items")}

    additions = (
        ("source_attempt_id", sa.Column("source_attempt_id", uuid_type, nullable=True)),
        ("confidence_at_error", sa.Column("confidence_at_error", sa.SmallInteger(), nullable=True)),
        (
            "confidence_probability_at_error",
            sa.Column("confidence_probability_at_error", sa.Float(), nullable=True),
        ),
        (
            "confidence_scale_version",
            sa.Column("confidence_scale_version", sa.String(32), nullable=True),
        ),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("error_items", column)

    inspector = sa.inspect(bind)
    foreign_keys = inspector.get_foreign_keys("error_items")
    has_attempt_fk = any(
        fk.get("referred_table") == "attempts"
        and fk.get("constrained_columns") == ["source_attempt_id"]
        for fk in foreign_keys
    )
    if not has_attempt_fk:
        op.create_foreign_key(
            "fk_error_items_source_attempt_id",
            "error_items",
            "attempts",
            ["source_attempt_id"],
            ["id"],
            ondelete="SET NULL",
        )

    unique_constraints = inspector.get_unique_constraints("error_items")
    has_source_attempt_unique = any(
        constraint.get("column_names") == ["source_attempt_id"]
        for constraint in unique_constraints
    )
    if not has_source_attempt_unique:
        op.create_unique_constraint(
            "uq_error_items_source_attempt_id",
            "error_items",
            ["source_attempt_id"],
        )

    indexes = inspector.get_indexes("error_items")
    has_source_attempt_index = any(
        index.get("column_names") == ["source_attempt_id"]
        for index in indexes
    )
    if not has_source_attempt_index:
        op.create_index(
            "ix_error_items_source_attempt_id",
            "error_items",
            ["source_attempt_id"],
        )

    checks = inspector.get_check_constraints("error_items")
    check_names = {check.get("name") for check in checks}
    if "ck_error_items_confidence_at_error" not in check_names:
        op.create_check_constraint(
            "ck_error_items_confidence_at_error",
            "error_items",
            "confidence_at_error IS NULL OR confidence_at_error BETWEEN 1 AND 5",
        )
    if "ck_error_items_confidence_probability_at_error" not in check_names:
        op.create_check_constraint(
            "ck_error_items_confidence_probability_at_error",
            "error_items",
            "confidence_probability_at_error IS NULL OR "
            "confidence_probability_at_error BETWEEN 0 AND 1",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    checks = {check.get("name") for check in inspector.get_check_constraints("error_items")}
    for name in (
        "ck_error_items_confidence_probability_at_error",
        "ck_error_items_confidence_at_error",
    ):
        if name in checks:
            op.drop_constraint(name, "error_items", type_="check")

    indexes = {index.get("name") for index in inspector.get_indexes("error_items")}
    if "ix_error_items_source_attempt_id" in indexes:
        op.drop_index("ix_error_items_source_attempt_id", table_name="error_items")

    inspector = sa.inspect(bind)
    unique_constraints = inspector.get_unique_constraints("error_items")
    if any(
        constraint.get("name") == "uq_error_items_source_attempt_id"
        for constraint in unique_constraints
    ):
        op.drop_constraint("uq_error_items_source_attempt_id", "error_items", type_="unique")

    inspector = sa.inspect(bind)
    for foreign_key in inspector.get_foreign_keys("error_items"):
        if (
            foreign_key.get("referred_table") == "attempts"
            and foreign_key.get("constrained_columns") == ["source_attempt_id"]
            and foreign_key.get("name")
        ):
            op.drop_constraint(foreign_key["name"], "error_items", type_="foreignkey")

    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("error_items")}
    for name in (
        "confidence_scale_version",
        "confidence_probability_at_error",
        "confidence_at_error",
        "source_attempt_id",
    ):
        if name in columns:
            op.drop_column("error_items", name)
