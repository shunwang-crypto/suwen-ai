# API 业务契约

> 状态：Active
> 目标读者：前后端开发、测试与集成方
> 维护边界：本文记录稳定业务语义、状态机、权限和错误码；运行时字段与参数的完整定义以
> `/api/openapi.json` 和 Pydantic schema 为准。

约定：资源命名 `/api/{resource}`；错误统一信封 `{code, message, request_id}`；
鉴权三档：公开 / 用户(Bearer) / 管理员。SSE 端点用 fetch+ReadableStream 消费
（原生 EventSource 无法带 Authorization 头）。

新增或改变端点、状态机、错误码、枚举和 PUT/PATCH 语义时，必须在同一提交更新本文与
集成测试。不可见的个人资源通常返回 404，以避免泄露存在性。

## 基础端点

### 认证（公开，独立限流 5次/分/IP）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/auth/register | {email, password≥8} → 201 UserOut；重复 409 EMAIL_EXISTS |
| POST | /api/auth/login | → {access_token, expires_at} + httpOnly refresh cookie（path=/api/auth） |
| POST | /api/auth/refresh | cookie 轮换；旧 token 重放 → 401 并撤销该用户全部 refresh token |
| POST | /api/auth/logout | (用户) access jti 进 Redis 黑名单 + 撤销 refresh + 清 cookie → 204 |
| GET | /api/auth/me | (用户) → UserOut |

### 对话（用户，120次/分限流）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/chat | {message, session_id?, mode, kb_ids?, attachment_ids?} → SSE 流（见下）；无 session_id 自动建会话 |
| GET | /api/chat/sessions | 会话列表（按更新时间倒序，≤100） |
| GET | /api/chat/sessions/{id}/messages | 历史消息（他人会话 → 404，不泄漏存在性） |
| GET | /api/chat/sessions/{id}/run | 查询该会话的生成状态 → {status: running\|idle, run_id?} |
| POST | /api/chat/runs/{run_id}/stop | 显式停止后台生成 → 202；不存在或非本人任务 → 404 |
| PATCH | /api/chat/sessions/{id} | {title} 重命名 |
| DELETE | /api/chat/sessions/{id} | → 204，级联删消息 |

### 系统（公开）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/health | {status, gateway_mode, checks:{postgres, redis}} |
| GET | /api/docs | OpenAPI 文档 |

## SSE 事件协议（九类）

```json
{"event":"session","data":{"session_id":"…","run_id":"…"}}
{"event":"token","data":{"text":"泰勒"}}
{"event":"node_update","data":{"node":"generate","status":"running","detail":"生成回答中"}}
{"event":"trace_event","data":{"type":"router","route":"kb","reason":"…"}}          // M3
{"event":"citation","data":{"idx":1,"doc_id":"…","chunk_id":"…","page":42,"highlight":"…","doc_name":"…"}}  // M2
{"event":"followups","data":{"questions":["…"]}}
{"event":"interrupt","data":{"kind":"blueprint_confirm","payload":{},"resume_url":"/api/threads/{id}/resume"}}  // M3
{"event":"error","data":{"code":"GENERATION_FAILED","message":"…"}}
{"event":"done","data":{"session_id":"…","usage":{"cost_cny":0.002,"mode":"live","latency_ms":1234}}}
```

`POST /api/chat` 返回头同时暴露 `X-Chat-Session-ID` 和 `X-Chat-Run-ID`，供前端在
首个 SSE 帧被代理短暂缓冲时立即识别任务。SSE 订阅断开（切换会话、切换页面或刷新）
不会取消生成；任务会在 API 进程内继续。只有调用 `POST /api/chat/runs/{run_id}/stop`
才会主动取消。返回原会话时，客户端先查询 `/run`，任务结束后重新读取历史消息。

`mode` 为 `fast | think | web | research`：`think` 固定走 strong 模型且只返回最终答案；
`web` 强制进入网页搜索路径，`research` 同时启用 strong 思考模型与网页检索。
`kb_ids` 是独立资料范围，可与 `fast/think` 组合；`web/research` 使用网页范围。
旧客户端的 `kb` 模式及“未发送 mode 但携带知识库 ID”仍兼容。助手消息的 `meta.chat_mode`
记录本轮用户选择，和网关执行态 `meta.mode`（live/replay/stub）分开保存。

