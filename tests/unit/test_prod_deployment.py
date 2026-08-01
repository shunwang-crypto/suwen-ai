from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "deploy/docker-compose.prod.yml").read_text()
CADDY = (ROOT / "deploy/Caddyfile.prod").read_text()


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
