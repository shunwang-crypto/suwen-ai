import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

requires_services = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1",
    reason="集成测试需要 PG/Redis: `make up-infra && make test-integration`",
)


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    if os.environ.get("INTEGRATION") != "1":
        yield
        return
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"
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