### 知识库（M2，用户级鉴权 + 限流）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/kb | {name, subject} 建库 |
| GET | /api/kb | 库列表 |
| DELETE | /api/kb/{id} | 删库 + 异步物理清理（向量/对象） |
| POST | /api/kb/{id}/documents | multipart 上传；后缀白名单+魔数+配额三重校验；同哈希幂等；入 ARQ 摄取队列 |
| GET | /api/kb/{id}/documents | 文档列表（含两阶段状态机 status/error_msg） |
| DELETE | /api/kb/{id}/documents/{doc} | 删文档 + 异步清理向量与对象（F2-A3） |
| POST | /api/kb/{id}/documents/{doc}/reindex | 重新摄取 |
| GET | /api/kb/{id}/documents/{doc}/chunks | 分块预览器（text/页码/heading_path/向量化状态） |
| POST | /api/kb/{id}/debug/retrieve | 检索调试面板：dense/sparse 命中、融合重排、置信度全透明 |

`/api/chat` 已扩展：请求体可带 `kb_id` → 检索过程 trace_event + citation 事件 + 引用块防注入提示词 + 无依据时明确拒答（不调模型）。

Wave3 机构库（D14）：
- `GET /api/kb` 每项新增 `owner_type/org_id/org_name/writable`；机构库写端点
  （上传/删文档/重建索引/删库/检索调试）member 一律 `403 ORG_READONLY`。
- `/api/chat` 请求体新增 `kb_ids: UUID[]`（联合检索，`kb_id` 保留等价 `kb_ids=[kb_id]`）；
  任一 kb 不可见 → `403 KB_FORBIDDEN`；citation 事件新增 `kb_id/kb_name` 来源库标注。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/orgs | {name} 建机构，创建者自动 admin，返回 8 位 invite_code |
| POST | /api/orgs/join | {invite_code} 凭码加入为 member；无效码 404，重复加入 409 |
| GET | /api/orgs | 我的机构列表（含 role/member_count；invite_code 仅 admin 可见） |
| GET | /api/orgs/{id}/members | 成员列表（仅成员可见，非成员 404） |
| POST | /api/orgs/{id}/kb | 建机构库（仅 org admin；owner_type='org', tenant_id=org_id） |

VLSE 闭环（F8-A2 画像注入 / M4-D2 画像联动出题 / §5.5 对话洞察）：
- `/api/chat` kb 路径把学生画像摘要（≤300 字，确定性拼装，不经 LLM）注入 generate 的
  system prompt，并下发 `trace_event {type:"profile", weak_points:[], chars}`；
  **有画像时答案语义缓存禁用**（答案已按该学生个性化，跨用户复用即错答案）。
  SSE `done` 之后每会话每 5 轮入队一次后台 Reflection。
- `POST /api/quiz` 请求体新增 `from_weak_points: bool`（true 时后端按掌握度自动填
  `kc_names`，`topic` 可省略并缺省为“薄弱点强化练习”，入图前先行下发
  `trace_event {node:"weak_points", event:"weak_points_selected", kc_names, p_mastery, recent_error}`；
  无薄弱点 → `400 NO_WEAK_POINTS`，不静默降级成普通出题）。
- `GET /api/profile/weak-points[?kb_id=]` → `{threshold, limit, weak_points:[{kc_id, name,
  p_mastery, recent_error}]}`；与出题走同一函数，预览即所练。
- 对话洞察由 worker `reflect_session(tenant_id, user_id, session_id)` 异步写画像：
  mastery_log `evidence_type="chat_reflection"`、`weight=0.3`（弱于测评 1.0 / 辅导小结 0.5），
  stub 网关下跳过。

