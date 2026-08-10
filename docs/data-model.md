# 数据模型与生命周期

> 状态：Active
> 目标读者：后端开发、数据迁移、安全审查和测试人员
> 最终定义：`apps/api/src/eduagents_api/models.py` 与 `alembic/versions/`。

本文描述稳定业务语义，不逐字段复制 ORM。任何 schema 变化都必须新增 migration、迁移测试
并同步本文；已经提交的 migration 不得修改。

## 所有权与租户语义

个人数据通常以用户 ID 作为 `tenant_id`。机构共享知识库采用泛化 owner 语义：

- `knowledge_bases.owner_type=user`：`tenant_id` 是用户 ID。
- `knowledge_bases.owner_type=org`：`tenant_id` 是 `orgs.id`。

`documents`、`chunks` 和向量 payload 继承知识库 owner。历史 `organizations` 和
`users.org_id` 是早期占位；当前机构模型使用 `orgs + org_members`。

题目资料当前是个人资源：

- `learning_resources.owner_type=personal`；
- `owner_id/created_by` 指向用户；
- schema 保留 `organization` 枚举用于未来演进，但机构题目资料共享尚未实现。

知识库和题目资料的 `owner_type` 枚举不同，不能混用。

## PostgreSQL RLS

受租户保护的表启用并强制 RLS。应用事务通过 `tenant_session` 设置：

- `app.tenant_id`：当前个人/owner；
- `app.org_ids`：当前用户所属机构；
- `app.is_admin`：受控跨租户运营或机构管理路径。

GUC 未设置时策略默认拒绝。应用连接使用 `edu_app`（`NOSUPERUSER NOBYPASSRLS`）；migration
使用独立高权限连接。服务层仍需显式校验所有权，RLS 是纵深防御而不是替代。

机构库读取允许 owner 或机构成员；写入需要机构管理员，服务层校验后使用正确 owner 事务。
`usage_log` 的管理员聚合是受限例外。

## 核心关系

