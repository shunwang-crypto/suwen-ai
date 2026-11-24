#!/usr/bin/env python3
"""Audit and permanently purge one dedicated EduAgents runtime instance.

The default is a read-only dry run.  Execution requires all of:

    APP_ENV=dev|test
    --execute --confirm-instance=<exact instance name>
    --expected-db-host/--expected-redis-host/--expected-qdrant-url

The production deployment can use the same tool after an operator has
explicitly identified the target instance.  It intentionally keeps Alembic
and LangGraph schema-migration metadata while removing runtime checkpoint
rows.  No seed or demo data is created.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from eduagents_core.config import Settings, get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

RUNTIME_TABLES = (
    "organizations",
    "orgs",
    "org_members",
    "users",
    "user_model_credentials",
    "registration_invites",
    "refresh_tokens",
    "password_reset_tokens",
    "knowledge_bases",
    "documents",
    "chunks",
    "knowledge_components",
    "kc_edges",
    "mastery_records",
    "mastery_log",
    "quizzes",
    "questions",
    "attempts",
    "assessment_submissions",
    "error_items",
    "fsrs_review_events",
    "fsrs_params",
    "error_imports",
    "kc_explanations",
    "learning_resources",
    "resource_questions",
    "tutor_problem_drafts",
    "assessment_attempts",
    "assessment_answers",
    "learning_events",
    "plans",
    "plan_resources",
    "plan_adjustments",
    "plan_concepts",
    "chat_sessions",
    "chat_messages",
    "chat_attachments",
    "usage_log",
    "feedback",
    "learner_insights",
    "learner_memory_policies",
    "answer_ratings",
    "metacognition_predictions",
    "explain_backs",
    "user_preferences",
    "learning_report_snapshots",
    "pending_cleanup",
    "eval_runs",
    "wallet_accounts",
    "wallet_ledger",
    "payment_orders",
    "harness_sessions",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)
PRESERVED_TABLES = ("alembic_version", "checkpoint_migrations")
APP_COLLECTION_SUFFIXES = ("answer_cache",)


@dataclass(frozen=True)
class StorageSnapshot:
    backend: str
    target: str
    objects: int
    bytes: int


def _arg_or_env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _target(url: str, default_port: int) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or default_port
    path = (parsed.path or "").lstrip("/")
    return host, port, path


def _display_target(url: str, default_port: int) -> str:
    host, port, path = _target(url, default_port)
    return f"{host}:{port}/{path or '?'}"


def _local_root(settings: Settings) -> Path:
    root = Path(settings.local_storage_dir or (Path.home() / ".local/share/eduagents/objects"))
    root = root.expanduser().resolve()
    forbidden = {Path("/"), Path.home().resolve(), Path.cwd().resolve()}
    if root in forbidden:
        raise RuntimeError(f"refusing unsafe object root: {root}")
    if not root.name == "objects":
        raise RuntimeError(f"object root must end in an explicit 'objects' directory: {root}")
    return root


def _local_objects(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"object root is not a real directory: {root}")
    objects: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"refusing symlink under object root: {path}")
        if path.is_file():
            objects.append(path)
    return objects


def _local_snapshot(settings: Settings) -> StorageSnapshot:
    root = _local_root(settings)
    objects = _local_objects(root)
    return StorageSnapshot("local", str(root), len(objects), sum(p.stat().st_size for p in objects))


def _minio_snapshot(settings: Settings) -> StorageSnapshot:
    from minio import Minio

    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_app_access_key,
        secret_key=settings.minio_app_secret_key,
        secure=settings.minio_secure,
    )
    if not client.bucket_exists(settings.minio_bucket):
        return StorageSnapshot(
            "minio",
            f"{settings.minio_endpoint}/{settings.minio_bucket}",
            0,
            0,
        )
    objects = list(client.list_objects(settings.minio_bucket, recursive=True))
    return StorageSnapshot(
        "minio",
        f"{settings.minio_endpoint}/{settings.minio_bucket}",
        len(objects),
        sum((obj.size or 0) for obj in objects),
    )


def storage_snapshot(settings: Settings) -> StorageSnapshot:
    if settings.storage_backend == "local":
        return _local_snapshot(settings)
    return _minio_snapshot(settings)


async def _database_snapshot(engine: AsyncEngine) -> tuple[dict[str, int], list[tuple[str, int]], dict[str, str]]:
    counts: dict[str, int] = {}
    domains: list[tuple[str, int]] = []
    identity: dict[str, str] = {}
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "select current_database(), current_user, "
                    "inet_server_addr()::text, inet_server_port()"
                )
            )
        ).one()
        identity = {
            "database": str(row[0]),
            "user": str(row[1]),
            "server": f"{row[2]}:{row[3]}",
        }
        for table in (*RUNTIME_TABLES, *PRESERVED_TABLES):
            exists = (
                await conn.execute(text("select to_regclass(:name)"), {"name": f"public.{table}"})
            ).scalar()
            if exists is None:
                if table in RUNTIME_TABLES:
                    raise RuntimeError(f"expected application table is missing: {table}")
                continue
            counts[table] = int((await conn.execute(text(f'SELECT count(*) FROM "{table}"'))).scalar_one())
        if counts.get("users", 0):
            domains = [
                (str(domain), int(count))
                for domain, count in (
                    await conn.execute(
                        text(
                            "select split_part(email, '@', 2), count(*) "
                            "from users group by 1 order by 2 desc, 1"
                        )
                    )
                ).all()
            ]
    return counts, domains, identity


async def _redis_snapshot(settings: Settings) -> tuple[int, int]:
    from redis.asyncio import Redis

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        db_size = int(await redis.dbsize())
        scanned = 0
        async for _key in redis.scan_iter(count=500):
            scanned += 1
        return db_size, scanned
    finally:
        await redis.aclose()


def _qdrant_client(settings: Settings):
    from qdrant_client import QdrantClient

    if settings.qdrant_local_path:
        return QdrantClient(path=settings.qdrant_local_path)
    return QdrantClient(url=settings.qdrant_url)


def _qdrant_snapshot(settings: Settings) -> dict[str, int]:
    client = _qdrant_client(settings)
    try:
        names = {item.name for item in client.get_collections().collections}
        wanted = (settings.qdrant_collection, *APP_COLLECTION_SUFFIXES)
        return {
            name: int(client.get_collection(name).points_count or 0)
            for name in wanted
            if name in names
        }
    finally:
        client.close()


async def snapshot(settings: Settings) -> tuple[dict[str, int], list[tuple[str, int]], dict[str, str], tuple[int, int], dict[str, int], StorageSnapshot]:
    engine = create_async_engine(settings.migration_database_url, pool_pre_ping=True)
    try:
        db, domains, identity = await _database_snapshot(engine)
        redis = await _redis_snapshot(settings)
    finally:
        await engine.dispose()
    qdrant = await asyncio.to_thread(_qdrant_snapshot, settings)
    storage = await asyncio.to_thread(storage_snapshot, settings)
    return db, domains, identity, redis, qdrant, storage


def print_snapshot(
    settings: Settings,
    instance: str,
    db: dict[str, int],
    domains: list[tuple[str, int]],
    identity: dict[str, str],
    redis: tuple[int, int],
    qdrant: dict[str, int],
    storage: StorageSnapshot,
) -> None:
    print(f"INSTANCE={instance}")
    print(f"APP_ENV={settings.app_env}")
    print(f"DATABASE_TARGET={_display_target(settings.migration_database_url, 5432)}")
    print(f"DATABASE_IDENTITY={identity}")
    print(f"REDIS_TARGET={_display_target(settings.redis_url, 6379)}")
    print(f"REDIS_DB_SIZE={redis[0]} REDIS_SCANNED={redis[1]}")
    print(f"QDRANT_TARGET={settings.qdrant_local_path or settings.qdrant_url}")
    print(f"QDRANT_COLLECTIONS={qdrant}")
    print(f"OBJECT_STORAGE={storage.backend} TARGET={storage.target} OBJECTS={storage.objects} BYTES={storage.bytes}")
    print("USER_EMAIL_DOMAINS=")
    for domain, count in domains:
        print(f"  {domain}={count}")
    print("DATABASE_COUNTS=")
    for table in (*RUNTIME_TABLES, *PRESERVED_TABLES):
        if table in db:
            print(f"  {table}={db[table]}")


def validate_target(settings: Settings, args: argparse.Namespace, identity: dict[str, str]) -> None:
    if settings.app_env == "prod" and not args.allow_prod:
        raise RuntimeError("refusing APP_ENV=prod without --allow-prod")
    if args.confirm_instance != args.instance_name:
        raise RuntimeError("--confirm-instance does not exactly match the configured instance name")
    expected_db_host = args.expected_db_host
    expected_redis_host = args.expected_redis_host
    expected_qdrant = args.expected_qdrant_url
    if not all((expected_db_host, expected_redis_host, expected_qdrant)):
        raise RuntimeError("execute requires expected DB host, Redis host and Qdrant URL")
    db_host, db_port, db_name = _target(settings.migration_database_url, 5432)
    app_host, app_port, app_db = _target(settings.database_url, 5432)
    if (db_host, db_port, db_name) != (expected_db_host, args.expected_db_port, args.expected_database):
        raise RuntimeError(f"migration target mismatch: {db_host}:{db_port}/{db_name}")
    if (app_host, app_port, app_db) != (expected_db_host, args.expected_db_port, args.expected_database):
        raise RuntimeError(f"application target mismatch: {app_host}:{app_port}/{app_db}")
    if identity["database"] != args.expected_database:
        raise RuntimeError(f"connected database is {identity['database']!r}, expected {args.expected_database!r}")
    redis_host, redis_port, redis_db = _target(settings.redis_url, 6379)
    if (redis_host, redis_port, redis_db) != (expected_redis_host, args.expected_redis_port, str(args.expected_redis_db)):
        raise RuntimeError(f"Redis target mismatch: {redis_host}:{redis_port}/db{redis_db}")
    actual_qdrant = settings.qdrant_local_path or settings.qdrant_url
    if actual_qdrant != expected_qdrant:
        raise RuntimeError(f"Qdrant target mismatch: {actual_qdrant}")
    if settings.storage_backend == "minio" and settings.minio_bucket != args.expected_bucket:
        raise RuntimeError(f"object bucket mismatch: {settings.minio_bucket}")


async def _delete_redis(settings: Settings) -> int:
    from redis.asyncio import Redis

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        keys = [key async for key in redis.scan_iter(count=500)]
        for start in range(0, len(keys), 500):
            await redis.delete(*keys[start : start + 500])
        return len(keys)
    finally:
        await redis.aclose()


def _delete_qdrant(settings: Settings) -> int:
    client = _qdrant_client(settings)
    deleted = 0
    try:
        names = {item.name for item in client.get_collections().collections}
        for name in (settings.qdrant_collection, *APP_COLLECTION_SUFFIXES):
            if name not in names:
                continue
            offset = None
            while True:
                points, offset = client.scroll(
                    collection_name=name,
                    offset=offset,
                    limit=500,
                    with_payload=False,
                    with_vectors=False,
                )
                if points:
                    from qdrant_client import models

                    client.delete(
                        collection_name=name,
                        points_selector=models.PointIdsList(points=[point.id for point in points]),
                        wait=True,
                    )
                    deleted += len(points)
                if offset is None:
                    break
    finally:
        client.close()
    return deleted


def _delete_storage(settings: Settings) -> int:
    if settings.storage_backend == "minio":
        from minio import Minio

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_app_access_key,
            secret_key=settings.minio_app_secret_key,
            secure=settings.minio_secure,
        )
        if not client.bucket_exists(settings.minio_bucket):
            return 0
        objects = list(client.list_objects(settings.minio_bucket, recursive=True))
        for obj in objects:
            client.remove_object(settings.minio_bucket, obj.object_name)
        return len(objects)

    root = _local_root(settings)
    objects = _local_objects(root)
    for path in objects:
        path.unlink()
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        directory.rmdir()
    return len(objects)


async def _truncate_database(settings: Settings) -> None:
    engine = create_async_engine(settings.migration_database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            table_sql = ", ".join(f'"{table}"' for table in RUNTIME_TABLES)
            await conn.execute(text(f"TRUNCATE TABLE {table_sql} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true", help="perform the irreversible purge")
    p.add_argument("--confirm-instance", default="", help="must exactly equal --instance-name")
    p.add_argument("--instance-name", default=_arg_or_env("PURGE_INSTANCE_NAME", "eduagents-local-dev"))
    p.add_argument("--allow-prod", action="store_true", help="explicitly allow APP_ENV=prod")
    p.add_argument("--expected-db-host", default=_arg_or_env("PURGE_EXPECTED_DB_HOST"))
    p.add_argument("--expected-db-port", type=int, default=int(_arg_or_env("PURGE_EXPECTED_DB_PORT", "5432")))
    p.add_argument("--expected-database", default=_arg_or_env("PURGE_EXPECTED_DATABASE", "eduagents"))
    p.add_argument("--expected-redis-host", default=_arg_or_env("PURGE_EXPECTED_REDIS_HOST"))
    p.add_argument("--expected-redis-port", type=int, default=int(_arg_or_env("PURGE_EXPECTED_REDIS_PORT", "6379")))
    p.add_argument("--expected-redis-db", type=int, default=int(_arg_or_env("PURGE_EXPECTED_REDIS_DB", "0")))
    p.add_argument("--expected-qdrant-url", default=_arg_or_env("PURGE_EXPECTED_QDRANT_URL"))
    p.add_argument("--expected-bucket", default=_arg_or_env("PURGE_EXPECTED_BUCKET", "eduagents"))
    return p


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    db, domains, identity, redis, qdrant, storage = await snapshot(settings)
    print_snapshot(settings, args.instance_name, db, domains, identity, redis, qdrant, storage)
    if not args.execute:
        print("MODE=dry-run (no data changed)")
        return 0
    validate_target(settings, args, identity)
    print("MODE=execute")
    print("CONFIRMED: external objects, Redis DB and runtime tables will be permanently deleted")
    try:
        deleted_qdrant = await asyncio.to_thread(_delete_qdrant, settings)
        deleted_objects = await asyncio.to_thread(_delete_storage, settings)
        deleted_redis = await _delete_redis(settings)
        await _truncate_database(settings)
    except Exception as exc:  # noqa: BLE001 - report the exact partial boundary
        print(f"PURGE_FAILED={type(exc).__name__}: {exc}")
        print("No success claim was made; inspect every store before retrying.")
        return 1
    print(f"DELETED_QDRANT_POINTS={deleted_qdrant}")
    print(f"DELETED_OBJECTS={deleted_objects}")
    print(f"DELETED_REDIS_KEYS={deleted_redis}")
    print("DELETED_DATABASE_TABLES=" + str(len(RUNTIME_TABLES)))
    print("PRESERVED_TABLES=" + ",".join(PRESERVED_TABLES))
    after = await snapshot(settings)
    after_db, _domains, _identity, after_redis, after_qdrant, after_storage = after
    nonzero = {table: count for table, count in after_db.items() if table in RUNTIME_TABLES and count}
    if nonzero or after_redis[0] or any(after_qdrant.values()) or after_storage.objects:
        print("PURGE_VERIFY_FAILED", {"database": nonzero, "redis": after_redis, "qdrant": after_qdrant, "objects": after_storage.objects})
        return 1
    if not after_db.get("alembic_version"):
        print("PURGE_VERIFY_FAILED alembic_version disappeared")
        return 1
    print("PURGE_VERIFY=PASS")
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run(parser().parse_args())))
    except Exception as exc:  # noqa: BLE001 - CLI must report failed audits clearly
        raise SystemExit(f"PURGE_ABORTED={type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    main()
