"""Add provenance and stable identity fields to knowledge components.

The initial migration builds the frozen core tables from the current ORM
metadata.  Consequently, a from-zero upgrade can arrive here with these
columns, the check constraint, the self-FK, and indexes already present, while
an existing 0027 database has none of them.  This revision deliberately
handles both shapes without changing any previously released migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "knowledge_components"
_SOURCE_KIND = "source_kind"
_STABLE_KEY = "stable_key"
_SUPERSEDED_BY_ID = "superseded_by_id"
_SOURCE_CHECK = "ck_knowledge_components_source_kind"
_SUPERSEDED_FK = "fk_knowledge_components_superseded_by_id"
_STABLE_INDEX = "uq_knowledge_components_kb_stable_key"
_SUPERSEDED_INDEX = "ix_knowledge_components_superseded_by_id"


def _has_column(name: str) -> bool:
    return name in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _has_index(name: str) -> bool:
    return name in {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def _has_check_constraint(name: str) -> bool:
    return name in {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(_TABLE)
    }


def _superseded_fk() -> dict | None:
    """Return a superseded-by FK, including an ORM-created unnamed one."""

    for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(_TABLE):
        if (
            foreign_key.get("constrained_columns") == [_SUPERSEDED_BY_ID]
            and foreign_key.get("referred_table") == _TABLE
            and foreign_key.get("referred_columns") == ["id"]
        ):
            return foreign_key
    return None


def upgrade() -> None:
    source_kind_type = sa.String(length=32)
    stable_key_type = sa.String(length=512)
    uuid_type = sa.UUID(as_uuid=True)

    if not _has_column(_SOURCE_KIND):
        op.add_column(
            _TABLE,
            sa.Column(
                _SOURCE_KIND,
                source_kind_type,
                nullable=True,
                server_default=sa.text("'enrichment'"),
            ),
        )
    # The update is intentionally explicit even when PostgreSQL filled an
    # added column from its default: it also repairs a nullable pre-created
    # column without touching any other KC data.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET {_SOURCE_KIND} = 'enrichment' "
            f"WHERE {_SOURCE_KIND} IS NULL"
        )
    )
    op.alter_column(
        _TABLE,
        _SOURCE_KIND,
        existing_type=source_kind_type,
        nullable=False,
        server_default=sa.text("'enrichment'"),
    )

    if not _has_column(_STABLE_KEY):
        op.add_column(_TABLE, sa.Column(_STABLE_KEY, stable_key_type, nullable=True))

    if not _has_column(_SUPERSEDED_BY_ID):
        op.add_column(_TABLE, sa.Column(_SUPERSEDED_BY_ID, uuid_type, nullable=True))

    foreign_key = _superseded_fk()
    if foreign_key is None:
        op.create_foreign_key(
            _SUPERSEDED_FK,
            _TABLE,
            _TABLE,
            [_SUPERSEDED_BY_ID],
            ["id"],
            ondelete="SET NULL",
        )
    elif (foreign_key.get("options") or {}).get("ondelete", "").upper() != "SET NULL":
        # A pre-created FK with another delete action would still be able to
        # remove/deny historical rows, so normalize that constraint in place.
        op.drop_constraint(foreign_key["name"], _TABLE, type_="foreignkey")
        op.create_foreign_key(
            _SUPERSEDED_FK,
            _TABLE,
            _TABLE,
            [_SUPERSEDED_BY_ID],
            ["id"],
            ondelete="SET NULL",
        )

    if not _has_check_constraint(_SOURCE_CHECK):
        op.create_check_constraint(
            _SOURCE_CHECK,
            _TABLE,
            "source_kind IN ('enrichment', 'chapter_fallback')",
        )

    if not _has_index(_SUPERSEDED_INDEX):
        op.create_index(_SUPERSEDED_INDEX, _TABLE, [_SUPERSEDED_BY_ID])

    if not _has_index(_STABLE_INDEX):
        op.create_index(
            _STABLE_INDEX,
            _TABLE,
            ["kb_id", _STABLE_KEY],
            unique=True,
            postgresql_where=sa.text(f"{_STABLE_KEY} IS NOT NULL"),
        )


def downgrade() -> None:
    if _has_index(_STABLE_INDEX):
        op.drop_index(_STABLE_INDEX, table_name=_TABLE)
    if _has_index(_SUPERSEDED_INDEX):
        op.drop_index(_SUPERSEDED_INDEX, table_name=_TABLE)

    foreign_key = _superseded_fk()
    if foreign_key is not None:
        op.drop_constraint(foreign_key["name"], _TABLE, type_="foreignkey")
    if _has_check_constraint(_SOURCE_CHECK):
        op.drop_constraint(_SOURCE_CHECK, _TABLE, type_="check")

    if _has_column(_SUPERSEDED_BY_ID):
        op.drop_column(_TABLE, _SUPERSEDED_BY_ID)
    if _has_column(_STABLE_KEY):
        op.drop_column(_TABLE, _STABLE_KEY)
    if _has_column(_SOURCE_KIND):
        op.drop_column(_TABLE, _SOURCE_KIND)
