# 开发与测试手册

> 状态：Active
> 目标读者：本地开发者、测试人员和 AI 编程代理
> 生产部署：请改读[部署手册](../deploy/README-deploy.md)。

## 环境要求

- Python 3.12
- Node.js 20+ 与 npm（CI 当前使用 Node 22）
- PostgreSQL 16、Redis 7
- Qdrant；对象存储相关链路需要 MinIO 或兼容实现
- 可选 Docker 20+ 与 Compose v2

当前 Makefile 为开发机提供了默认 conda Python 路径。其他环境应显式覆盖：

~~~bash
make PY=/path/to/python PIP=/path/to/pip install
~~~

不要把本机绝对路径写入新文档、代码或共享配置。

## 首次初始化

~~~bash
git clone <repo-url> eduagents
cd eduagents
[ -f .env ] || cp .env.example .env

make install
(cd apps/web && npm ci)
make dev-infra
make migrate
make seed
~~~

`make dev-infra` 启动本地 PostgreSQL、Redis，并确保 Qdrant 可用；如果选择容器基础设施，
使用 `make up-infra`，不要同时运行占用相同端口的两套服务。

`make seed` 仅用于本地演示，幂等创建学生与管理员账号并摄取示例语料。生产环境禁止运行。

注册流程在生产必须先通过图形验证码再发送邮箱验证码；验证码发送和密码找回都复用
`EmailSender`。本地/测试默认可通过 `EMAIL_VERIFICATION_REQUIRED=0` 复用旧认证夹具，专项
邮箱注册测试使用 fake sender，不连接 Resend。真实 Resend 冒烟只能由运维显式执行并指定
测试收件箱，Key 只存在服务器 Secrets 或未提交的 `.env`。

## 启动开发服务

分别在三个终端运行：

~~~bash
make api
~~~

~~~bash
make worker
~~~

~~~bash
cd apps/web
npm run dev
~~~

验证：

~~~bash
curl -fsS http://127.0.0.1:8000/api/health/live
curl -fsS http://127.0.0.1:8000/api/health/ready
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000
~~~

- `live` 只证明 API 进程活着。
- `ready` 检查 PostgreSQL、Redis、Qdrant 和 Worker 心跳；依赖异常时返回 503。
- Web 开发入口为 `http://127.0.0.1:3000`。

## 网关模式

| `GATEWAY_MODE` | 语义 | 纪律 |
|---|---|---|
| `stub` | 本地确定性桩 | 必须明确标注，不能生成质量结论 |
| `live` | 真实模型调用 | 需要模型、嵌入、重排密钥和预算 |
| `record` | live 并录制请求/响应 | 磁带可能包含敏感内容，提交前审查 |
| `replay` | 回放磁带 | 只证明可重复流程，不等同当前模型质量 |

CI 默认不需要真实模型密钥。生产环境必须显式使用 `APP_ENV=prod` 并遵守部署手册。

生产可选启用 `MODEL_ROUTING_ENABLED=true` 使用独立的 text/vision/plan 候选链；候选端点必须是
HTTPS，密钥只放服务器 Secrets 或未提交的 `.env`。`OPENCODE_ZEN_API_KEY=public` 不是 OpenCode
官方保证的长期生产认证方式，需上线前验证 401/403 并准备真实 Key。完整候选变量见 `.env.example`。

## 测试与质量检查

### Python

~~~bash
make lint
python -m compileall -q packages apps tests alembic
make test-unit
~~~

### 集成测试

`make test-integration` 会在本机 PostgreSQL 上创建一个带随机后缀的临时数据库，并启动一个
随机端口的 disposable Redis 容器；迁移使用本地 migration role，应用使用 `edu_app`，两者
都只指向这次运行创建的数据库。测试结束会先关闭应用连接、LangGraph checkpointer 和
Qdrant 本地 client，再删除精确匹配的临时数据库、Redis 容器和对象目录。

测试 fixture 会拒绝 `APP_ENV=prod`、远程 PostgreSQL，以及无法确认数据库名的连接；不会
清空或删除 `eduagents`、共享 Redis、MinIO 或任何生产资源。集成网关、嵌入、重排和邮件
均强制使用 stub/开发替身，SMTP 测试不会投递邮件。LangGraph 的 checkpointer 表只在临时
测试数据库中由 migration role 初始化，API role 不会在应用启动时执行 setup。

~~~bash
make test-integration
~~~

直接运行时使用：

~~~bash
INTEGRATION=1 python -m pytest tests/integration -q
MIGRATION_TESTS=1 python -m pytest tests/integration/test_migration_0012.py -q
~~~

`test-integration` 已经包含隔离资源生命周期；`test-integration-isolated` 是同一入口的
显式别名。不要在集成测试中改用开发数据库或 Redis DB 0。

集成测试会使用 PostgreSQL、Redis 和测试配置；不要让测试连接生产数据库或生产对象存储。

### Web

~~~bash
cd apps/web
npm run lint
npm run build
~~~

Next.js 版本可能包含超出模型训练数据的新约定。修改 `apps/web` 前必须完整阅读
`apps/web/AGENTS.md`，并按其中要求阅读本地 `node_modules/next/dist/docs/` 的相关文档。

### 文档

~~~bash
make docs-check
~~~

该检查验证 Active 文档索引和相对链接。功能变更若影响产品边界、API、数据或安全，必须
在同一提交更新相应文档。

## Migration 规范

