# 苏问 AI课堂 — 可验证的个性化学习平台

苏问 AI课堂面向学生的持续学习闭环，而不是只优化一次问答。平台把可溯源知识库、AI
辅导、真实作答、错题复习、掌握度与学习画像连接起来，并为教师和运营人员提供受权限
控制的专项界面。

当前项目处于上线准备阶段。学生主链路、题目资料、数据隔离和账户生命周期已经实现；
完整考试模式、班级任务、教师批量阅卷与成绩管理仍属于后续能力。准确状态见
[STATUS.md](STATUS.md)，详细文档从 [docs/README.md](docs/README.md) 进入。

## 核心能力

- 知识库：上传或导入教材、讲义和笔记，完成解析、分块、混合检索、引用定位和学习指南。
- AI 学习：支持可溯源问答、分级辅导、智能练习、学习计划和可解释的下一步建议。
- 题目资料：导入图片、PDF、Word、PPT、文本中的试卷、练习册、作业或题单，保存原件并
  进行多题识别和人工校对。
- 真实作答：以 `AssessmentAttempt` 保存一次考试或练习的真实答案、分数和核验状态。
- 错题闭环：人工选择或核验错误后进入错题本；只有已核验记录才进入 FSRS、掌握度、
  画像、报告和推荐。
- 数据治理：具备租户隔离、文件安全检查、原件权限校验、用户数据导出和账号注销清理。
- 专项角色界面：教师可查看和导出本人可访问的普通测验；管理员可使用运营、评估、故障
  注入、反馈、邀请码和用户额度管理界面。日常运营不应直接操作生产数据库。

### 三类内容不要混用

| 概念 | 保存什么 | 用于什么 | 不是什么 |
|---|---|---|---|
| 知识库 | 教材、讲义、笔记等参考内容及检索分块 | RAG、引用、讲解、知识点词表 | 不是错题本或考试记录 |
| 题目资料 | 试卷、练习册、作业、题单、原文件和结构化题目 | 拆题、校对、真实作答、选择错题 | 不会自动写入普通 RAG |
| 错题本 | 已确认错误或人工选择的 `ErrorItem` | FSRS 复习、变式练习、画像更新 | 不是上传文件目录 |

~~~mermaid
flowchart LR
    KB[知识库] --> QA[检索与讲解]
    R[题目资料] --> RQ[结构化题目 + 人工核验]
    RQ --> A[真实作答]
    RQ --> E[明确选择错题]
    A -->|核验为错误| E
    E --> L[FSRS / 掌握度 / 画像 / 报告]
~~~

待核验题和未最终核验的作答不会进入右侧学习状态。完整边界见
[系统架构](docs/architecture.md)。

## 角色与当前范围

| 角色 | 当前能力 | 当前边界 |
|---|---|---|
| 学生 | 知识库、问答、辅导、练习、题目资料、错题、复习、计划、画像与报告 | 主产品角色 |
| 教师 | 普通测验列表与导出 | 尚无班级任务、批量阅卷和成绩管理闭环 |
| 管理员 | 运营指标、评估、故障注入、反馈和账户管理端点 | 通过受控 UI/API 工作，不把数据库直连作为产品能力 |

机构目前用于共享知识库；机构级题目资料共享仍未实现。

## 架构概览

~~~mermaid
flowchart TB
    WEB[Next.js Web] --> API[FastAPI API]
    API --> PG[(PostgreSQL + RLS)]
    API --> REDIS[(Redis)]
    API --> Q[(Qdrant)]
    API --> OBJ[(对象存储)]
    API --> GW[ModelGateway]
    API --> WORKER[ARQ Worker]
    WORKER --> PG
    WORKER --> Q
    WORKER --> OBJ
    GW --> LLM[模型 / 嵌入 / 重排服务]
~~~

领域核心位于 `packages/core`，API、Web 和异步任务分别位于 `apps/api`、`apps/web`、
`apps/worker`。详细组件、数据流和信任边界见 [docs/architecture.md](docs/architecture.md)。

## 本地快速启动