### 多智能体工作流（M3，用户级鉴权 + 限流；SSE 流均含 node_update/trace_event/interrupt/done/error）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/threads/{thread_id}/resume | {value} interrupt 恢复协议 (M3-D2)；thread_id=`kind:user:local`，跨用户 404 |
| POST | /api/quiz | {topic, kb_id?, num_questions, difficulty_mix, from_weak_points?} → SSE：Blueprint → interrupt(blueprint_confirm) |
| GET | /api/quiz · /api/quiz/{id} | 普通智能练习卷列表 / 题目详情（kc_ids 已归一化）；`blueprint.kind=error_import` 的导入卷不返回 |
| POST | /api/quiz/{id}/attempts | {answers} → 判分（客观本地/简答 LLM 附理由）+ 错题自动入本 + BKT 直写 + mastery_log 审计；正确率<0.6 时附 `plan_adjustment`（自动补强，F3-A3）；导入卷不可作答 |
| POST | /api/plans | {goal, kb_id?, budget_hours_per_day, total_days} → SSE → interrupt(plan_confirm) |
| GET | /api/plans | 计划列表 |
| GET | /api/plans/{id} | 计划详情：每任务带 `task_key`（stageI.dayJ.taskK，三层 0 基下标）与 `done`，附 `progress{total,done,ratio}` |
| POST | /api/plans/{id}/tasks/{task_key}/toggle | {done?} 勾选/取消任务（缺省取反）；done 内嵌 structure_json，不另建列 |
| POST | /api/plans/{id}/adjust | 依学情调整：p_mastery<0.4 且计划未覆盖的 KC → 最近未完成日插「补强: {KC}」30 分钟任务 → {inserted, reason} |
| GET | /api/plans/{id}/adjustments | 调整历史（倒序）：{reason(中文), trigger: manual\|low_score_attempt, inserted[]} |
| POST | /api/tutor | {problem, message, thread_local_id?} 苏格拉底辅导单轮；同 thread 多轮推进 hint_level |

### 错题本（M4/F6，用户级鉴权 + 限流）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/errors | 错题列表；筛选 `state`(new\|reviewing\|mastered\|archived\|active) / `cause`(五分类) / `kc_id`，分页 `page`/`page_size`；含题干·选项·答案·解析·我的作答·错误次数·下次复习时间 |
| GET | /api/errors/stats | 仅统计 `verification_status=verified` 的错题：{total, by_cause(五分类补零), by_state, by_kc(top10)}；待核验数量单列 `pending_verification` |
| POST | /api/errors/{id}/archive | 手动归档：state→archived，退出今日复习队列 |
| POST | /api/errors/{id}/cause | {cause} 错因修正（五分类之一，越界 422） |

> 错因自动分类：`POST /api/reviews/{id}/answer` 判错时以 cheap 档 json_mode 重标错因并回传 `cause`；
> 网关 stub 模式不发请求、异常一律回落 `knowledge_gap`（入本初值），绝不冒充分类结果。

### 错题导入与原始来源（用户本人 + 租户隔离）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/errors/imports/capabilities | 上传能力、大小上限、图片/文档后缀和视觉模型是否可用 |
| POST | /api/errors/imports/manual | 创建空的手动草稿 → 201；未确认草稿按用户限额（默认 20）计数 |
| POST | /api/errors/imports | multipart `file` 上传图片/PDF/Word/PPT/文本，保存对象存储原件并返回草稿；文件魔数、后缀和大小均校验 |
| POST | /api/errors/imports/{id}/confirm | `{type, stem, options, my_answer, answer, explanation, kc_names, difficulty, kb_id?, cause, answer_verified}`；创建 `ErrorItem`，不创建 `Attempt`；答案未核验为 `pending` |
| PATCH | /api/errors/{error_item_id}/content | 修改导入错题内容；只能 `pending → verified`，不能倒退；复习/掌握度证据产生后禁止改题型、选项、正确答案、知识点 |
| GET | /api/errors/imports/{id}/source | 本人读取原始来源；图片/PDF `inline`，Word/PPT `attachment`，响应带安全的 `Content-Disposition` 和 `nosniff` |
| GET | /api/errors/{error_item_id}/source | 已保存错题读取来源的等价入口，继续校验错题所有权 |
| DELETE | /api/errors/imports/{id} | 仅可删除未确认草稿；数据库先进入内部 `deleting` 状态，原件删除成功后才删除数据库行 |

