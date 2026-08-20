from pathlib import Path

import pytest


def test_runtime_purge_keeps_schema_metadata_out_of_delete_set():
    from scripts.purge_runtime_data import PRESERVED_TABLES, RUNTIME_TABLES

    assert "alembic_version" not in RUNTIME_TABLES
    assert "checkpoint_migrations" not in RUNTIME_TABLES
    assert set(PRESERVED_TABLES) == {"alembic_version", "checkpoint_migrations"}
    assert {"users", "refresh_tokens", "feedback", "wallet_ledger"} <= set(RUNTIME_TABLES)


def test_runtime_table_list_covers_current_orm_models():
    from eduagents_api.models import Base

    from scripts.purge_runtime_data import RUNTIME_TABLES

    assert set(Base.metadata.tables) <= set(RUNTIME_TABLES)


def test_local_object_audit_rejects_symlinks(tmp_path: Path):
    from scripts.purge_runtime_data import _local_objects

    root = tmp_path / "objects"
    root.mkdir()
    (root / "payload.txt").write_text("runtime", encoding="utf-8")
    (root / "link").symlink_to(root / "payload.txt")

    with pytest.raises(RuntimeError, match="symlink"):
        _local_objects(root)


def test_target_display_never_contains_url_credentials():
    from scripts.purge_runtime_data import _display_target

    assert _display_target("postgresql+asyncpg://secret:password@db.example:5432/eduagents", 5432) == "db.example:5432/eduagents"
