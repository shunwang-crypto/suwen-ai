import asyncio
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

requires_services = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1",
    reason="集成测试需要隔离的临时 PG/Redis: `make test-integration`",
)


def _redact_connection_text(value: str) -> str:
    return re.sub(r"(postgres(?:ql)?(?:\+[a-z0-9_]+)?://)[^\s]+", r"\1<redacted>", value, flags=re.I)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


async def _create_database(database_name: str, source_url) -> None:
    import asyncpg

    conn = await asyncpg.connect(
        user=source_url.username,
        password=source_url.password,
        host=source_url.host,
        port=source_url.port or 5432,
        database="postgres",
    )
    try:
        await conn.execute(
            f"CREATE DATABASE {_quote_identifier(database_name)} "
            f"OWNER {_quote_identifier(source_url.username)}"
        )
    finally:
        await conn.close()


async def _drop_database(database_name: str, source_url) -> None:
    import asyncpg

    conn = await asyncpg.connect(
        user=source_url.username,
        password=source_url.password,
        host=source_url.host,
        port=source_url.port or 5432,
        database="postgres",
    )
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await conn.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(database_name)}")
    finally:
        await conn.close()


def _start_isolated_redis() -> tuple[str, str]:
    """Start a disposable Redis container and return (container, URL).

    A separate process is required here: selecting another logical DB on the
    developer's Redis would still allow accidental FLUSHDB/key collisions.
    """
    name = f"eduagents-test-redis-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    command = [
        "docker", "run", "--detach", "--rm", "--name", name,
        "--publish", "127.0.0.1::6379", "redis:7-alpine",
        "redis-server", "--save", "", "--appendonly", "no",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        container = result.stdout.strip()
        port_result = subprocess.run(
            ["docker", "port", name, "6379/tcp"],
            check=True,
            capture_output=True,
            text=True,
        )
        binding = port_result.stdout.strip().splitlines()[0].rsplit(":", 1)[-1]
        return container, f"redis://127.0.0.1:{int(binding)}/0"
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True)
        raise RuntimeError(
            "无法启动隔离 Redis；请确保 Docker 可用且 redis:7-alpine 镜像已就绪"
        ) from exc


def _stop_isolated_redis(container: str) -> None:
    # Use the exact container id/name created above; never remove a broad set
    # of containers or touch the developer's shared Redis service.
    subprocess.run(["docker", "rm", "--force", container], capture_output=True, text=True)


async def _setup_checkpoint_tables(database_url: str) -> None:
    """Prepare LangGraph's PostgreSQL checkpointer in the disposable DB.

    Checkpoint tables are intentionally not application migrations: the API
    role must not run ``setup()`` in production. The isolated test database
    is created with the migration role, so this keeps integration tests as
    close as possible to the deployment contract without touching a shared
    database.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    conninfo = database_url.replace("postgresql+asyncpg://", "postgresql://")
    pool = AsyncConnectionPool(
        conninfo,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    await pool.open()
    try:
        await AsyncPostgresSaver(pool).setup()
    finally:
        await pool.close()


@pytest.fixture(scope="session", autouse=True)
def _isolated_services():
    """Provision disposable PostgreSQL and Redis for the integration run.

    The fixture refuses production mode, creates a unique database owned by
    the migration role, and tears down only the exact database/container it
    created. Qdrant and object storage are already local paths from the root
    test conftest.
    """
    if os.environ.get("INTEGRATION") != "1":
        yield
        return

    from eduagents_core.config import get_settings
    from sqlalchemy.engine import make_url

    get_settings.cache_clear()
    source_settings = get_settings()
    if os.environ.get("APP_ENV") == "prod" or source_settings.app_env == "prod":
        pytest.fail("集成测试拒绝 APP_ENV=prod")
    source_migration_url = make_url(source_settings.migration_database_url or source_settings.database_url)
    source_database = source_migration_url.database
    source_host = source_migration_url.host or ""
    if source_host not in {"127.0.0.1", "localhost"}:
        pytest.fail("集成测试只允许本机 PostgreSQL，不连接远程/生产数据库")
    if not source_database or source_database == "postgres":
        pytest.fail("无法确认开发 PostgreSQL 数据库，拒绝创建临时库")

    database_name = f"eduagents_test_{os.getpid()}_{uuid.uuid4().hex[:12]}"
    redis_container = ""
    previous_env = {
        key: os.environ.get(key)
        for key in ("DATABASE_URL", "MIGRATION_DATABASE_URL", "REDIS_URL", "APP_ENV", "STORAGE_BACKEND")
    }
    try:
        asyncio.run(_create_database(database_name, source_migration_url))
        app_url = make_url(source_settings.database_url).set(database=database_name)
        migration_url = source_migration_url.set(database=database_name)
        redis_container, redis_url = _start_isolated_redis()
        os.environ["DATABASE_URL"] = str(app_url)
        os.environ["MIGRATION_DATABASE_URL"] = str(migration_url)
        os.environ["REDIS_URL"] = redis_url
        # Keep the normal dev semantics for integration tests (some tests
        # explicitly switch to APP_ENV=test to verify that channel).
        os.environ["APP_ENV"] = "dev"
        os.environ["STORAGE_BACKEND"] = "local"
        get_settings.cache_clear()
        yield
    except Exception:
        raise
    finally:
        from eduagents_api.db import dispose_engine

        asyncio.run(dispose_engine())
        try:
            from eduagents_core.vectorstore.qdrant_store import _client_cache

            for client in list(_client_cache.values()):
                client.close()
            _client_cache.clear()
        except Exception:  # noqa: BLE001 - cleanup must not hide the test result
            pass
        if redis_container:
            _stop_isolated_redis(redis_container)
        asyncio.run(_drop_database(database_name, source_migration_url))
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _migrated(_isolated_services):
    if os.environ.get("INTEGRATION") != "1":
        yield
        return
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "alembic upgrade failed:\n"
        + _redact_connection_text(result.stderr)
    )
    from eduagents_core.config import get_settings

    asyncio.run(_setup_checkpoint_tables(get_settings().migration_database_url))
    yield


@pytest.fixture
async def client():
    from eduagents_core.config import get_settings

    get_settings.cache_clear()
    from eduagents_api.main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
