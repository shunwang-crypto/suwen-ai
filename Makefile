PY_ENV ?= /home/shunw/miniforge3/envs/eduagents
PY := $(PY_ENV)/bin/python
PIP := $(PY_ENV)/bin/pip
COMPOSE := docker compose -f deploy/docker-compose.yml
# conda libstdc++ 优先 (pymupdf/qdrant ABI 冲突, 见 tests/conftest.py 注释)
export LD_LIBRARY_PATH := $(PY_ENV)/lib

.PHONY: env-check install up up-infra down migrate migration-check test-migration api worker compile test test-backend test-unit test-integration test-integration-isolated test-web lint docs-check lock demo-m0 demo-m1

env-check:
	@test -x "$(PY)" || { echo "ERROR: project Python not found: $(PY)" >&2; exit 1; }
	@$(PY) -c 'import os, sys; expected=os.path.realpath("$(PY_ENV)"); actual=os.path.realpath(sys.prefix); error = f"ERROR: expected conda env {expected}, got {actual}" if actual != expected else (f"ERROR: expected Python 3.12, got {sys.version.split()[0]}" if sys.version_info[:2] != (3, 12) else ""); error and sys.exit(error); print(f"Python OK: {sys.executable} ({sys.version.split()[0]})")'

install: env-check
	$(PIP) install -e packages/core -e apps/api -e apps/worker pytest pytest-asyncio fakeredis httpx ruff

up:            ## 全套服务 (构建 + 启动)
	$(COMPOSE) up -d --build

up-infra:      ## 仅基础设施 (docker 版; daemon DNS 修复后可用)
	$(COMPOSE) up -d postgres redis qdrant minio

dev-infra:     ## 本地 conda 版 PG/Redis (无 Docker 依赖, 见 decisions.md D5)
	bash scripts/dev_infra.sh

down:
	$(COMPOSE) down

migrate: env-check
	$(PY) -m alembic upgrade head

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
	$(PY) -m compileall -q packages apps tests alembic eval scripts loadtest

test-unit: env-check
	$(PY) -m pytest tests/unit -q

test-integration: env-check  ## 创建一次性 PG/Redis/Qdrant 测试资源后运行集成测试
	INTEGRATION=1 $(PY) -m pytest tests/integration -q

test-integration-isolated: test-integration

test-web:
	cd apps/web && npm run test:unit && npx tsc --noEmit && npm run lint && npm run build

lint: env-check
	$(PY) -m ruff check packages apps tests alembic eval scripts loadtest
	$(PY) scripts/check_web_font_sizes.py

docs-check: env-check
	$(PY) scripts/check_docs.py

gen-corpus: env-check
	$(PY) scripts/gen_corpus.py

seed: env-check          ## 学生/管理员演示账号 + 学生知识库 + 摄取语料 (幂等)
	$(PY) scripts/seed_demo.py

eval-retrieval: env-check  ## 检索评估 (需先 make seed; TENANT/KB 从 seed 输出取)
	$(PY) eval/run_retrieval_eval.py --golden eval/golden_set/v1.jsonl \
		--tenant-id $(TENANT) --kb-id $(KB) --output /tmp/eduagents-evidence/retrieval_eval.md

lock: env-check
	$(PIP) freeze --exclude-editable > requirements.lock

demo-m0:       ## M0 DoD: 从零建库迁移成功
	bash scripts/demo_m0.sh

demo-m1:       ## M1 DoD: compose 全绿 + SSE + 降级 + usage + 越权
	bash scripts/demo_m1.sh

.PHONY: time-travel
time-travel: env-check   ## make time-travel DAYS=3 (演示: 虚拟时钟前进, §9)
	$(PY) scripts/time_travel.py $(DAYS)

# ===== 工程收尾追加 (备份/预热/demo-m2..m5); 只追加, 不改上方既有 target =====
.PHONY: backup restore preheat demo-m2 demo-m3 demo-m4 demo-m5

backup:        ## 备份三件套: pg_dump -Fc + objects tar + Qdrant 快照 (保留14份轮转)
	bash scripts/backup.sh

restore:       ## 恢复: make restore STAMP=20260727-033000 (交互二次确认)
	bash scripts/restore.sh $(STAMP)

preheat:       ## 会前预热: 三服务健康 + API/web 探活 + demo 登录 + chat 预热缓存 (§9.4)
	bash scripts/preheat.sh

demo-m2: env-check       ## M2 演示: RAG e2e (上传→两阶段摄取→混合检索→引用→拒答→恶意文件拒收)
	@INTEGRATION=1 $(PY) -m pytest tests/integration/test_rag_e2e.py -q \
		&& echo "demo-m2: PASS (RAG e2e; M2-D2/D3/D4 链路)" \
		|| { echo "demo-m2: FAIL"; exit 1; }
	@echo "-- 检索评估 CLI (结果写入临时目录，不进入仓库): --"
	@echo "   make seed                                    # 输出 TENANT/KB id"
	@echo "   make eval-retrieval TENANT=<tenant> KB=<kb>  # 黄金集 hit_rate@5 / MRR / nDCG@10"
	@echo "   消融开关: eval/run_retrieval_eval.py --no-sparse | --no-rerank (M2-D5)"

demo-m3: env-check       ## M3 演示: 出题 interrupt→resume + schema 校验 + VLSE 闭环 (quiz→BKT→FSRS→复习)
	@INTEGRATION=1 $(PY) -m pytest tests/integration/test_quiz_flow.py tests/integration/test_vlse_loop.py -q \
		&& echo "demo-m3: PASS (quiz interrupt/resume + VLSE 闭环; M3-D2/D3 链路)" \
		|| { echo "demo-m3: FAIL"; exit 1; }

demo-m4: env-check       ## M4 演示: 错题→time-travel→到期复习 + 计划错误路径 (M4-D2/D4 链路)
	@INTEGRATION=1 $(PY) -m pytest tests/integration/test_reviews_flow.py tests/integration/test_plan_errors.py -q \
		&& echo "demo-m4: PASS (reviews flow + plan errors; M4-D2/D4 链路)" \
		|| { echo "demo-m4: FAIL"; exit 1; }

demo-m5: env-check       ## M5 演示: 闭环实验快速版 (实验B, 3 seeds, 输出到临时目录, 不覆盖正式证据)
	@OUT=$$(mktemp -d /tmp/eduagents-demo-m5.XXXXXX) && \
	$(PY) eval/simulation/run_experiment.py --experiment b --seeds 3 --skip-sensitivity --output $$OUT \
		&& echo "demo-m5: PASS (快速版；结果位于临时目录，正式生产基线待重建)" \
		|| { echo "demo-m5: FAIL"; exit 1; }
	@echo "-- 压测指引: loadtest/README.md；结果必须写入临时目录或明确日期的外部 evidence --"
