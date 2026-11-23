# 本地开发与测试

> 本文只描述本地工作流。项目 Python 固定使用 Conda 环境，不维护部署或线上操作说明。

## 环境

- Python 3.12：`$HOME/miniforge3/envs/eduagents`
- Node.js 20+ 与 npm
- DSH SDK/runtime 固定 `0.2.2rc2`；平台 wheel 自带运行时，无须升级系统 Node 来运行 DSH
- PostgreSQL 16、Redis 7、Qdrant
- 文件对象链路按 `.env` 使用 MinIO 或本地实现

即使终端当前处于 `base`，也不要裸跑 `python`、`pip`、`pytest` 或 `alembic`。所有 Python
命令从仓库根目录通过 Makefile 执行：

~~~bash
make env-check
~~~

Makefile 会直接调用：

~~~text
$HOME/miniforge3/envs/eduagents/bin/python
~~~

并校验 `sys.prefix` 和 Python 3.12。其他机器可显式覆盖：

~~~bash
make PY_ENV=/absolute/path/to/conda-env env-check
~~~

## 初始化与启动

~~~bash
cd /path/to/eduagent
[ -f .env ] || cp .env.example .env
make install
(cd apps/web && npm ci)
make dev-infra
make migrate
make seed
~~~

`make dev-infra` 幂等启动本机 PostgreSQL、Redis 和 Qdrant。然后在三个终端运行：

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

本地地址：

- Web：`http://127.0.0.1:3000`
- API：`http://127.0.0.1:8000`
- Swagger：`http://127.0.0.1:8000/api/docs`
- 就绪检查：`http://127.0.0.1:8000/api/health/ready`

本地 seed 账号：

- `demo@eduagents.cn` / `demo-pass-123`
- `admin@eduagents.cn` / `admin-pass-123`

## 模型模式

| `GATEWAY_MODE` | 本地用途 |
|---|---|
| `stub` | 默认；确定性开发桩，不需要模型密钥 |
| `live` | 联调真实模型，密钥只写未提交的 `.env` |
| `record` | 临时录制调试，输出必须放仓库外 |
| `replay` | 回放已有磁带 |

stub 输出不能用来判断模型质量。真实模型、嵌入、重排和联网搜索变量见 `.env.example`。

## Harness 运行与旧会话迁移

普通/附件问答、辅导、出题和计划都通过 DSH，再进入现有模型网关。
`HARNESS_TIMEOUT_SECONDS` 默认 180 秒，`HARNESS_MAX_CONCURRENCY` 默认 8；超时和显式
停止取消模型工作并回收子进程。SDK 处于预览阶段，升级时须同时验证 profile patch、
原生工具历史和协议。

升级已有本地数据库时，先停止 API/Worker，再安装仅用于旧数据导入的可选依赖：

~~~bash
"$HOME/miniforge3/envs/eduagents/bin/pip" install -e 'packages/core[legacy-migration]'
make migrate
~~~

`make migrate` 应用 `0039` 并导入旧会话；新库跳过旧数据读取，不需要上述可选依赖。
导入器不执行旧图，不删除旧表，不覆盖已有新会话。导入完成后 API 和 Worker 只运行 DSH。
`make migrate-legacy` 可单独幂等重跑导入。`test_dsh_runtime.py` 使用真实配套二进制和
测试模型网关，不向真实模型发送请求；其结果不能用于宣称模型质量或成本改善。

## 验证命令

后端静态检查与单测：

~~~bash
make docs-check
make lint
make compile
make test-unit
~~~

集成测试：

~~~bash
make test-integration
make migration-check
~~~

集成测试会创建名称随机的临时 PostgreSQL 数据库、临时 Redis 和对象目录，并在结束后清理。
fixture 会拒绝远程数据库和无法确认名称的连接，不会清空本地开发库 `eduagents`。模型、
嵌入、重排和邮件在测试中使用开发替身。

单个迁移测试：

