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

使用官方 DeepSeek Flash 时，将相应档位的 `*_BASE_URL` 设为 `https://api.deepseek.com`、
`*_MODEL` 设为 `deepseek-flash`，并通过 `*_ENABLE_THINKING=false` 关闭默认开启的思考模式。
网关为官方接口发送 `thinking: {type: disabled}`，其他已配置的双模式接口继续使用
`enable_thinking`。需要让所有教学任务使用同一 Flash 模型时，配置 cheap、standard、strong
和 plan 四个档位，并关闭可选的 `MODEL_ROUTING_ENABLED` 路由。密钥只保存在本地 `.env`；
token 单价用于预算估算，实际费用以模型提供方账单为准。

## Harness 运行与旧会话迁移

教育任务由原生 DSH 运行，领域工具定义教学规则；单次评分/评审服务仍可直接使用网关。
`HARNESS_TIMEOUT_SECONDS` 默认 180 秒，`HARNESS_MAX_CONCURRENCY` 默认 8，
`HARNESS_MAX_MODEL_CALLS` 默认 16，`HARNESS_CONTEXT_WINDOW_TOKENS` 默认 32768。
自动压缩请求也计入模型调用上限。超时和取消回收子进程并保存可用原生检查点。
SDK 处于预览阶段，升级时同时验证 profile patch、原生日志、压缩及工具协议。

~~~bash
make migrate
~~~

`make migrate` 只应用 Alembic，包括 `0040` 原生任务和 `0041` 记忆迁移。已有旧 LangGraph checkpoint
且需要恢复其待确认业务状态时，停 API/Worker 后显式执行离线导入：

~~~bash
"$HOME/miniforge3/envs/eduagents/bin/pip" install -e 'packages/core[legacy-migration]'
make migrate-legacy
~~~

新库无需旧依赖。导入器不执行旧图、不删除旧表、不覆盖新会话。`test_dsh_runtime.py`
和 `test_native_agent_tasks.py` 使用真实配套二进制，验证工具执行、确认权限、原生恢复、
压缩、取消、BYOK 与计量。测试替身生成的内容明确标记为开发桩，不能用于模型质量结论。

## 验证命令

后端静态检查与单测：

~~~bash
make docs-check
make lint
make compile
make test-unit
~~~

提示词版本和离线进化：

~~~bash
make eval-prompts-smoke
# 显式调用已配置的真实模型；候选与报告保存在临时目录。
$HOME/miniforge3/envs/eduagents/bin/python eval/run_prompt_evolution.py \
  --live --propose-version edu-v2 --candidate-output /tmp/edu-v2.json \
  --repetitions 2 --max-calls 600 --max-cost-cny 10 \
  --output /tmp/eduagents-prompt-comparison.json
~~~

`--live` 的费用上限覆盖主 Agent、压缩、独立评审和候选反思；达到上限后停止新调用，
最后一个在途调用仍按实际费用计入。CLI 在固定合成资料上比较教学策略，不访问应用数据库。
已有候选可用 `--candidate /tmp/edu-v2.json` 代替 `--propose-version` 和 `--candidate-output`。

记忆回归使用合成数据，不调用真实模型、不写应用历史：

~~~bash
make eval-memory
make test-migration TEST=tests/integration/test_migration_0041.py
~~~

13 个本地案例覆盖跨会话召回、知识更新、用户纠正、过期、抑制和无证据时返回空。
这不是 LongMemEval 官方分数，也不能证明真实模型提取或讲解质量；数据库集成测试验证
本人/课程隔离和后台删除竞态，原生 Harness 测试验证记忆工具与归档失效。
比较前校验候选声明的父版本；调优和留出输入不得重复。真实费用、按任务质量及失败原因
写入 JSON 报告，桩模型不会生成有效质量分数。质量比较仍需要教师检查实际教学表现。

采纳候选时将经过审查的 JSON 放入 core 的 `education/prompt_versions/`，新增版本而不覆盖
旧版本，完成检查后用 `EDUCATION_PROMPT_VERSION` 选择它。原任务固定旧教学版本，当前权限
规则仍重新计算；恢复所需的旧版本缺失或内容被修改时明确报错。回滚选择旧版本即可。
修改工具协议或权限需另改业务代码和相应测试，不通过候选教学提示词改变。

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