`GET /api/errors` 的导入题字段包括 `verification_status`、`source_import_id`、`content_editable`；题目资料错题还返回 `resource_id/resource_question_id`，前端可回到题目资料查看原题。`my_answer` 对导入题来自 `ErrorItem.original_response`，自动练习错题仍来自真实 `Attempt`。`Question.source_type` 为 `generated|manual|image|document`；新导入题的 `quiz_id` 为空并通过 `ResourceQuestion` 归属资料，不会出现在普通测验或教师组卷列表。

`ErrorItem.verification_status` 的状态只能从 `pending` 前进到 `verified`，不能回退；待核验错题不会进入复习、FSRS、掌握度、画像、报告或推荐。题目资料错题的“查看原题资料”链接使用 `/resources?resource_id=<resource_id>&question_id=<resource_question_id>`，页面会通过受保护接口打开资料并定位题目；无效题目参数不会跨资料读取，返回列表时会清理这两个查询参数。

导入状态为 `draft|confirmed`，对象清理期间内部为 `deleting`；过期草稿由 API 创建请求和 worker 定时任务清理。相关错误码：`ANSWER_NOT_VERIFIED`、`VERIFICATION_ROLLBACK_FORBIDDEN`、`ERROR_ITEM_LOCKED_AFTER_REVIEW`、`ERROR_ITEM_NOT_EDITABLE`、`TOO_MANY_PENDING_ERROR_IMPORTS`、`IMPORT_ALREADY_CONFIRMED`、`IMPORT_DELETE_PENDING`、`ERROR_IMPORT_DELETE_FAILED`、`STORAGE_QUOTA_EXCEEDED`、`SOURCE_NOT_FOUND`。

用户数据导出仍在 `uploads_manifest.json` 保留来源元数据；可用的错题原文件同时写入 `uploads/error_imports/{import_id}/{safe_filename}`，缺失对象以 `available=false` 和 `error=SOURCE_NOT_FOUND` 记录，不阻断其他数据导出。

## 结构化题目资料与真实测评

题目资料不进入普通 KnowledgeBase/RAG。资源文件由 `LearningResource` 保存，题目由
`ResourceQuestion` 保存并通过 `Question.quiz_id = null` 与正式题目脱钩；真实考试/作业
记录使用 `AssessmentAttempt → AssessmentAnswer → ResourceQuestion`。所有端点只返回本人
租户的数据；资源、题目和测评记录的越权访问统一返回 404。

### 题目资料

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/resources` | JSON 创建无文件资料，或 multipart `file` 上传 PDF/Word/PPT/文本/图片；返回 `status=uploaded` |
| GET | `/api/resources` | 本人资源列表 |
| GET | `/api/resources/{id}` | 资源元数据、识别状态和题数 |
| PATCH | `/api/resources/{id}` | 修改标题、科目、年级、学期、资源类型、关联 KB；不自动摄取 RAG |
| DELETE | `/api/resources/{id}` | 删除草稿资源；对象删除失败返回 `503 RESOURCE_DELETE_FAILED`，数据库行保留待重试 |
| GET | `/api/resources/{id}/source` | 本人原始文件；图片/PDF inline，Word/PPT attachment，固定 `nosniff` 和安全文件名 |
| POST | `/api/resources/{id}/extract` | 入队异步识别，状态 `uploaded/failed → extracting → reviewing/failed`；`extracting/reviewing/ready` 重试幂等且不重复入队；`textbook/lecture/notes` 或 `retrieval_policy=reference` 返回 `409 REFERENCE_RESOURCE_NOT_EXTRACTABLE` |
| GET | `/api/resources/{id}/questions` | 题目校对列表；存在未提交 attempt 时不返回参考答案 |
| POST | `/api/resources/{id}/questions/manual` | 手动新增一题，初始 `verification_status=pending` |
| POST | `/api/resources/{id}/confirm` | 按 `resource_question_ids` 选择性确认并将选中错题加入错题本；不创建 Attempt/Quiz |
| PATCH | `/api/resource-questions/{id}` | 修改题干、题型、选项、答案、知识点、页码、区域和置信度 |
| POST | `/api/resource-questions/{id}/verify` | 幂等确认 `pending → verified`；不允许倒退 |
| DELETE | `/api/resource-questions/{id}` | 删除尚未关联错题/测评的误识别题 |

`LearningResource.resource_type` 为 `exam_paper | mock_paper | workbook | worksheet | homework |
`other | single_question`；历史 `textbook | lecture | notes` 仅作为参考资料保留，不出现在新建
题目资料选项中。`LearningResource` 返回 `owner_id/created_by/source_filename/mime_type/size_bytes/sha256/`
`page_count/status/retrieval_policy/linked_kb_id/source_url/question_count/`
`pending_question_count`；`retrieval_policy=questions_only|none` 的题目资料不会自动创建
`Document/Chunk` 或进入普通 RAG。`ResourceQuestion.verification_status` 为
`pending | verified | rejected`；题目识别字段包含
`question_no/page_no/type/stem/options/detected_student_answer/`
`detected_reference_answer/explanation/points/kc_names/difficulty/confidence`，并返回
`is_error_selected` 和 `content_editable`。`is_error_selected` 只有在客户端明确选择题目
加入错题本时才持久化为 true；完成题目校对本身不会默认选择全部题目。`content_editable`
在题目尚未产生 FSRS 复习或掌握度证据时为 true；产生证据后为 false，服务端同时拒绝会
改变判分语义的编辑。`pending` 题不会进入复习、FSRS、掌握度、画像、报告或推荐；confirm
请求中的空 `resource_question_ids/error_question_ids` 表示不选择任何错题。

