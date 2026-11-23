PY_ENV ?= $(HOME)/miniforge3/envs/eduagents
PY := $(PY_ENV)/bin/python
PIP := $(PY_ENV)/bin/pip
# conda libstdc++ 优先 (pymupdf/qdrant ABI 冲突, 见 tests/conftest.py 注释)
export LD_LIBRARY_PATH := $(PY_ENV)/lib

.PHONY: env-check install dev-infra migrate migrate-legacy migration-check test-migration api worker compile test test-backend test-unit test-integration test-integration-isolated test-web lint docs-check gen-corpus seed eval-retrieval time-travel

env-check:
	@test -x "$(PY)" || { echo "ERROR: project Python not found: $(PY)" >&2; exit 1; }
	@$(PY) -c 'import os, sys; expected=os.path.realpath("$(PY_ENV)"); actual=os.path.realpath(sys.prefix); error = f"ERROR: expected conda env {expected}, got {actual}" if actual != expected else (f"ERROR: expected Python 3.12, got {sys.version.split()[0]}" if sys.version_info[:2] != (3, 12) else ""); error and sys.exit(error); print(f"Python OK: {sys.executable} ({sys.version.split()[0]})")'

install: env-check
	$(PIP) install -e packages/core -e apps/api -e apps/worker pytest pytest-asyncio fakeredis httpx ruff

dev-infra:     ## 启动本机 PostgreSQL、Redis 和 Qdrant
	bash scripts/dev_infra.sh

migrate: env-check
	$(PY) -m alembic upgrade head
	$(PY) scripts/migrate_legacy_harness.py

migrate-legacy: env-check   ## 导入旧会话 (停 API/worker 后执行, 迁移角色, 幂等)
	$(PY) scripts/migrate_legacy_harness.py

migration-check: env-check
	@heads=$$($(PY) -m alembic heads | wc -l); test "$$heads" -eq 1 || { echo "ERROR: expected one Alembic head, got $$heads" >&2; exit 1; }
	$(PY) -m alembic heads

test-migration: env-check
	@test -n "$(TEST)" || { echo "ERROR: pass TEST=tests/integration/test_migration_xxxx.py" >&2; exit 1; }
	MIGRATION_TESTS=1 $(PY) -m pytest $(TEST) -q

api: env-check           ## 本机开发运行 API
	$(PY) -m uvicorn eduagents_api.main:app --reload --port 8000

worker: env-check
	$(PY) -m arq eduagents_worker.main.WorkerSettings

test: test-unit test-integration

test-backend: test

compile: env-check
	$(PY) -m compileall -q packages apps tests alembic eval scripts

test-unit: env-check
	$(PY) -m pytest tests/unit -q

test-integration: env-check  ## 创建一次性 PG/Redis/Qdrant 测试资源后运行集成测试
	INTEGRATION=1 $(PY) -m pytest tests/integration -q

test-integration-isolated: test-integration

test-web:
	cd apps/web && npm run test:unit && npx tsc --noEmit && npm run lint && npm run build

lint: env-check
	$(PY) -m ruff check packages apps tests alembic eval scripts
	$(PY) scripts/check_web_font_sizes.py

docs-check: env-check
	$(PY) scripts/check_docs.py

gen-corpus: env-check
	$(PY) scripts/gen_corpus.py

seed: env-check          ## 学生/管理员演示账号 + 学生知识库 + 摄取语料 (幂等)
	$(PY) scripts/seed_demo.py

eval-retrieval: env-check  ## 检索评估 (需先 make seed; TENANT/KB 从 seed 输出取)
	$(PY) eval/run_retrieval_eval.py --golden eval/golden_set/v1.jsonl \
		--tenant-id $(TENANT) --kb-id $(KB) --output /tmp/eduagents-retrieval-eval.md

time-travel: env-check   ## make time-travel DAYS=3
	$(PY) scripts/time_travel.py $(DAYS)