1. 已提交的 migration 永远不修改，包括为了“让测试更容易”。
2. schema 变化创建新 migration，revision 链只能有一个预期 head。
3. ORM、migration、数据模型文档和 API 类型必须同步。
4. 升级必须保护存量数据；危险回填应可审计、可重试或分阶段执行。
5. downgrade 不能伪造已经删除的历史事实；无法恢复时要明确边界。
6. 新 migration 必须增加从旧 head 升级的专项测试，并运行 `alembic heads`。
7. 应用连接和 migration 连接分离：应用角色不得拥有绕过 RLS 的权限。

常用命令：

~~~bash
make migrate
python -m alembic current
python -m alembic heads
~~~

## API 与数据变更规范

- 资源所有权查询必须带租户和用户/机构边界；不可见资源优先使用既有 404 语义。
- 状态机只能沿文档声明方向前进，重试必须幂等。
- pending 或未核验记录不得进入 FSRS、掌握度、画像、报告和推荐。
- `PUT` 表示整体替换时必须在契约和测试中明确；`PATCH` 只做部分更新。
- 评分、掌握度、错题等不可逆证据写入前先完成全部校验，并放在同一事务。
- 新错误码需要加入 API 契约、前端友好提示和集成测试。
- 前后端枚举必须来自同一业务语义；知识库 `owner_type=user|org` 与题目资料
  `owner_type=personal|organization` 是不同契约。

## Git 与工作树规范

修改前后都运行：

~~~bash
git status --short
git diff --check
~~~

- 工作树中的既有修改属于用户，除非用户明确授权，不得覆盖、回滚或顺带提交。
- 一个提交只包含一个清晰目标；业务修复、migration 和大规模文档治理尽量独立提交。
- 不使用 `git reset --hard`、`git checkout --` 等破坏性命令处理未知改动。
- 提交前检查 staged diff，而不只看工作树总 diff。

## 常见故障

| 现象 | 检查 |
|---|---|
| API 报缺表或列 | `make migrate`，再看 `python -m alembic current` |
| PostgreSQL/Redis 未就绪 | `make dev-infra`，检查对应端口和日志 |
| Qdrant 不可用 | `curl http://127.0.0.1:6333/healthz`；检查本地模式是否被多进程占用 |
| 异步任务不执行 | 只保留一个预期开发 Worker，检查 ready 心跳与 ARQ 日志 |
| Web 类型或构建异常 | 在 `apps/web` 运行 `npm ci && npm run build` |
| 页面 API 认证异常 | 使用 `127.0.0.1` 保持同源代理和 cookie 口径一致 |
| stub 输出被误认为真实模型 | 检查 `.env` 的 `GATEWAY_MODE` 和页面环境提示 |
| 对象删除失败 | 查看 pending cleanup、对象存储连接和相关重试状态，不手工删数据库掩盖问题 |
| 知识库能问但检索永远 0 命中 | 向量库与 PG 脱节，跑 `python scripts/check_vector_drift.py`（见下） |

### 向量库对账

`/api/health/ready` 的 qdrant 探针只验连通性，不看数据。若 Qdrant 丢过数据或被重建，
PG 侧仍标着 `embedding_status='done'`，检索会静默返回 0 命中并降级到 web 兜底——
表面正常，RAG 已失效。对账把两侧差异显式化：

~~~bash
python scripts/check_vector_drift.py            # 全量，只列有问题的库
python scripts/check_vector_drift.py --all      # 连一致的库一起列
python scripts/check_vector_drift.py --kb <id>  # 只看一个库
python scripts/check_vector_drift.py --json     # 机器可读，供 cron/告警接管
~~~

退出码：`0` 一致、`1` 存在漂移、`2` 对账本身出错，可直接挂 cron 或 CI。
管理员亦可走 `GET /api/admin/vector-reconcile`（`?kb_id=` 单库、`?include_all=true` 全量）。

两个方向都查，含义不同：

- **`missing_vectors`**（PG 有 / Qdrant 无）：该库检索已失效，需重新摄取文档才能恢复。
- **`orphan_points`**（Qdrant 有 / PG 无）：删除未清理干净。点的 payload 含文档原文，
  知识库删除或账号注销后仍留存属于数据删除不完整，不只是存储浪费。

两侧均为只读，不会自动修改数据。

清理孤儿向量（`missing_vectors` 不适用，那要重新摄取原文档）：

~~~bash
python scripts/purge_orphan_vectors.py             # 预演，只打印将删什么
python scripts/purge_orphan_vectors.py --execute   # 真删，删后自动复核
python scripts/purge_orphan_vectors.py --execute --kb <id>
~~~

默认 dry-run，删除必须显式 `--execute`。只删 PG 中已无对应 `knowledge_bases` 行的
kb_id，现存库一律不碰（哪怕它有漂移）。删除按 `(tenant_id, kb_id)` 过滤下发，不做
无租户删除。**不可逆**：向量重建需要原始文件重新摄取。对账报错时拒绝执行删除。

## 变更完成清单

- [ ] 目标功能、权限和失败路径均有测试。
- [ ] migration（如有）从前一 head 升级通过。
- [ ] Ruff、compileall、TypeScript、ESLint 和生产构建通过。
- [ ] API、数据、安全或部署文档已同步。
- [ ] `make docs-check` 与 `git diff --check` 通过。
- [ ] 工作树只剩明确属于用户或后续任务的改动。