题目资料返回的 `owner_type` 取值为 `personal | organization`；这是资料契约，不能与知识库返回的 `owner_type=user | org` 混用。

修改 `linked_kb_id` 会在同一事务中按题目保存的 `kc_names` 重新解析每道题的 `kc_ids`；取消关联会清空这些 ID。若资料题已有 `ErrorItem`、`AssessmentAnswer`、`MasteryLog` 或其他学习证据，返回 `409 RESOURCE_KB_LOCKED_AFTER_EVIDENCE`，资料和题目映射均保持原值。

`Question.quiz_id` 对资源题为空；资源题只能在本人租户内读取。题目产生 FSRS review、
ErrorItem mastery evidence 或 AssessmentAnswer mastery evidence 后，改变题型、选项、
正确答案、知识点会返回 `409 ERROR_ITEM_LOCKED_AFTER_REVIEW`；状态只能
`pending → verified`，不能倒退。错误码还包括 `RESOURCE_QUESTION_NOT_VERIFIED`、
`RESOURCE_QUESTION_QUOTA_EXCEEDED`、`RESOURCE_QUESTION_HAS_ERROR_ITEM`、
`RESOURCE_QUESTION_HAS_ASSESSMENT`、`RESOURCE_QUESTION_HAS_ATTEMPT`、`RESOURCE_KB_LOCKED_AFTER_EVIDENCE`、
`QUESTION_HAS_QUIZ_OWNER`、`TOO_MANY_PENDING_RESOURCES`、`RESOURCE_QUOTA_EXCEEDED`、
`RESOURCE_PAGE_LIMIT_EXCEEDED`、`RESOURCE_FILE_TOO_LARGE`、`STORAGE_QUOTA_EXCEEDED`、
`RESOURCE_SOURCE_REQUIRED`、`SOURCE_SAVE_FAILED`、`RESOURCE_DELETE_FAILED`、
`RESOURCE_HAS_ERROR_ITEMS`、`RESOURCE_HAS_ASSESSMENTS`、`RESOURCE_HAS_ATTEMPTS`、
`EXTRACTION_QUEUE_FAILED`。

