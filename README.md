# 苏问 AI课堂

苏问 AI课堂是围绕用户自有学习资料构建的个性化学习项目。用户上传教材、讲义和笔记建立
知识库，上传试卷、练习册和作业建立题目资料；AI 负责检索讲解、分级辅导、练习和计划，
不替代资料来源，也不把阅读或聊天自动当作“已经掌握”。

当前只维护本地开发体验。Git 历史负责保存旧方案，不在仓库中保留生产部署、竞赛材料、
历史提示词或兼容性文档。

## 产品主线

~~~text
用户资料
  ├─ 教材 / 讲义 / 笔记 → 知识库 → 检索、引用、讲解
  └─ 试卷 / 作业 / 题单 → 题目资料 → 校对、真实作答、错题

可验证作答 → 掌握度 / FSRS → 复习、画像、报告、下一步建议
~~~

三类内容必须保持分离：

| 概念 | 内容 | 用途 |
|---|---|---|
| 知识库 | 教材、讲义、笔记、网页及检索分块 | RAG、引用、讲解、知识点词表 |
| 题目资料 | 试卷、练习册、作业、题单和结构化题目 | 拆题、校对、真实作答 |
| 错题本 | 已确认错误或用户明确选择的题目 | FSRS 复习、变式练习、学习状态 |

AI 识别结果、待核验题目、阅读行为和普通聊天不能直接更新掌握度。掌握度只接受代码中
明确允许的学习证据，且必须经过统一写入口和幂等保护。

## 代码结构

- `apps/web`：Next.js 前端。
- `apps/api`：FastAPI API、业务状态机和权限边界。
- `apps/worker`：文档摄取、识别、清理、报告等异步任务。
- `packages/core`：模型网关、检索、解析、学习模型和 LangGraph 工作流。
- `alembic/versions`：数据库迁移。
- `prompts`：运行时真正使用的模型提示词。
- `tests`：单元与集成测试。

更详细的边界见[系统架构](docs/architecture.md)、[API 契约](docs/api-contract.md)和
[数据模型](docs/data-model.md)。

## 本地启动

本机固定使用 Conda 环境：

~~~text
/home/shunw/miniforge3/envs/eduagents
~~~

即使当前 shell 显示为 `base`，也不要裸跑 `python`、`pip`、`pytest` 或 `alembic`。
Makefile 始终调用上述环境中的 Python 3.12，并在执行前校验；使用系统 Python 或 base
环境会直接失败。

首次初始化：

~~~bash
cd /home/shunw/eduagents
[ -f .env ] || cp .env.example .env
make env-check
make install
(cd apps/web && npm ci)
make dev-infra
make migrate
make seed
~~~

分别启动 API、Worker 和 Web：

~~~bash
make api
make worker
cd apps/web && npm run dev
~~~

- Web：<http://127.0.0.1:3000>
- API 文档：<http://127.0.0.1:8000/api/docs>
- API 就绪检查：<http://127.0.0.1:8000/api/health/ready>

`make seed` 仅创建本地测试数据：

- 学生：`demo@eduagents.cn` / `demo-pass-123`
- 管理员：`admin@eduagents.cn` / `admin-pass-123`

完整环境变量、测试和排障方式见[本地开发手册](docs/development.md)。

## 验证

~~~bash
make docs-check
make lint
make compile
make test-unit
make test-integration
make migration-check
make test-web
~~~

测试数量会随实现变化，因此文档只维护命令，不维护容易过期的固定数字。所有生成结果、
截图和临时证据放在仓库外，不新增说明性 Markdown 或一次性产物。

## 文档

仓库只维护四份项目文档：

- [系统架构与产品边界](docs/architecture.md)
- [API 业务契约](docs/api-contract.md)
- [数据模型与生命周期](docs/data-model.md)
- [本地开发与测试](docs/development.md)

文档与实现冲突时，以当前代码、迁移和自动化测试为准，并在同一变更中修正文档。
