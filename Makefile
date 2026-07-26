PY := /home/shunw/miniforge3/envs/eduagents/bin/python
PIP := /home/shunw/miniforge3/envs/eduagents/bin/pip
COMPOSE := docker compose -f deploy/docker-compose.yml

.PHONY: install up up-infra down migrate api worker test test-unit test-integration lint lock demo-m0 demo-m1

install:
	$(PIP) install -e packages/core -e apps/api -e apps/worker pytest pytest-asyncio fakeredis httpx ruff

up:            ## 全套服务 (构建 + 启动)
	$(COMPOSE) up -d --build

up-infra:      ## 仅基础设施 (docker 版; daemon DNS 修复后可用)
	$(COMPOSE) up -d postgres redis qdrant minio

dev-infra:     ## 本地 conda 版 PG/Redis (无 Docker 依赖, 见 decisions.md D5)
	bash scripts/dev_infra.sh

down:
	$(COMPOSE) down

migrate:
	$(PY) -m alembic upgrade head

api:           ## 本机开发运行 API
	$(PY) -m uvicorn eduagents_api.main:app --reload --port 8000

worker:
	$(PY) -m arq eduagents_worker.main.WorkerSettings

test: test-unit test-integration

test-unit:
	$(PY) -m pytest tests/unit -q

test-integration:  ## 需要 make up-infra 先起 PG/Redis
	INTEGRATION=1 $(PY) -m pytest tests/integration -q

lint:
	$(PY) -m ruff check packages apps tests

lock:
	$(PIP) freeze --exclude-editable > requirements.lock

demo-m0:       ## M0 DoD: 从零建库迁移成功
	bash scripts/demo_m0.sh

demo-m1:       ## M1 DoD: compose 全绿 + SSE + 降级 + usage + 越权
	bash scripts/demo_m1.sh