~~~mermaid
erDiagram
    USER ||--o{ KNOWLEDGE_BASE : owns
    ORG ||--o{ ORG_MEMBER : has
    ORG ||--o{ KNOWLEDGE_BASE : owns
    KNOWLEDGE_BASE ||--o{ DOCUMENT : contains
    DOCUMENT ||--o{ CHUNK : splits
    KNOWLEDGE_BASE ||--o{ KNOWLEDGE_COMPONENT : defines

    USER ||--o{ LEARNING_RESOURCE : owns
    LEARNING_RESOURCE ||--o{ RESOURCE_QUESTION : contains
    RESOURCE_QUESTION }o--|| QUESTION : structures
    LEARNING_RESOURCE ||--o{ ASSESSMENT_ATTEMPT : sources
    ASSESSMENT_ATTEMPT ||--o{ ASSESSMENT_ANSWER : contains
    RESOURCE_QUESTION ||--o{ ASSESSMENT_ANSWER : answers
    ASSESSMENT_ANSWER ||--o| ERROR_ITEM : may_create
    RESOURCE_QUESTION ||--o{ ERROR_ITEM : may_select

    USER ||--o{ MASTERY_RECORD : has
    USER ||--o{ MASTERY_LOG : audits
    USER ||--o{ QUIZ : owns
    QUIZ ||--o{ QUESTION : contains
    QUESTION ||--o{ ATTEMPT : receives
    ATTEMPT ||--o| METACOGNITION_PREDICTION : may_have
    USER ||--o{ METACOGNITION_PREDICTION : owns
    USER ||--o{ EXPLAIN_BACK : writes
    QUESTION ||--o{ ERROR_ITEM : produces
    ERROR_ITEM ||--o{ FSRS_REVIEW_EVENT : reviewed
    USER ||--o{ FSRS_REVIEW_EVENT : owns
    USER ||--o{ LEARNING_EVENT : owns
    QUESTION ||--o{ LEARNING_EVENT : snapshots
~~~

## 账户与组织

| 实体 | 语义 |
|---|---|
| `User` | 规范化邮箱、密码哈希、`email_verified_at`、平台角色、状态、未成年相关字段和套餐；学生/教师/管理员角色在此 |
| `RefreshToken` | 哈希存储、轮换和撤销链；重放会撤销相关 token |
| `PasswordResetToken` | 找回密码的一次性、限时凭据 |
| `Org` | 机构、邀请码、创建者和存储配额 |
| `OrgMember` | 用户与机构关系，角色为机构 `admin/member` |
| `WalletAccount` | 每个用户一行的苏问 AI 额度余额，服务层事务更新 |
| `WalletLedger` | 追加式额度流水；`signup_grant`、消费和充值等变动均保留审计记录 |

平台管理员和机构管理员是不同权限域。

管理员额度调整使用 `WalletLedger.kind='admin_adjust'`，业务凭证写入 `ref`。0015 migration
增加 `(user_id, ref)` 条件唯一索引，仅覆盖 `admin_adjust` 且非空的凭证；调额时锁定账户行，
在同一事务内写流水并更新余额，同一用户同一凭证的重试不会重复入账。

0013 为 `users.email_verified_at` 增加邮箱所有权事实，并在迁移时先检查
`lower(btrim(email))` 的历史碰撞；碰撞会使迁移明确失败，不覆盖或删除账号。无碰撞时历史
用户以 `created_at` 回填为兼容意义上的历史已验证，新注册用户只在邮箱验证码消费成功的
注册事务中写入。数据库同时建立 `lower(email)` 唯一索引；应用层规范化用于查询、登录和限流。

## 知识库与检索

| 实体 | 语义 |
|---|---|
| `KnowledgeBase` | 个人/机构 owner、名称、科目、版本、学习指南配置 |
| `Document` | 上传文件或网页来源、状态、哈希、大小、解析后端、上传者和 0019 有界逐页识别元数据 |
| `Chunk` | 父子分块、页码、标题路径、文本、上下文前缀、知识点和嵌入状态 |
| `KnowledgeComponent` | 知识库受控知识点词表和别名 |
| `KcEdge` | 知识点先修 DAG；应用层拒绝成环 |

文档状态由上传进入文本提取、可选扫描页识别、分块、索引、增强或失败；`partial_ready`
表示至少一页成功且部分页明确失败。`recognition_meta_json` 只保存逐页方法、状态、计数和
有界错误码，不保存全文、图片/Base64 或模型原始响应。Qdrant 只存可重建的向量投影，关系库
是文档元数据和状态的权威来源。

`KnowledgeComponent` 的 `source_kind` 区分真实语义增强的 `enrichment` 与 stub 摄取建立的
`chapter_fallback`；章节 fallback 使用 KB 内稳定 key 和确定性 UUID，重跑不会生成重复身份。
`superseded_by_id` 只表达有界的 KC 映射关系，采用 `ON DELETE SET NULL`，不删除依赖它的历史题目、
掌握度或学习事件。`stable_key` 仅在非空时受 `(kb_id, stable_key)` 条件唯一索引约束。

stub 文档在 `ready` 前完成章节 KC 与 child chunk 回写；live/record/replay 在语义 KC 覆盖并完成
前保持 enrichment barrier，不能把 fallback 或空词表当作 `enriched`。面向画像的 Quiz 只有在
每道题的 KC 快照通过当前 tenant/KB 校验后才能写入 Attempt、LearningEvent、MasteryRecord 和
MasteryLog；作答链路复用同一批已归一化 KC ID。

## 普通测验与学习状态

| 实体 | 语义 |
|---|---|
| `Quiz` | AI 练习蓝图、所属用户/知识库和生成状态 |
| `Question` | 题干、题型、答案、解析、难度、知识点；可属于 Quiz，也可由 ResourceQuestion 引用 |
| `Attempt` | 普通测验中针对单题的真实响应和判分结果 |
| `MetacognitionPrediction` | 作答前可选的 1–5 级信心预测，绑定真实 Attempt；只用于确定性校准统计 |
| `ExplainBack` | 学生反讲原文、可选 question/Attempt 来源、rubric 版本、结构化质量分和降级状态；质量分默认 shadow，不作为掌握度或错题证据 |
| `ErrorItem` | 错题来源、错因、状态、核验状态、FSRS card、资源/作答关联；Quiz 错题可保存唯一 Attempt/信心快照 |
| `MasteryRecord` | 用户与知识点的当前 BKT 状态和置信度 |
| `MasteryLog` | 每次掌握度变化的旧值、新值、证据类型、证据 ID 和权重 |
| `FsrsReviewEvent` | 一次已核验复习的 py-fsrs ReviewLog、card 前后快照、评分和参数版本；以 ErrorItem UUID 作为业务卡身份 |
| `LearningEvent` | quiz/review/assessment 统一学习证据；保存题目 KC/难度快照和业务发生时间 |
| `FsrsParams` | 用户级、版本化的 21 参数 FSRS-6 拟合产物；默认 `shadow`，只有显式门槛通过后才可激活 |

`Question.quiz_id` 可空。资源题不创建隐藏 Quiz，也不能进入普通测验或教师组卷列表。

`MetacognitionPrediction` 与对应 `Attempt/Question/User` 同事务写入；1–5 映射为
`0.1/0.3/0.5/0.7/0.9`，Brier 误差和高信心答错率按查询窗口实时计算，不保存易过期聚合。
预测与 Explain-back 都是 observation-only 数据，不调用 BKT/FSRS，不写 `MasteryRecord`、
`MasteryLog`、`ErrorItem`、`LearnerInsight` 或报告。Explain-back 模型/JSON 失败时仍保存学生
原文和 `degraded=true` 的可重试反馈；额度异常继续使用统一 402/429 契约，不伪装成降级成功。

新复习在同一事务更新 `ErrorItem.fsrs_card_json`、写入 `fsrs_review_events` 和
`learning_events`；`(user_id, idempotency_key)` 防止相同复习请求二次推进。新 quiz Attempt
和最终 `verified` 的 assessment answer 各写一个统一事件。pending/未核验记录不写事件；历史
review 不从当前 card 猜测回填。两张事件表启用 FORCE RLS、用户删除级联、导出清单，并按
730 天事件留存任务清理；迁移 0022 不做历史回填。

迁移 0024 为 ErrorItem 增加可空的 Attempt/信心快照字段、检查约束和唯一
`source_attempt_id` 关联；没有可证明来源的历史行保持 NULL。迁移 0025 建立 `FsrsParams`，保存
21 个 FSRS-6 参数、拟合指标和 shadow/active/retired 状态；非 active 产物按 730 天留存规则清理。
迁移 0027 为 ExplainBack 增加可选来源、`rubric_version`、`score_json` 和 `quality_shadow`；
这些质量分仍是观察/shadow 数据，默认不展示数值，也不写 BKT/FSRS。

## 题目资料与真实作答

### LearningResource

保存题目来源的所有权、类型、标题、科目、年级、学期、原文件名、MIME、大小、对象 key、
哈希、页数、状态、检索策略和可选 `linked_kb_id`。

新建题目资料类型为 `exam_paper`、`mock_paper`、`workbook`、`worksheet`、`homework`、
`single_question` 或 `other`。`textbook/lecture/notes` 仅兼容历史参考资料，不进入题目识别。

### ResourceQuestion

连接资源与 `Question`，保存 ordinal、题号、章节、页码、原图区域、分值、兼容的整题识别
置信度和逐字段 `field_confidence_json`，以及 `pending/verified/rejected`。逐字段对象只允许
白名单字段，记录有界的 `confidence/source/warnings`；服务端依据关键字段和结构字段阈值计算
是否可确认。核验状态和学习证据会约束后续编辑。

### AssessmentAttempt 与 AssessmentAnswer

`AssessmentAttempt` 保存一次考试/作业/练习的学生、类型、日期、时长、总分和状态。状态只
能 `draft -> submitted -> graded`。`AssessmentAnswer` 保存资源题响应、分数、正确性、评分
状态和评语。

最终核验前不会产生掌握度或错题证据。核验后的错误答案通过
`AssessmentAnswer -> ErrorItem` 一对零或一关联保证幂等。

### ErrorImport

兼容错题录入草稿、原文件和识别任务。新导入题最终挂在 `LearningResource/ResourceQuestion`
上；旧隐藏 Quiz 由 0012 迁移转换，不再作为当前实现模式。

## 计划、会话、画像和运营

| 实体 | 语义 |
|---|---|
| `Plan / PlanResource / PlanAdjustment` | 学习计划结构、稳定关联的知识库/网页资源和每次调整审计；`structure_json` 保留确认时的 `goal_analysis` 与定性 `skill_gaps` 快照；`Plan.kb_id` 可空，删除知识库后 SET NULL |
| `ChatSession / ChatMessage` | 可查询的会话展示历史 |
| `ChatAttachment` | 当前对话附件、提取文本和对象引用 |
| `LearnerInsight` | AI 提炼、用户可见可删的长期洞察 |
| `UserPreference` | 用户学习偏好和个性化开关 |
| `LearningReportSnapshot` | 报告快照与刷新状态 |
| `UsageLog` | 模型/功能/用户维度 token、成本、延迟和模式 |
| `UserModelCredential` | 0017 用户 DeepSeek 凭据的 AES-256-GCM 密文、nonce、版本和脱敏配置；不保存明文 |
| `TutorProblemDraft` | 0018 分级辅导的选页、待校对字段、确认快照、资源/题目安全 ID 与绑定 thread；不保存文件二进制/Base64/API Key |
| `MetacognitionPrediction / ExplainBack` | 0020 基础观察 + 0027 来源/rubric/质量 shadow 字段；730 天留存，随用户/Attempt 级联清理并进入数据导出 |
| `EvalRun` | 评估配置、语料/黄金集版本和指标 |
| `WalletAccount / WalletLedger / PaymentOrder` | 钱包、不可变流水和支付订单骨架 |

## 文件与外部状态

`user_model_credentials`（0017）按 `(user_id, provider)` 唯一。服务端环境变量提供 32 字节 AES-256-GCM 主密钥，AAD 绑定 `user_id/provider/key_version`；数据库仅保存密文、12 字节 nonce、版本、后四位和模型选择。用户删除时凭据按 FK CASCADE 删除；管理员只能通过安全聚合 API 看到 provider、启用状态和后四位。`UsageLog` 同时记录 `credential_source`、`provider` 和 `platform_billed_cost_cny`，用户 Key 成功调用的平台成本为 0，平台回退和视觉调用仍按平台规则计费。

对象存储保存知识库原件、题目资料、错题来源、聊天附件和导出包。数据库只保存对象 key 和
元数据。下载必须经过授权 API，不向客户端暴露任意对象 key。

`PendingCleanup` 是删除 outbox：数据库删除和清理任务同事务提交，Worker 重试删除 Qdrant、
对象存储、Redis 和 checkpoint 外部状态。`done` 保留为旧行兼容字段；`last_attempt_at`、
`next_attempt_at`、`completed_at` 和 `locked_at/locked_by` 记录可恢复租约；`locked_by` 保存每次
claim 独有的随机 lease token，续租、成功和失败更新同时校验具体行 ID 与 token。失败信息只
保存有界脱敏的 `last_error_code/last_error_text`，`progress_json` 只保存阶段性状态。Worker
通过 `FOR UPDATE SKIP LOCKED` 领取任务，过期租约可恢复；对象删除失败不得先删除数据库事实
并伪装成功。

`TutorProblemDraft.resource_id` 复用 `LearningResource` 原件和对象存储；识别候选仅保存在
`recognized_questions_json`，用 `selected_question_index` 选中一道，不创建 `Question`、
`ResourceQuestion`、Quiz、Attempt 或错题。`confirmed` 后题干、选项、
已知条件、当前思路合成为 `confirmed_problem_text`，并绑定唯一 tutor thread；后续轮次不再读取
识别草稿或客户端题干。未确认草稿 24 小时后由内部清理任务删除，原件通过
`PendingCleanup(kind=object_prefix)` 幂等清理。草稿表随用户和资源级联删除，进入用户导出。

`eduagents_core.document_recognition` 是业务无关的 PDF 检查、逐页分类、渲染、平台视觉转写与
Markdown 规范化层。知识库把成功页转成带真实页码的 `Element/Chunk`；辅导再独立执行多题
结构化抽取和人工确认。共享层本身不创建 KnowledgeBase、TutorProblemDraft、Question、
ResourceQuestion、Quiz、Attempt、ErrorItem 或学习证据。

## 用户导出与注销

### 导出

`POST /api/account/export` 创建异步 ZIP。导出包含结构化数据、文档/上传 manifest，以及当前
可读取的题目资料和错题来源文件。外部对象缺失时记录 `available=false` 和错误原因，不阻断
其他数据导出。

### 注销

`DELETE /api/account` 重新验证密码后：

1. 删除个人知识库和用户行；
2. 通过外键级联删除个人学习、会话、题目资料、作答、错题、画像等关系数据；
3. `usage_log.user_id` 置空以保留去标识化成本审计；
4. 写入 tenant cleanup，由 Worker 清理向量、对象、缓存和 checkpoint。

删除语义的具体外键以 migration 为准；新增用户数据表必须同时进入导出、注销和隔离测试。

## 不可变与锁定规则

- 已提交 migration 不修改，只增加新 revision。
- pending 题和未核验作答不写学习状态。
- 提交后的学生响应不可覆盖；graded 为终态，仅相同语义重试幂等。
- 产生 FSRS、错题、作答或掌握度证据后，不允许静默改变题型、答案或知识点。
- 更换资源关联知识库只在无证据时允许，并事务性更新所有题目 `kc_ids`。
- 账务流水和掌握度日志属于审计数据，不通过普通更新接口改写。
