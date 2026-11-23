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

掌握度只能由 `apply_mastery_evidence` 边界写入。当前有效证据仅包括真实 Quiz
作答、到期 FSRS 复习、最终核验的 AssessmentAnswer 及其申诉纠正。阅读资料、
普通对话、AI 解释、Explain-back 反讲反馈、辅导小结、计划任务完成和未核验题目都不更新
BKT。Explain-back 只写 observation-only 反馈记录；对话反思和辅导小结只写
`LearnerInsight`，供用户查看和删除。

`evidence_state` 由有效 `MasteryLog` 的独立 `evidence_id` 数量派生，不从
`p_mastery` 先验或历史 `confidence` 反推：0 次为 `unobserved`，1–2 次为
`estimated`，3 次及以上为 `observed`。只有 `observed` 才能进入稳定“薄弱”或
“已掌握”叙述、补强推荐和报告归因；其余状态优先收集证据。

## 运行组件

| 组件 | 职责 | 当前边界 |
|---|---|---|
| Next.js Web | 学生、教师、管理员界面和同源 API 访问 | 不保存权威业务状态 |
| FastAPI API | 鉴权、邮箱所有权验证、权限、状态机、SSE、事务和业务契约 | 同步请求不承担长耗时摄取；邮件统一经 EmailSender |
| ARQ Worker | 文档摄取、题目识别、导出、清理、报告等异步任务 | 本地开发时与 API 分进程启动；知识增强 claim 同时保存 `enrich_job_id` 与 `Document.version`，旧版本任务不能回写新分块/KC或提交终态 |
| PostgreSQL | 关系数据、状态机、审计、RLS、多租户 | migration 使用独立高权限连接 |
| Redis | 限流、预算、缓存、任务队列、虚拟时钟 | 不是长期事实存储 |
| Qdrant | 文档向量与答案缓存向量 | 查询必须携带租户/知识库过滤 |
| 对象存储 | 知识库原件、题目资料、错题来源、聊天附件、导出包 | 下载统一经过授权 API |
| ModelGateway | live/record/replay/stub、重试、熔断、降级与计量 | stub 结果不得当作真实质量证据 |

本地开发使用 `make dev-infra` 启动 PostgreSQL、Redis 和 Qdrant；API、Worker 和 Web 分别
在宿主机进程中运行。对象存储可按 `.env` 使用 MinIO 或本地实现。

深度研究问答在 LangGraph 的单个 `research` 节点内运行独立的轻量工具循环：模型根据已有
网页资料选择搜索或结束，宿主限制为最多 3 轮、5 次查询、每轮 2 条查询和 14 个来源。
所有模型调用仍经 ModelGateway，动作 JSON 校验、查询去重、外部内容审核与过滤由宿主执行。
规划失败时无来源只尝试原问题一次，有来源则结束并保留；后续搜索失败保留已有来源，预算
熔断和取消继续向上传递。搜索进度实时走 SSE，最终回答核验资料支持，最多重生成一次。
核验不可用时明确标记未完成，重生成后仍缺依据时附未支持提示。普通网页问答和知识库
fast/think 路径沿用现有实现，计划与出题继续使用持久化确认恢复。

## 前端信息架构

学生核心工作流固定为今日 `/today`、课程 `/courses`、练习 `/practice`、计划 `/plans` 和
成长 `/growth`。桌面侧栏的 `PRIMARY_NAV_ITEMS` 另保留对话 `/chat` 作为随时可用的上下文
学习工具；学习计划聚合页从账户菜单、今日和课程工作区进入。机构 `/orgs` 按登录后一次
`/api/orgs` 结果条件显示，请求失败时 fail-open 保持显示。

高亮由显式别名表 `NAV_HIGHLIGHT_ALIASES` 决定（`/tutor`→`/chat`、`/quiz`/`/reviews`→`/practice`、
`/materials`→`/courses`），不使用通配前缀推断，避免一个新路由静默改变高亮。

课程工作区 `/courses/[kbId]` 就是知识库本身，四个 tab：资料、指南、知识图、计划（`?tab=`）。
「知识图」是课程级 Learning Map（full 模式）：该库全部知识点与先修边，按章展示，跨章直接前置
一并带出并标注；节点只显示证据档与定性档位词，掌握度数值不出境。库级图没有「计划任务」这个
锚点，所以分层在前端按给定边的最长路径算 —— 那是布局决策，不新增任何依赖关系；先修关系成环时
残留节点排在末列并如实提示，不静默丢弃。

学习计划详情里的「知识点学习路径」有两个等价视图：默认的**依赖图**把后端 `kc_dependency`
的拓扑层画成列、把后端给出的先修边画成连线（前端只做布局，不推断依赖关系，也只画两端
节点都在渲染集合里的边）；「列表」视图逐层列出同样的节点，窄屏与逐条阅读时更好用。
计划图只覆盖当前计划的目标 KC 及其递归先修闭包。全库知识图由
`GET /api/kb/{id}/kc-graph` 提供，并在课程工作区「知识图」tab 按章节聚合展示；两者复用真实
先修边，但计划图不会为了补全课程视图而扩大任务范围。

专注会话 `/practice/session?mode=review|quiz&from=<站内路径>` 全屏无侧栏，`from` 经
`safeInternalPath` 校验，站外地址回落到站内默认返回目标。

以下历史路由当前仅作为迁移期重定向存在，不是产品入口；新代码、文档和测试不得继续生成
这些链接：

| 旧路由 | 目标 |
|---|---|
| `/quiz` | `/practice?tab=quiz`（8 类深链参数转发） |
| `/reviews` | `/practice?tab=review`；`?view=errors` → `?tab=errors` |
| `/materials` | `/courses`（按 `kb` / `view=unclassified` / `resource_id` 查库 / 默认四分支） |
| `/tutor` | `/chat?tutor=1&…`，首轮后自动清标记 |
| `/plans?id=` | 页内解析后 → `/courses/{kb_id}?tab=plan` |

重定向是客户端 `router.replace` 而非 `next.config` redirects：当 destination 自带 query 时
Next 不转发 incoming query，深链参数会丢失；需要查库才能确定目标的重定向也只能在页内做。

## 数据与信任边界

- 应用数据库连接使用 `edu_app`（`NOSUPERUSER NOBYPASSRLS`），migration 使用独立连接。
- 事务设置个人租户、机构列表和管理员 GUC；服务层权限与 RLS 形成双重防线。
- 不可见的个人资源通常返回 404，避免泄露资源是否存在。
- 文件上传校验扩展名、大小和签名；文件名规范化；响应带 `nosniff`。
- 删除数据库事实与删除外部对象通过 pending cleanup/outbox 协调，失败可重试。
- 文档摄取/知识增强的 PostgreSQL 写入受文档版本栅栏保护。重建索引会在行锁内递增
  `Document.version` 并清除增强 claim；删除直接删除事实并通过 cleanup 清理向量。增强刷新
  保留旧的 `enriched` 内容直到新一轮向量写入成功，失败只释放本轮 claim，不把旧内容标成不可用。
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