~~~bash
make test-migration TEST=tests/integration/test_migration_0029.py
make test-migration TEST=tests/integration/test_migration_0038.py
make test-migration TEST=tests/integration/test_migration_0039.py
~~~

0038 新增普通练习/复习提交记录，运行更新后的 API 前执行 `make migrate`。
专项验证评分异常、并发提交去重、评分重试与账号生命周期：

~~~bash
INTEGRATION=1 "$HOME/miniforge3/envs/eduagents/bin/python" -m pytest tests/integration/test_assessment_submissions.py -q
~~~

前端：

~~~bash
make test-web
~~~

该命令运行 Web 单测、TypeScript、ESLint 和 Next.js 构建。直接修改 `apps/web` 前先读
`apps/web/AGENTS.md`，并按要求查阅当前安装版本的 Next.js 本地文档。

## Migration 规则

1. 已提交的 migration 不修改；schema 变化新增 revision。
2. revision 链保持单一 head。
3. ORM、migration、API 类型和[数据模型](data-model.md)同步修改。
4. 新 migration 增加从旧 head 升级的专项测试。
5. 应用连接与 migration 连接分离；应用角色不得绕过 RLS。

常用命令：

~~~bash
make migrate
make migration-check
~~~

## 业务变更规则

- 资源查询必须带用户或机构所有权边界。
- pending 或未核验内容不得进入 FSRS、掌握度、成长分析和推荐。
- 掌握度更新必须复用统一证据入口，不能新增第二套 BKT 写逻辑。
- 阅读、普通对话、模型小结、反讲反馈和计划任务完成不是掌握证据；只有普通练习作答、
  到期复习、核验后的真实作答及其申诉重判可以调用掌握度写入口。
- `p_mastery` 是包含先验的估计值；所有薄弱判断必须同时要求统一的 `evidence_state` 为
  `observed`，不能把 `unobserved` 或 `estimated` 显示成稳定薄弱。
- 评分、掌握度和错题等证据写入必须幂等，并在同一事务完成校验与保存。
- 新错误码同步 [API 契约](api-contract.md)、前端提示和测试。
- 前端模型长文本使用统一 Markdown 渲染器，短公式字段使用 `MathText`，页面不自行解析
  Markdown 正则。
- 学生核心工作流固定为 `/today`、`/courses`、`/practice`、`/plans`、`/growth`；`/chat`
  是上下文学习工具，新增能力应归入已有用户目标，不能重新扩张为平级页面。
- 异步请求和 SSE 在跨知识库、资源、计划或路由时必须有 request/scope guard。

## 常见故障

| 现象 | 处理 |
|---|---|
| 命令跑到 base 或 Python 3.13 | 停止裸跑，改用 Makefile；先执行 `make env-check` |
| API 报缺表或列 | `make migrate && make migration-check` |
| PostgreSQL/Redis 未就绪 | `make dev-infra`，查看 `~/.local/share/eduagents/` 日志 |
| Qdrant 未就绪 | `curl http://127.0.0.1:6333/healthz` |
| 异步任务不执行 | 确认 `make worker` 进程和 API ready 心跳 |
| Web 类型或构建异常 | `cd apps/web && npm ci && npm run build` |
| 页面认证异常 | Web 与 API 都使用 `127.0.0.1`，保持 cookie 同源口径 |
| 检索始终零命中 | 用 `scripts/check_vector_drift.py` 检查 PG 与 Qdrant 是否脱节 |

本地向量对账要求显式只读 DSN，默认不修改数据：

~~~bash
export READONLY_DATABASE_URL='<local PostgreSQL DSN>'
export READONLY_QDRANT_URL='http://127.0.0.1:6333'
$HOME/miniforge3/envs/eduagents/bin/python scripts/check_vector_drift.py
~~~

## 提交前

~~~bash
git status --short
git diff --check
make docs-check
make lint
make compile
make test-unit
make test-integration
make migration-check
make test-web
~~~

不要把截图、运行证据、临时报告、一次性提示词或新增说明文档提交到仓库。长期事实只更新
根 README 与 `docs/` 中现有四份文档。