要求 Python 3.12、Node.js 20+、npm，以及可用的 PostgreSQL、Redis 和 Qdrant。仓库
`Makefile` 在当前开发机有默认 Python 路径；其他机器可通过
`make PY=/path/to/python PIP=/path/to/pip <target>` 覆盖。

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

分别启动三个进程：

~~~bash
make api
make worker
(cd apps/web && npm run dev)
~~~

访问地址：

- Web：<http://127.0.0.1:3000>
- API 文档：<http://127.0.0.1:8000/api/docs>
- 就绪检查：<http://127.0.0.1:8000/api/health/ready>

`make seed` 创建的账号仅供本地演示环境：

- 学生：`demo@eduagents.cn` / `demo-pass-123`
- 管理员：`admin@eduagents.cn` / `admin-pass-123`

不要在生产环境运行演示 seed，也不要沿用示例密钥。完整环境、网关模式和排障说明见
[开发手册](docs/development.md)；容器化生产路径见
[部署手册](deploy/README-deploy.md)。

## 常用验证

~~~bash
make docs-check
make lint
make test-unit
make test-integration
python -m compileall -q packages apps tests alembic
(cd apps/web && npm run lint && npm run build)
~~~

集成测试需要 PostgreSQL 和 Redis。测试数量会随实现增长，因此文档只维护可复现命令，
不维护容易过期的固定总数。

## 模型网关模式

| 模式 | 行为 | 使用场景 |
|---|---|---|
| `stub` | 确定性开发桩，明确标识非真实模型输出 | 本地开发、CI |
| `live` | 调用真实模型，启用重试、熔断、降级和计量 | 受控演示、生产 |
| `record` | 在 live 调用基础上录制磁带 | 仅限明确隔离的临时工作区，不写入仓库 |
| `replay` | 回放已有磁带；当前仓库不保留磁带 | 无磁带时必须明确失败或使用标识为 stub 的模式 |

生产环境必须使用 `APP_ENV=prod`，并按部署手册配置真实密钥、对象存储、备份和 TLS。
历史运行数据、演示账号、录制磁带、截图和评测输出已在上线前清除；新的生产验收证据必须针对明确日期和环境重新生成。

### 注册模式与 Resend SMTP

注册模式由唯一配置 `REGISTRATION_MODE` 控制：`closed` 关闭公开注册，`invite` 要求邮箱验证码
和管理员发出的独立邀请码，`open` 只要求邮箱验证码。切换模式不会删除历史邀请码记录。
图形验证码用于防机器人，邮箱验证码用于证明邮箱所有权；普通登录不要求每次输入邮箱验证码。
公开入口只创建 `student`，教师和管理员必须通过受控流程创建。生产使用
已验证域名的 Resend SMTP：主机 `smtp.resend.com`、用户 `resend`、API Key 作为
`SMTP_PASSWORD`，发件地址使用 `noreply@mail.adfgt.xyz` 或同一 Verified 域名下的地址，
并配置 `SMTP_REPLY_TO`。真实 Key 只存在服务器 Secrets 或未提交的 `.env`，不要写入代码、
测试、日志或文档；建议同时配置 SPF、DKIM 和 DMARC。`invite`/`open` 模式缺少 SMTP 时 API 拒绝启动。

## 文档导航

- [文档总入口](docs/README.md)
- [当前发布状态](STATUS.md)
- [系统架构与产品边界](docs/architecture.md)
- [开发与测试规范](docs/development.md)
- [API 业务契约](docs/api-contract.md)
- [数据模型](docs/data-model.md)
- [可执行学习计划](docs/learning-plan.md)
- [架构决策记录](docs/decisions.md)
- [安全与合规](docs/safety.md)
- [生产部署](deploy/README-deploy.md)

文档与代码冲突时，以当前代码、迁移和自动化测试为最终事实，并在同一变更中修正文档。
文档维护规则见 [docs/README.md](docs/README.md#维护规则)。

## 上线边界

上线前应完成生产配置审计、真实备份恢复演练、TLS、监控告警、容量基线和小流量灰度。
当前产品适合受控内测，不应把尚未完成的教师班级管理或完整考试能力对外宣称为已交付。