### 真实作答与错题记录

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/assessment-attempts` | 创建 `draft` attempt；同一资源允许多次 |
| GET | `/api/assessment-attempts` | 本人 attempt 列表 |
| GET | `/api/assessment-attempts/{id}` | 作答和评分；提交前不泄漏参考答案 |
| PATCH | `/api/assessment-attempts/{id}` | 仅 draft 可修改元数据 |
| DELETE | `/api/assessment-attempts/{id}` | 仅删除本人 `draft`；答案级联删除；`submitted/graded` 返回 `409 ASSESSMENT_NOT_DRAFT`，越权或重复删除返回 404 |
| PUT | `/api/assessment-attempts/{id}/answers` | 整体替换当前 attempt 的答案集合；请求未出现的旧答案会删除，空字符串且 `unanswered=false` 也会删除，`unanswered=true` 保存为“未作答” |
| PATCH | `/api/assessment-attempts/{id}/answers` | 部分更新请求涉及的题目；未出现的旧答案保留，明确空答案只删除对应题目 |
| POST | `/api/assessment-attempts/{id}/submit` | `draft → submitted`；至少有一条实际作答或显式 `unanswered=true`，否则 `409 ASSESSMENT_ANSWER_REQUIRED` |
| POST | `/api/assessment-attempts/{id}/grade-or-verify` | 写入评分；`verify=true` 才写 mastery/error；错误通过 `AssessmentAnswer → ErrorItem` 幂等关联，不创建 Attempt |

关联资源时 attempt 类型由 `resource_type` 自动映射：`exam_paper → formal_exam`、
`mock_paper → mock_exam`、`homework → homework`、`workbook/worksheet/other → practice`；
无资源记录才使用请求中的 `practice/imported_record`。attempt 类型为
`formal_exam | mock_exam | homework | practice | imported_record`，状态
只能从 `draft → submitted → graded` 单调前进；作答评分状态为 `pending | graded | verified`。
`draft` 才能保存作答，空字符串不会创建 `AssessmentAnswer`；显式未作答用
`unanswered=true` 持久化为响应文本 `未作答`。`submitted` 后 response 锁定；`verify=false` 只能写入 graded，不能
单独把 answer 标成 verified；`verify=true` 必须为所有已保存答案提供正确性，才会将整次
attempt 置为 graded。相同请求重试幂等，已 graded 的语义字段改变返回 409。
未核验作答/主观评分不进入掌握度、FSRS、画像、报告、推荐。已核验测评答案会纳入
报告真实作答次数和正确率。缺省主观题满分使用 `ResourceQuestion.points`，否则为 `1.0`；
正确得满分，错误得 `0`。主观题的 `score` 只是原始分数，不能自动推断正确性；最终
`verify=true` 必须显式提供 `is_correct`。分数必须满足 `score >= 0`、`max_score > 0`
且 `score <= max_score`，否则返回 `422 INVALID_ASSESSMENT_SCORE`。相关错误码：`ASSESSMENT_NOT_EDITABLE`、
`ASSESSMENT_NOT_SUBMITTED`、`ASSESSMENT_ALREADY_GRADED`、`ASSESSMENT_ANSWER_LOCKED`、
`ASSESSMENT_NOT_DRAFT`、`ASSESSMENT_ANSWER_REQUIRED`、`ASSESSMENT_ANSWER_NOT_FOUND`、
`ATTEMPT_VERIFICATION_REQUIRED`、`ANSWER_CORRECTNESS_REQUIRED`、`INVALID_ASSESSMENT_SCORE`、`INVALID_ATTEMPT_TYPE`、
`INVALID_GRADING_STATUS`、`DUPLICATE_ANSWER`、`RESOURCE_QUESTION_NOT_FOUND`、
`RESOURCE_QUESTION_NOT_VERIFIED`。

### 迁移与导出

0012 新增 `learning_resources`、`resource_questions`、`assessment_attempts` 和
`assessment_answers`，并为 `questions.quiz_id` 改为可空、`error_items` 增加
`original_response/resource_question_id/assessment_answer_id` 引用。迁移会把旧
`ErrorImport` 和 `blueprint.kind=error_import` 的题转成资源，保留 ErrorItem、FSRS、
MasteryLog 和原始对象后再清理隐藏 Quiz；不会删除历史 Attempt。资源 owner、创建者、
测评学生/阅卷人和关联 KB 均有明确 FK 删除语义；downgrade 先移除引用再删除结构化表，
不伪造已删除的隐藏 Quiz。
旧环境可执行 `scripts/audit_0011_error_import_isolation.sql` 做只读审计。

账号导出的 `uploads_manifest.json` 保留原始对象元数据，同时把可读取的资源文件写入
`uploads/resources/{resource_id}/{safe_filename}`，旧错题导入文件写入
`uploads/error_imports/{import_id}/{safe_filename}`。缺失对象记录
`available=false,error=SOURCE_NOT_FOUND`，不阻断其他数据导出。
