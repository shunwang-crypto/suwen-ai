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

## 教材检索与本地 GPU

在 WSL 终端用 `nvidia-smi` 确认显卡，使用 WSL 能访问的 Ollama 服务。新知识库可先
运行 `ollama pull bge-m3` 和 `ollama pull qwen3:4b-instruct-2507-q4_K_M`。
在本地 `.env` 使用以下配置并重启 API 和 Worker。

~~~dotenv
EMBEDDING_MODE=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
GATEWAY_MODE=live
OLLAMA_CHAT_MODEL=qwen3:4b-instruct-2507-q4_K_M
OLLAMA_REQUIRE_GPU=true
MEMORY_SEMANTIC_MODE=auto
RETRIEVAL_ENABLE_RERANK=false
WORKER_MAX_JOBS=1
~~~

教材和语义记忆共享 Ollama。嵌入客户端按四条一批串行请求 1024 维向量。
API 和 Worker 启动时分别预热并核对已加载模型的 `size_vram` 和 `size`，发现 CPU 分层即停止启动。
可在服务日志查 `ollama_gpu_verified`，或在运行时访问 `/api/ps`。单模型调度时两种模型轮流占用显存。
设置 `OLLAMA_CHAT_MODEL` 后，文本生成和来源核验均使用本地模型，关闭该模型开关即可使用配置的云网关。
模型用法见 [Ollama embed](https://docs.ollama.com/api/embed) 和
[运行中模型的显存字段](https://docs.ollama.com/api/ps)。

启动本机服务并登录演示账号后，可从一个新知识库导入《动手学深度学习》中文教材，再运行真实课程评测。

~~~bash
py="$HOME/miniforge3/envs/eduagents/bin/python"
"$py" scripts/import_d2l_course.py --demo
"$py" -m eval.run_course_rag --demo --output /tmp/eduagents-deep-learning-course.json
~~~

导入器按固定版本的官方目录获取 140 份 Markdown 和一份书目文件，合并为 20 份课程资料。代码、公式和原文链接随章节保存。
默认收据位于 `~/.local/share/eduagents/course-imports/d2l-zh/import.json`，重跑使用同一收据续传。使用其他账号时提供环境变量
`EDUAGENTS_TOKEN`，每个课程可用单独的 `--work-dir` 并在评测端传 `--receipt`。题集覆盖 15 个教学章节和 3 个资料外问题；
其中 5 道课程题和 3 道资料外题串行经过实际聊天接口。评测分别记录指定小节排名、答案关键词、引用打开结果、拒答原因和缓存命中。
原文与许可证见 [D2L 中文教材仓库](https://github.com/d2l-ai/d2l-zh/tree/e6b18ccea71451a55fcd861d7b96fddf2587b09a)。

本机这次导入生成 1585 个父块和 3374 个可检索子块，逐个核对了 PostgreSQL 与 Qdrant 的子块 ID 和租户。
15 道章节题中，14 道的指定小节排在第一；GPU 异步计时题未进入前三。8 道聊天均未使用答案缓存，5 道课内题
通过内容检查并引用了可打开的指定章节，3 道资料外题在检索后以 `insufficient_evidence` 拒答。
首次测量发现关键词检查漏掉交并比解释中的错误，复测增加了矛盾表述检查。这组结果用于回归，样本数量不足以估计整门课的教学正确率。
本轮串行测试用了 272 秒。RTX 5060 Laptop 的整卡显存采样峰值为 3839 MiB，
WSL 的 7797 MiB 内存中最低有 3496 MiB 可用。API、Worker、评测及其子进程合计 PSS 的采样峰值为 1154 MiB。

已有教材切换时先停写入索引的 API 和 Worker，保留原配置，执行
`EMBEDDING_MODE=ollama $HOME/miniforge3/envs/eduagents/bin/python scripts/reembed_ollama.py`。
脚本分页重建独立集合，并复核新旧所有片段的 ID 与内容散列。核对成功后切换配置、重启服务。
原集合保留迁移时的内容，后续新增资料需在回切时重新索引。

以下两次运行分别重建独立临时索引。评测先检查教材文件名，再匹配标注片段或章节。
稠密向量取自 BGE-M3，关键词使用分词与 BM25 稀疏权重，排序使用 RRF 融合。
模型和融合方法见 [M3-Embedding](https://arxiv.org/abs/2402.03216) 和
[Qdrant Hybrid Queries](https://qdrant.tech/documentation/concepts/hybrid-queries/)。

~~~bash
py="$HOME/miniforge3/envs/eduagents/bin/python"
EMBEDDING_MODE=ollama "$py" eval/run_retrieval_eval.py --golden eval/golden_set/v2.jsonl --corpus-dir corpus --no-rerank --no-sparse --output /tmp/eduagents-dense.md
EMBEDDING_MODE=ollama "$py" eval/run_retrieval_eval.py --golden eval/golden_set/v2.jsonl --corpus-dir corpus --no-rerank --output /tmp/eduagents-hybrid.md
~~~

本机 RTX 5060 Laptop 实测，三本合成教材共 60 题，其中 54 题有答案。

| 检索方式 | 正确来源排第 1 | 正确来源进前 3 | MRR |
|---|---:|---:|---:|
| BGE-M3 | 41/54 | 51/54 | 0.8500 |
| BGE-M3 + 关键词 | 48/54 | 53/54 | 0.9290 |

剩余 6 题缺少教材依据。关闭精排时不给融合分数套用精排器的拒答阈值。引用是否支持回答
由问答 Agent 单独核验。区分召回质量与答案依据的方法可参见 [Ragas](https://arxiv.org/abs/2309.15217)。
两轮预热条件不同，本表只比较检索质量。

在临时索引上可直接运行真实教材问答。例如，给上述混合检索命令添加
`--qa-ollama-model qwen3:4b-instruct-2507-q4_K_M --qa-case-id q01 --qa-case-id q55`，
即可分别检查一道可答题与一道缺资料题。省略题号会覆盖整套题。模型从问答工具读取检索结果，
报告记录是否拒答、是否引用标注来源、有没有交付成功。核验服务失败单独记为失败。

## Harness 运行与旧会话迁移

教育任务由原生 DSH 运行，领域工具定义教学规则；单次评分/评审服务仍可直接使用网关。
`HARNESS_TIMEOUT_SECONDS` 默认 180 秒，`HARNESS_MAX_CONCURRENCY` 默认 8，
`HARNESS_MAX_MODEL_CALLS` 默认 16，`HARNESS_CONTEXT_WINDOW_TOKENS` 默认 32768。
本地 `.env.example` 将根会话并发设为 2、全局子 Agent 并发设为 1、后台 Worker 同时运行的作业设为 2，适合低内存 WSL。
自动压缩请求也计入模型调用上限。超时和取消回收子进程并保存可用原生检查点。
SDK 处于预览阶段，升级时同时验证 profile patch、原生日志、压缩及工具协议。

新协作计划使用 `EDUCATION_PROMPT_VERSION=edu-v2` 和 `AGENT_COLLABORATION_ENABLED=true`。
默认最多 8 个子 Agent、全局 3 个并发子任务、96 次共享逻辑调用、3 元估算费用和 2 次规划
返工。父子 Agent 的实际循环均由 DSH 执行；`edu-v1` 和已准入的单会话任务继续按原架构恢复。
协调任务的整体超时默认 360 秒（`AGENT_COLLABORATION_TIMEOUT_SECONDS`），单个子会话仍
使用 Harness 的 180 秒限制，避免多个角色串联时过早终止。
配置详见 `.env.example`。本地语义记忆可执行 `make install-memory` 后设置
`MEMORY_SEMANTIC_MODE=local`，首次启动下载中文模型到 `MEMORY_MODEL_CACHE_DIR`；嵌入
不可用时保持词法召回。auto 使用已配置 live 嵌入，disabled 关闭语义计算。

~~~bash
make migrate
~~~

`make migrate` 只应用 Alembic，包括 `0040` 原生任务、`0041` 记忆和 `0042` 协作者迁移。已有旧 LangGraph checkpoint
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
  --live --propose-version edu-v3-candidate --candidate-output /tmp/edu-v3-candidate.json \
  --repetitions 2 --max-calls 600 --max-cost-cny 10 \
  --output /tmp/eduagents-prompt-comparison.json
~~~

`--live` 的费用上限覆盖主 Agent、压缩、独立评审和候选反思；达到上限后停止新调用，
最后一个在途调用仍按实际费用计入。CLI 在固定合成资料上比较教学策略，不访问应用数据库。
已有候选可用 `--candidate /tmp/edu-v3-candidate.json` 代替 `--propose-version` 和 `--candidate-output`。
edu-v2 的进化候选必须保留四个协作角色策略；固定权限与程序检查不由模型修改。

架构对照借鉴协调者与工作 Agent、独立评审的设计，用同一组输入分别跑直接生成、单 Harness
和协作 Harness。默认选留出案例，可用 `--case-id` 指定案例。[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)

~~~bash
make eval-agents-smoke
$HOME/miniforge3/envs/eduagents/bin/python eval/run_agent_ablation.py \
  --live --case-id plan-holdout-prerequisite --case-id plan-holdout-observable \
  --repetitions 2 --max-calls 600 --max-cost-cny 10 \
  --output /tmp/eduagents-agent-ablation.json
~~~

直接生成不具有工具确认/恢复能力；比较的质量来自同一评审对实际成果的匿名评分。
报告另列契约检查、失败、调用和墙钟耗时。所有真实调用包括评审都消耗预算，报告不会写
入学习历史；开发桩仅覆盖协议，不能用于宣称协作质量提升。

比较架构时先核对报告的 `case_ids`、`expected_runs`、`recorded_runs` 和 `status`。
例如两题、三种架构、各跑一次应该有六条记录。只有完整运行才适合比较总成本。
`clean` 统计已完成且没有评审违规、程序契约错误或运行错误的结果。每份合格成果的
`generation_calls_per_clean` 和 `generation_wall_ms_per_clean` 将该架构所有已记录尝试
的生成开销除以合格数，包含失败尝试，单列评审开销；合格数为零时这两个值为 `null`。

本机生成模型也能走同一评测入口。以下命令预热模型后，串行跑直接生成、单 Harness 和协作
Harness。12 个计划约束案例各自声明了天数、可用时间、教材和验收标准，可按 ID 选择。
报告分别记录生成和评审的调用次数、费用与耗时，本机费用字段表示云供应商收费。

~~~bash
ollama pull qwen3:4b-instruct-2507-q4_K_M
"$HOME/miniforge3/envs/eduagents/bin/python" eval/run_agent_ablation.py \
  --ollama-model qwen3:4b-instruct-2507-q4_K_M --golden eval/golden_set/plan-constraints.jsonl \
  --case-id plan-simple-units --case-id plan-complex-short --repetitions 1 --max-calls 250 \
  --output /tmp/eduagents-plan-gpu.json
~~~

在 RTX 5060 Laptop 上用 `qwen3:4b-instruct-2507-q4_K_M` 对
`plan-simple-units` 和 `plan-complex-transfer` 各跑一轮，直接生成和单 Agent
都交付了 2/2 份草案，三角色协作交付了 0/2 份。三种架构均有 0/2 份通过全部质量检查。
这是两个开发用例的结果，该模型在这组题上的协作开销还没有带来合格交付。

模型预算按原预扣日期结算。Redis Lua 原子写入预算差额和待落账用量，Worker 每分钟重放。
PostgreSQL 通过用量 ID 去重，用量和钱包流水在同一事务提交。超过一小时仍缺少结果的
调用标为 `usage_unknown`，等待取得上游凭据，未确认费用保留估算。此恢复机制依赖 Redis 数据可用。

以下命令使用一次性 PostgreSQL、Redis 和可控模型响应复核跨工作流的故障。第一组从提交测验
走到确认学习计划，核对错答归属、改判后的失效和并发确认后的任务数。第二组模拟已输出部分内容的
模型流断开、API 账务事务提交失败，再由两个消费者重放同一条用量，检查数据库流水和 Redis 日预算。

~~~bash
make test-integration TEST=tests/integration/test_plan_reinforcement_evidence.py
make test-integration TEST='tests/integration/test_budget_e2e.py::test_interrupted_paid_stream_survives_failed_commit_and_concurrent_replay tests/integration/test_harness_recovery.py::test_parallel_api_runtimes_share_confirmation_lock_and_native_archive'
~~~

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

该命令运行 Web 单测、TypeScript 和 ESLint。Web 单测按文件串行运行，生产构建用
`make build-web` 单独执行。单测、集成、迁移测试、评测入口和构建通过 Linux/WSL 的 `flock`
共用当前工作区的锁，即使从不同终端运行也会排队。可用 `make test-unit TEST=路径` 或
`make test-integration TEST=路径` 只验证改动涉及的测试。测试使用开发替身，不加载本地记忆
嵌入模型，Harness 默认两条顶层并发、一个协作子任务并发。

直接修改 `apps/web` 前先读
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
