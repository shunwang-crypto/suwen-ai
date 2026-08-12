# 系统架构与产品边界

> 状态：Active
> 目标读者：产品、开发和测试
> 最终事实：当前代码、Alembic migration 和自动化测试。

## 产品定位

苏问 AI课堂是以学生长期状态为中心的学习平台。知识库负责可信资料，AI 工作流负责问答、
练习、计划和辅导，真实作答与错题为学习状态提供证据，FSRS 与掌握度模型决定后续复习、
画像和建议。

系统不是通用聊天壳，也不是已经完成的教务系统。当前主产品角色是学生；教师和管理员
已有受限专项界面，但班级任务、教师批量阅卷和成绩管理仍未形成完整闭环。

## 角色与权限

### 学生

拥有个人知识库、对话、测验、题目资料、真实作答、错题、复习、计划、画像、报告、偏好
和账户数据。个人资源通过用户/租户条件和 PostgreSQL RLS 隔离。

### 教师

当前教师接口用于列出和导出本人可访问的普通测验。导入错题产生的历史隐藏 Quiz 和题目
资料中的 `Question` 不会混入教师组卷列表。班级、任务下发、批量阅卷和成绩管理是后续
产品阶段。

### 管理员与机构管理员

平台管理员通过 `/admin`、`/evals` 及受保护 API 查看运营、成本、延迟、评估、故障注入、
反馈和账户状态。机构管理员管理机构成员与共享知识库。

平台角色 `admin` 与机构成员角色 `admin/member` 是两套不同权限，不能混用。

## 核心概念

### 知识库（KnowledgeBase）

知识库保存教材、讲义、笔记和网页等参考内容。`Document` 经 Worker 解析为 `Chunk`，再写入
Qdrant；问答、引用定位、学习指南和知识点词表使用这条链路。个人库由用户拥有，机构库由
机构拥有并向成员提供只读访问，机构管理员可写。

### 题目资料（LearningResource）

题目资料是试卷、练习册、作业、题单或单题的来源容器，保存原文件、元数据、识别状态和
`ResourceQuestion`。它的默认检索策略是 `questions_only`，不会自动创建普通知识库的
`Document/Chunk`，学生答案也不会进入 RAG。

题目资料可以关联一个可见知识库，用该库受控词表解析题目 `kc_ids`。在尚无学习证据时，
更换关联会事务性重映射；产生错题或作答证据后会锁定，避免改写历史学习语义。

### 结构化题目（ResourceQuestion + Question）

`ResourceQuestion` 保存题号、章节、页码、原文件区域、分值、置信度和核验状态；`Question`
保存题干、题型、选项、答案、解析和知识点。资源题的 `Question.quiz_id` 为空，不属于普通
AI 测验。

AI 识别结果必须由用户校对。`pending` 题不进入真实作答、FSRS、掌握度、画像、报告或推荐；
用户可以核验、拒绝或明确选择加入错题本。

### 真实作答（AssessmentAttempt）

一次真实考试、作业或练习记录使用：

~~~text
AssessmentAttempt -> AssessmentAnswer -> ResourceQuestion -> Question
~~~

状态只能 `draft -> submitted -> graded`。草稿允许整体替换或部分更新答案；提交后学生响应
锁定；最终核验才会写入掌握度与错题。客观题由服务端判分，主观题最终核验必须明确正确性。

### 错题（ErrorItem）

`ErrorItem` 是派生学习记录，不是文件分类。它可以来自普通测验判错、题目资料中人工明确
选择，或真实作答最终核验为错误。`pending` 错题不进入复习；`verified` 后才进入 FSRS、
变式练习和学习状态。

## 学习闭环

~~~mermaid
flowchart LR
    DOC[教材 / 讲义 / 笔记] --> KB[KnowledgeBase]
    KB --> RET[检索 / 引用 / 辅导]
    SRC[试卷 / 练习册 / 作业 / 题单] --> RES[LearningResource]
    RES --> RQ[ResourceQuestion]
    RQ --> REVIEW[人工校对与核验]
    REVIEW --> ASSESS[AssessmentAttempt]
    REVIEW --> ERROR[ErrorItem]
    ASSESS -->|最终核验为错误| ERROR
    ASSESS -->|最终核验| MASTERY[MasteryRecord + MasteryLog]
    ERROR --> FSRS[FSRS 复习]
    FSRS --> MASTERY
    MASTERY --> PROFILE[画像 / 报告 / 推荐 / 计划]
~~~

## 运行组件

| 组件 | 职责 | 当前边界 |
|---|---|---|
| Next.js Web | 学生、教师、管理员界面和同源 API 访问 | 不保存权威业务状态 |
| FastAPI API | 鉴权、邮箱所有权验证、权限、状态机、SSE、事务和业务契约 | 同步请求不承担长耗时摄取；邮件统一经 EmailSender |
| ARQ Worker | 文档摄取、题目识别、导出、清理、报告等异步任务 | 本地开发时与 API 分进程启动 |
| PostgreSQL | 关系数据、状态机、审计、RLS、多租户 | migration 使用独立高权限连接 |
| Redis | 限流、预算、缓存、任务队列、虚拟时钟 | 不是长期事实存储 |
| Qdrant | 文档向量与答案缓存向量 | 查询必须携带租户/知识库过滤 |
| 对象存储 | 知识库原件、题目资料、错题来源、聊天附件、导出包 | 下载统一经过授权 API |
| ModelGateway | live/record/replay/stub、重试、熔断、降级与计量 | stub 结果不得当作真实质量证据 |

本地开发使用 `make dev-infra` 启动 PostgreSQL、Redis 和 Qdrant；API、Worker 和 Web 分别
在宿主机进程中运行。对象存储可按 `.env` 使用 MinIO 或本地实现。

## 数据与信任边界

- 应用数据库连接使用 `edu_app`（`NOSUPERUSER NOBYPASSRLS`），migration 使用独立连接。
- 事务设置个人租户、机构列表和管理员 GUC；服务层权限与 RLS 形成双重防线。
- 不可见的个人资源通常返回 404，避免泄露资源是否存在。
- 文件上传校验扩展名、大小和签名；文件名规范化；响应带 `nosniff`。
- 删除数据库事实与删除外部对象通过 pending cleanup/outbox 协调，失败可重试。
- 用户导出包含可读取原件，缺失对象记录在 manifest 中而不伪造成功。

## 当前产品边界

| 能力 | 状态 | 说明 |
|---|---|---|
| 学生知识库与学习闭环 | 已实现 | 当前界面仍在收敛信息架构 |
| 多题题目资料和真实作答 | 已实现 | 由学生本人录入和最终核验 |
| 个人/机构共享知识库 | 已实现 | 机构题目资料共享未实现 |
| 教师普通测验导出 | 已实现 | 不是班级成绩管理 |
| 管理员运营和评估界面 | 已实现 | 不属于学生主导航 |
| 完整考试模式 | 计划中 | 缺计时、防作弊、监考、统一交卷等 |
| 班级任务、批量阅卷、成绩管理 | 计划中 | 需要班级与教师工作流数据模型 |

接口和实体细节分别见 [API 契约](api-contract.md)与[数据模型](data-model.md)。
