from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "deploy/docker-compose.prod.yml").read_text()
CADDY = (ROOT / "deploy/Caddyfile.prod").read_text()
DOCKERFILE = (ROOT / "deploy/api.Dockerfile").read_text()
DEPLOY = (ROOT / "scripts/deploy_prod.sh").read_text()
DEPLOY_DOC = (ROOT / "deploy/README-deploy.md").read_text()
VERIFY = (ROOT / "scripts/verify_prod.sh").read_text()
GRAPH_RUNTIME = (ROOT / "apps/api/src/eduagents_api/graph_runtime.py").read_text()
HEALTH = (ROOT / "apps/api/src/eduagents_api/routers/health.py").read_text()


def _service_block(name: str, next_name: str) -> str:
    start = COMPOSE.index(f"  {name}:\n")
    end = COMPOSE.index(f"  {next_name}:\n", start)
    return COMPOSE[start:end]


def test_only_api_and_worker_have_public_egress():
    assert "  backend:\n    internal: true" in COMPOSE
    assert "  egress:\n    driver: bridge" in COMPOSE
    assert "networks: [backend, egress]" in _service_block("api", "worker")
    assert "networks: [backend, egress]" in _service_block("worker", "web")
    for name, next_name in (("postgres", "redis"), ("redis", "qdrant"), ("qdrant", "minio"), ("minio", "minio-init"), ("minio-init", "migrate"), ("migrate", "api"), ("web", "caddy")):
        assert "egress" not in _service_block(name, next_name)


def test_docs_are_hidden_before_api_catch_all():
    assert CADDY.index("handle /api/docs*") < CADDY.index("handle /api/*")
    assert CADDY.index("handle /api/openapi.json") < CADDY.index("handle /api/*")
    assert "respond 404" in CADDY


def test_minio_init_precedes_api_and_worker_without_root_credentials():
    init = _service_block("minio-init", "migrate")
    assert "MINIO_ROOT_USER" in init and "MINIO_ROOT_PASSWORD" in init
    assert "MINIO_APP_ACCESS_KEY" in init and "MINIO_APP_SECRET_KEY" in init
    for name, next_name in (("api", "worker"), ("worker", "web")):
        block = _service_block(name, next_name)
        assert "minio-init:" in block
        assert "service_completed_successfully" in block
        assert 'MINIO_ROOT_USER: ""' in block
        assert 'MINIO_ROOT_PASSWORD: ""' in block
        assert "MINIO_APP_ACCESS_KEY" in block
        assert "MINIO_APP_SECRET_KEY" in block
        assert "MINIO_ROOT_USER:?" not in block
        assert "MINIO_ROOT_PASSWORD:?" not in block


def test_admin_bootstrap_is_in_api_image_and_uses_migration_job():
    assert "COPY scripts/create_first_admin.py scripts/create_first_admin.py" in DOCKERFILE
    assert "run --rm --no-deps migrate" in DEPLOY_DOC
    assert "python /app/scripts/create_first_admin.py" in DEPLOY_DOC
    assert "MIGRATION_DATABASE_URL" in _service_block("migrate", "api")
    assert "MIGRATION_DATABASE_URL: \"\"" in _service_block("api", "worker")
    assert "MIGRATION_DATABASE_URL: \"\"" in _service_block("worker", "web")


def test_deploy_requires_clean_tracked_tree_and_reports_release_identity():
    assert 'git -C "$ROOT_DIR" status --porcelain --untracked-files=no' in DEPLOY
    assert 'commit="$(git -C "$ROOT_DIR" rev-parse HEAD)"' in DEPLOY
    assert 'image_tag="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"' in DEPLOY
    assert "SMTP_REPLY_TO" in DEPLOY
    assert "PUBLIC_REGISTRATION=false" in DEPLOY
    assert "EMAIL_VERIFICATION_REQUIRED=true" in DEPLOY


def test_external_verifier_checks_page_and_private_docs():
    assert '"$BASE_URL/"' in VERIFY
    assert "/api/docs" in VERIFY and "/api/openapi.json" in VERIFY
    assert '[[ "$status" == 404 ]]' in VERIFY
    assert 'openssl x509 -checkhost "$host"' in VERIFY


def test_checkpoint_schema_is_initialized_by_migration_not_api():
    assert "setup_langgraph_checkpoint.py" in DOCKERFILE
    migrate = _service_block("migrate", "api")
    assert "alembic upgrade head && python /app/scripts/setup_langgraph_checkpoint.py" in migrate
    assert "await self._checkpointer.setup()" not in GRAPH_RUNTIME
    assert "migration_database_url" not in GRAPH_RUNTIME
    assert 'checks["graph"]' in HEALTH
