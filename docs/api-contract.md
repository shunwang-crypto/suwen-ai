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

### 认证（公开，独立限流）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/auth/captcha | 获取一次性图形验证码；用于防机器人 |
| GET | /api/config/public | 返回非敏感 `registration_mode`（`closed`/`invite`/`open`）和邮箱验证要求 |
| POST | /api/auth/register/email-code | `closed` 返回 `REGISTRATION_CLOSED`；其他模式接受 `{email, captcha_id, captcha_answer}`，通过图形验证码后发送 6 位邮箱验证码，202；邮箱 60 秒冷却、分钟/日限额 |
| POST | /api/auth/register | `closed` 返回 `REGISTRATION_CLOSED`；`invite` 必须带 `{email, email_code, invite_code, password≥8, display_name?}`；`open` 忽略 `invite_code`。成功 → 201 UserOut；新用户固定 `student` 且 `email_verified_at` 非空，重复 409 EMAIL_EXISTS |
| POST | /api/auth/login | → {access_token, expires_at} + httpOnly refresh cookie（path=/api/auth） |
| POST | /api/auth/refresh | cookie 轮换；旧 token 重放 → 401 并撤销该用户全部 refresh token |
| POST | /api/auth/logout | (用户) access jti 进 Redis 黑名单 + 撤销 refresh + 清 cookie → 204 |
| GET | /api/auth/me | (用户) → UserOut |

图形验证码只用于防机器人，邮箱验证码只用于证明邮箱所有权；两者不是同一功能。普通登录不
要求邮箱验证码，仍保留连续失败 3 次后要求图形验证码。公开注册不能创建 `teacher` 或
`admin`，这两类账号必须通过受控流程创建。邮箱按 `strip().lower()` 规范化，验证码只保存
HMAC 摘要，最多错误 5 次、10 分钟过期、成功后单次消费。注册关闭时返回
`403 REGISTRATION_CLOSED`。

邮件发送失败返回 `503 EMAIL_SEND_FAILED`（配置缺失为 `SMTP_NOT_CONFIGURED`），不会创建用户；
已注册邮箱的验证码接口仍返回统一 `202` 且不发送邮件，避免账号枚举。邮件正文仅含品牌、
验证码、有效期和忽略提示，不含密码或系统敏感信息。

认证错误码还包括 `EMAIL_CODE_REQUIRED`、`EMAIL_CODE_INVALID`、`EMAIL_CODE_RATE_LIMITED`、
`CAPTCHA_REQUIRED`、`CAPTCHA_INVALID`、`EMAIL_EXISTS`、`INVITE_CODE_INVALID` 和 `REGISTRATION_CLOSED`；错误统一使用
`{code, message, request_id}`。本地未配置 SMTP 时使用开发发送器；联调真实邮件时，凭据只写
未提交的 `.env`。

### 对话（用户，120次/分限流）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/chat | {message, session_id?, mode, kb_ids?, attachment_ids?} → SSE 流（见下）；无 session_id 自动建会话。旧 `kb_id` 仅兼容一版且在 OpenAPI 标为 deprecated |
| GET | /api/chat/sessions | 会话列表（按更新时间倒序，≤100） |
| GET | /api/chat/sessions/{id}/messages | 历史消息（他人会话 → 404，不泄漏存在性） |
| GET | /api/chat/sessions/{id}/run | 查询该会话的生成状态 → {status: running\|idle, run_id?} |
| POST | /api/chat/runs/{run_id}/stop | 显式停止后台生成 → 202；不存在或非本人任务 → 404 |
| PATCH | /api/chat/sessions/{id} | {title} 重命名 |
| DELETE | /api/chat/sessions/{id} | → 204，级联删消息 |

### 系统（公开）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/health/live | 进程存活探针 `{status: "ok"}`；不探依赖，失败处理由重启/编排器负责 |
| GET | /api/health/ready | 就绪探针 `{status,gateway_mode,checks:{postgres,redis,qdrant,worker[,graph,vector_audit]},vector_audit}`；PG/Redis/Qdrant 失败返回 503，worker/审计异常可为 degraded |
| GET | /api/health | 旧兼容路径，OpenAPI 标为 deprecated 并返回 `Deprecation: true` 与 successor `Link`；只检查 PG/Redis。仓库调用已切换，外部 monitor 仍需观察期确认后再移除兼容 |
| GET | /api/docs | OpenAPI 文档 |

### 钱包与额度（用户）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/billing/wallet | 当前额度余额 |
| GET | /api/billing/ledger | 额度变动流水 |
| GET | /api/billing/pricing | `{markup, signup_grant, recharge_enabled, referral_campaign:{enabled,url,reward_credits}, note}`；邀请活动关闭时 `url=null` |
| POST | /api/billing/orders | 本地 mock 支付通道；禁用时返回 `403 PAYMENT_DISABLED` |

模拟支付只用于本地调试，不代表真实支付能力。
硅基流动邀请活动只提供外部跳转；点击不产生额度。管理员在供应商后台确认有效邀请后，
通过调额接口使用唯一业务凭证人工发放，活动额度不可提现或转让。

### 管理员用户与额度
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/admin/users?query=...&limit=... | 仅管理员；邮箱精确优先并支持安全前缀查询；query 至少 3 个字符；仅返回用户身份、角色、状态、注册时间和字符串额度余额 |
| POST | /api/admin/users/{user_id}/wallet-adjust | 仅管理员；`{delta, note, reference}`；delta 必须是字符串、非零、-1000 至 1000 且最多 4 位小数；同一用户同一 reference 重试返回原结果，若参数不同返回 `409 WALLET_ADJUST_REFERENCE_CONFLICT` |
| GET | /api/admin/vector-reconcile?kb_id=...&include_all=... | 仅管理员；PG child chunk 与 Qdrant point 的 `(tenant_id,kb_id,point_id)` 精确元数据对账，只读、不读取向量/正文、不修改两侧；检索栈不可用时 `503 retrieval_stack_unavailable` |

调额使用 Decimal，并在同一个数据库事务内锁定钱包账户、写入 `admin_adjust` 流水和更新余额。
`(user_id, ref)` 的条件唯一索引由 0015 migration 提供，保证并发请求最多一次真正入账。
管理员审计日志记录执行人、目标用户、金额和业务凭证；搜索日志只记录 query 哈希。

无 `query` 时 `/api/admin/users` 返回 `{items,page,page_size,total,has_more}`，默认按注册时间倒序、每页 20；`query` 参数保留旧数组响应以兼容已有运营客户端。`GET /api/admin/users/{user_id}` 返回聚合详情和脱敏的 BYOK 状态（provider、启用状态、后四位、模型名），绝不返回密码、token、密文或 nonce。所有管理员端点要求 `admin` 角色。

`/api/admin/vector-reconcile` 返回
`{status, kb_total, kb_drifted, kb_orphaned, missing_vectors, orphan_points, items, orphans, errors, missing_points, extra_points, wrong_tenant_points, wrong_tenant_details, malformed_points, document_inconsistencies}`。
`status` 为 `ok` / `drift` / `error`。`missing_vectors` 是 PG 标 `done` 但 Qdrant 查不到的块数——
该库检索已静默失效，需重新摄取；`orphan_points` 是 Qdrant 有而 PG 无的点数，属删除不完整。
精确字段还区分 `extra_points`、`wrong_tenant_points`、`malformed_points` 和文档一致性问题；
扫描只取 point ID 与 tenant/kb/doc 标识，不读向量或正文。默认只返回有问题的库，
`include_all=true` 才带上一致的。单库查询（带 `kb_id`）仍从全局元数据扫描中过滤目标 KB，
因此能识别该 KB 的 extra/orphan；单个库出错记入 `errors` 而不中断整体扫描。


### 用户自带 DeepSeek 模型（0017）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/settings/model-credential | 返回 configured、enabled、fallback、provider、后四位、模型、验证状态和服务端允许模型；不返回 Key、密文或 nonce |
| PUT | /api/settings/model-credential | 受控验证后整体替换并启用；验证失败不覆盖旧配置 |
| POST | /api/settings/model-credential/test | 只测试输入 Key，不保存；测试环境 stub 不连接外部服务 |
| PATCH | /api/settings/model-credential | 只修改启用、平台回退和模型选择，不接收或返回密文 |
| DELETE | /api/settings/model-credential | 幂等删除，之后使用平台模型 |

第一期 provider 固定为 `deepseek`，base URL 和模型白名单只由服务端配置。`fallback_to_platform=false` 时用户文本 Key 失败直接返回 `BYOK_*` 错误；开启后才执行平台预算预检、平台降级链和钱包扣费，UsageLog 标记 `credential_source=platform`。成功使用用户 Key 的记录标记 `credential_source=user`、`platform_billed_cost_cny=0`，仍保留 token 用量。图片理解、Embedding、Rerank、Tavily、解析和存储不使用用户 Key。

启用 `MODEL_ROUTING_ENABLED=true` 后，平台文本、视觉和学习计划分别使用独立候选链；候选状态和熔断器由管理员故障面板按候选 ID 展示，不返回密钥、密钥后四位、完整供应商错误或请求内容。链路全部失败时返回稳定错误码
`MODEL_ROUTE_UNAVAILABLE`、`VISION_ROUTE_UNAVAILABLE` 或 `PLAN_ROUTE_UNAVAILABLE`。免费候选不在调用前预扣钱包，只有即将调用付费候选时才预检；usage 仍记录实际候选的 provider、model 和 token。视觉请求和学习计划不使用 BYOK。

## SSE 事件协议（九类）

```json
{"event":"session","data":{"session_id":"…","run_id":"…"}}
{"event":"token","data":{"text":"泰勒"}}
{"event":"node_update","data":{"node":"generate","status":"running","detail":"生成回答中"}}
{"event":"trace_event","data":{"type":"router","route":"kb","reason":"…"}}
{"event":"citation","data":{"idx":1,"doc_id":"…","chunk_id":"…","page":42,"highlight":"…","doc_name":"…"}}
{"event":"followups","data":{"questions":["…"]}}
{"event":"interrupt","data":{"kind":"blueprint_confirm","payload":{},"resume_url":"/api/threads/{id}/resume"}}
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

### 知识库（用户级鉴权 + 限流）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/kb | {name, subject} 建库 |
| GET | /api/kb | 库列表 |
| DELETE | /api/kb/{id} | 删库 + 异步物理清理（向量/对象） |
| POST | /api/kb/{id}/documents | multipart 上传；后缀白名单+魔数+配额三重校验；同哈希幂等；入 ARQ 摄取队列 |
| GET | /api/kb/{id}/documents | 文档列表（含两阶段状态机 status/error_msg） |
| DELETE | /api/kb/{id}/documents/{doc} | 删除文档并异步清理向量与对象 |
| POST | /api/kb/{id}/documents/{doc}/reindex | 重新摄取 |
| GET | /api/kb/{id}/documents/{doc}/chunks | 分块预览器（text/页码/heading_path/向量化状态） |
| POST | /api/kb/{id}/debug/retrieve | 检索调试面板：dense/sparse 命中、融合重排、置信度全透明 |

`/api/chat` 已扩展：请求体可带 `kb_id` → 检索过程 trace_event + citation 事件 + 引用块防注入提示词 + 无依据时明确拒答（不调模型）。

PDF 摄取逐页分类为 `native_text/scanned/mixed/failed`。完整可信文本页直接进入现有 chunker；
扫描页和“少量标题文本 + 图片主体”的 mixed 页整页有界渲染并使用平台视觉模型忠实转写。
`Document.recognition_meta_json` 只保存页码、分类、状态、字符/图片计数、是否使用视觉及有界
错误码，不保存全文、渲染图片或模型原始响应。文档状态新增 `recognizing` 和
`partial_ready`；单页失败时其余页继续分块、索引并显示失败页码，全部页面失败则不生成
Chunk/向量。重试先清理本文档旧向量与 Chunk，并在落库和向量写入前后重新确认 Document
仍存在。引用继续使用 Chunk 的真实 `page_start/page_end`。

机构库：
- `GET /api/kb` 每项新增 `owner_type/org_id/org_name/writable`；机构库写端点
  （上传/删文档/重建索引/删库/检索调试）member 一律 `403 ORG_READONLY`。
- `/api/chat` 请求体新增 `kb_ids: UUID[]`（联合检索，deprecated `kb_id` 暂保留等价 `kb_ids=[kb_id]`）；
  任一 kb 不可见 → `403 KB_FORBIDDEN`；citation 事件新增 `kb_id/kb_name` 来源库标注。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/orgs | {name} 建机构，创建者自动 admin，返回 8 位 invite_code |
| POST | /api/orgs/join | {invite_code} 凭码加入为 member；无效码 404，重复加入 409 |
| GET | /api/orgs | 我的机构列表（含 role/member_count；invite_code 仅 admin 可见） |
| GET | /api/orgs/{id}/members | 成员列表（仅成员可见，非成员 404） |
| POST | /api/orgs/{id}/kb | 建机构库（仅 org admin；owner_type='org', tenant_id=org_id） |

学习状态闭环（画像注入、画像联动出题与对话洞察）：
- `/api/chat` kb 路径把学生画像摘要（≤300 字，确定性拼装，不经 LLM）注入 generate 的
  system prompt，并下发 `trace_event {type:"profile", weak_points:[], chars}`；
  **有画像时答案语义缓存禁用**（答案已按该学生个性化，跨用户复用即错答案）。
  SSE `done` 之后每会话每 5 轮入队一次后台 Reflection。
- `POST /api/quiz` 请求体新增 `from_weak_points: bool`（true 时兼容路径委托同一服务端练习推荐，自动填
  `kc_names`，`topic` 可省略并缺省为“薄弱点强化练习”，入图前先行下发
  `trace_event {node:"weak_points", event:"weak_points_selected", mode, count, kc_names, difficulty_mix, rationale}`；
  trace 不含 `p_mastery`、`confidence` 或 `mastery_band`。若推荐命中 `plan_task`，计划归因由
  recommendation 的 `plan_id/task_id` 服务端回填并在入图前校验；浏览器带来的过期任务不会静默切换，
  其他 weak/cold-start/maintenance 分支不携带计划归因。KC enrichment 进行中返回 `409 QUIZ_KC_PREPARING`，
  已终止或无合法 KC 返回 `409 QUIZ_KC_NOT_READY`）。
- `GET /api/profile` 的每个掌握项返回 `evidence_state: unobserved|estimated|observed`
  与展示标签；`unobserved` 的 `p_mastery` 为 null，不对外假装成 30% 薄弱。
  响应同时返回 `weak_threshold`。
- `GET /api/profile/weak-points[?kb_id=]` → `{threshold, limit, weak_points:[{kc_id, name,
  p_mastery, evidence_state, recent_error}]}`；候选必须是 `observed`，并与出题共享同一个服务端 loader（近 7 天 verified 错题优先，
  再按 p_mastery 升序和稳定 KC 顺序），预览即所练；精确 p_mastery 只保留在该鉴权画像接口。
- 对话洞察由 worker `reflect_session(tenant_id, user_id, session_id)` 异步写入
  `learner_insights`；普通对话中的 `confused/understood` 均不写 `mastery_log`、不推进 BKT。
  stub 网关下跳过。

个性化记忆控制面（0031，ChatGPT 式用户控制层）：
- `POST /api/preferences/personalization` `{enabled: bool}` 记忆总开关：关闭后
  `/api/chat` 不再注入画像摘要、`/api/quiz from_weak_points` 与 `weak-points` 预览返回空、
  开场卡不再引用软抑制条目；**豁免清单**：未成年判定、内容审核、掌握度计算永不受此开关
  影响；关闭不删除任何数据（关闭 ≠ 删除）。切换即失效画像缓存。
- `GET /api/profile/insights` 每条新增 `suppressed`；`PATCH /api/profile/insights/{id}`
  `{suppressed: bool}` 软抑制（"不要再提及"）：保留原文只停注入，与 DELETE 物理删除互补。
- `GET /api/profile/memory/preview` → `{summary, weak_points, mastered}`：与问答注入同源
  同缓存的摘要预览（设置页"AI 眼中的你"），所见即 AI 所得。
- `GET /api/next-actions` → `{items:[{type,title,reason_code,reason_params,reason,href}]}`：
  今日页唯一决策源（确定性聚合，零 LLM）。**推荐理由走稳定码**：`reason_code` 是后端
  `ReasonCode` 枚举值（snake_case），`reason_params` 只含计数（int）、定性档位枚举词与
  有界展示字符串——参数是逐码闭合白名单且拒绝 float，掌握度小数在类型上无处落脚
  （P2-7）。展示文案的唯一出处是前端 `lib/reason-text.ts`。`reason` 散文是**过渡字段**，
  仅供未升级的旧客户端，新客户端只读 `reason_code`/`reason_params`。同一套码同时用于
  `/api/practice/next` 与出题蓝图的逐 KC 理由（`difficulty_reasons[].reason_code`）。
  例外：`/api/plans/{id}/adjust` 与 `adjustments` 的 `reason` 仍是中文散文——它是落库的
  审计事实，历史行无法反解为码。
- `POST /api/profile/answer-ratings` `{message_id?, rating: good|bad, reason?
  (too_long|too_short|confusing|off_topic), weak_points?[], profile_used}`：回答级评价遥测
  （独立于 0004 工单 feedback）；`(user_id, message_id)` 唯一，改主意为 UPDATE；提交后内联
  跑确定性统计：同一讲解风格近 30 天 ≥3 次评价且"没听懂"占比 ≥50% 时，写一条
  `learner_insights(kind=preference_signal, source=ai_inferred)` 建议（7 天同文去重、
  每用户 ≤3 条），仅展示待用户在设置页确认——**推断永不直接改偏好，评分链路
  （review_grade/quiz_grade）永不注入画像**。

### AI 工作流（用户级鉴权 + 限流；SSE 流均含 node_update/trace_event/interrupt/done/error）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/threads/{thread_id}/resume | `{value}` interrupt 恢复协议；thread_id=`kind:user:local`，跨用户 404 |
| POST | /api/quiz | {topic, kb_id?, num_questions, difficulty_mix, from_weak_points?, plan_id?, task_id?, confirmation_mode?} → SSE：Blueprint → interrupt(blueprint_confirm)；归因只接受本人计划的 practice/mixed 任务 |
| GET | /api/practice/next | 由服务端按到期错题、今日计划、薄弱 KC、已准备资料和上下文缺口统一返回下一步推荐；返回 `status/action/source/title/reason_code/reason_params/question_count/difficulty_mix`，不返回题干或掌握度小数 |
| POST | /api/quiz/recommended | 只接受 `scope_kb_id?/plan_id?/task_id?/previous_quiz_id?/adjustment?` 安全上下文；服务端重新计算推荐，决策变化返回 409 及最新 recommendation，稳定后复用 Quiz SSE 并以 `confirmation_mode=auto` 生成蓝图和题目 |
| GET | /api/quiz · /api/quiz/{id} | 普通智能练习卷列表 / 题目详情（kc_ids 已归一化）；`blueprint.kind=error_import` 的导入卷不返回 |
| POST | /api/quiz/{id}/attempts | `{answers:[{question_id,response,predicted_confidence?}]}`；完整提交归因练习后幂等完成对应计划任务；信心为可选 1–5 级且不影响判分。响应执行客观本地/简答 LLM 判分、错题自动入本、BKT 与 mastery_log；正确率<0.6 时附 `plan_adjustment`；导入卷不可作答 |
| GET | /api/metacognition/summary?window=day\|week\|all | 本人信心校准 `{sample_n,brier_score,overconfidence_rate}`；无有效样本的指标为 null，不保存聚合行 |
| POST | /api/metacognition/explain-back | `{student_text,kc_id?,prompt_span_ref?,source_attempt_id?,question_id?}` → `understanding_score`、反馈、缺口、重试建议、`rubric_version`、`quality_score`（默认 null）和 `quality_shadow`；只保存反馈观察，不写掌握度/画像/错题/报告 |
| POST | /api/plans | `{goal, current_level?, target_outcome?, success_criteria?, kb_id?, budget_hours_per_day, total_days}` → SSE → `interrupt(plan_confirm)`；`budget_hours_per_day` 与 `total_days` 必须由用户自然语言或缺失追问明确确认，服务端不再静默补默认值；interrupt 含受限的 `goal_analysis`、定性 `skill_gaps`、草案和资源。服务端重新校验 KB 访问、任务结构与资源引用 |
| GET | /api/plans | 计划列表，含 `kb_id/kb_name/status`；`status` 为 `active/completed/archived`，每个用户最多一个 active 计划 |
| GET | /api/plans/{id} | 本人计划详情（历史计划可读）：含 `status/kb_id/kb_name/resources`；`structure` 保留确认时的 `goal_analysis/skill_gaps` 快照；每任务带服务端生成且不随结构调整变化的 UUID `task_id`、`objective/instructions/completion_criteria/action_type/resource/resource_id/status`、`practice_count`（1–20）和 `difficulty`，附由 time-control 业务时钟计算的 UUID 集合 `schedule{current_day,today_tasks,overdue_tasks,upcoming_tasks}`、`progress{total,done,ratio}` 与只读派生 `kc_dependency{levels,edges}`。`kc_dependency` 只包含当前计划任务 KC 及真实先修边递归闭包，节点带 `kc_id/kc_name/task_ids/task_statuses/is_current_task/is_prerequisite/is_blocking/blocking_task_ids`，不含 `p_mastery`/`confidence`；空图返回空 `levels/edges`，不写回计划结构。 |
| GET | /api/plans/{id}/tasks/{task_id}/tutor-context | 校验本人计划和任务归属后返回有界 `problem/kb_id/plan_id/task_id`；任务文字不进入 URL，非法或他人任务统一 404 |
| POST | /api/plans/{id}/tasks/{task_id}/toggle | `{status: pending|done|skipped}` 显式更新任务状态；状态内嵌 `structure_json`，不另建列 |
| POST | /api/plans/{id}/tasks/{task_id}/complete | 资料阅读显式完成；仅接受带持久化资料的 `read/mixed` 任务，保存 `manual_read` 安全完成证据；打开链接不会自动完成 |
| POST | /api/plans/{id}/archive | 放弃唯一 active 计划并置为 archived；completed/archived 计划只读，不可隐式恢复 |
| POST | /api/plans/{id}/adjust | 依学情调整：p_mastery<0.4 且计划未覆盖的 KC → 最近未完成日插带目标、步骤、完成标准、`action_type=practice` 的「补强: {KC}」任务 → `{inserted, reason}` |
| GET | /api/plans/{id}/adjustments | 调整历史（倒序）：{reason(中文), trigger: manual\|low_score_attempt, inserted[]} |
| POST | /api/tutor | {problem, message, thread_local_id?, plan_id?, task_id?} 苏格拉底辅导单轮；同 thread 多轮推进 hint_level，只有 `summarize` 完成后回写归因任务 |
| GET | /api/tutor/entrypoints | 返回今日未完成计划任务的安全 `plan_id/task_id`，以及本人已核验可用错题的安全 `error_id` 列表；无上下文时返回稳定空状态，不返回题干、答案或完整任务文本 |

作答前信心的五级概率映射固定为 `0.1/0.3/0.5/0.7/0.9`。高信心答错率只统计概率
`>=0.7` 的样本；Brier 分数和该比率均只描述预测校准，不评价学习能力。跳过预测时不写
`metacognition_predictions`。重复 `question_id` 在一次提交中只处理首项，避免重复 Attempt。
Explain-back 使用 cheap 文本模型和输入/输出审核；模型或 JSON 失败时保留原文并返回
`degraded=true`，额度耗尽仍返回统一 `INSUFFICIENT_CREDITS/BUDGET_EXCEEDED`。`kc_id` 不存在、
非本人或跨租户统一 `404 KC_NOT_FOUND`；不支持的输入返回统一审核/校验错误信封。
`source_attempt_id` 必须属于当前用户；若同时提供 `question_id`，两者不匹配返回
`SOURCE_QUESTION_MISMATCH`。`rubric_version` 当前为 `explain-back-rubric-v1`；结构化
`quality_score` 只在未来显式通过一致性门槛后展示，默认 `quality_shadow=true`，不影响掌握度、FSRS、
推荐或报告。

Quiz 题目通过 schema 后会进入独立 cheap critic，检查蓝图一致性、难度、答案/解析、歧义和
可用素材中的明确事实冲突。只有 major 问题触发整卷返工，最多一次；第二次仍有 major 时以
`critic_review: exhausted` 结束，禁止无限循环。critic 网关或响应解析失败以
`critic_review: degraded` 保留 schema 合格题目，额度异常不降级而继续走统一 402/429。
事实核验会重新检索但不把素材正文或 critic 原始响应写入 Graph state/checkpoint；state 只保留
最多 5 条、每条有界的修复指令和返工次数。

Quiz 的 `done` 使用 schema v2，不再用空 `result` 表示失败：

```json
{
  "schema_version": 2,
  "outcome": "generation_failed",
  "result": null,
  "error": {
    "code": "QUIZ_GENERATION_EMPTY",
    "message": "这次没有生成出可用题目，请重试。",
    "stage": "generate",
    "retryable": true,
    "attempts": 3
  }
}
```

`outcome` 仅允许 `succeeded`、`cancelled_by_user`、`generation_failed`、
`validation_failed`。`succeeded` 只在题目校验通过并成功落库后发送；明确拒绝蓝图才是
`cancelled_by_user`；所有生成轮次没有非空题目数组是 `generation_failed`；至少有一题但
schema 重试耗尽是 `validation_failed`。`confirmation_mode` 为 `required` 或 `auto`，默认
`required`；生成失败诊断只记录 provider/model/feature、响应长度、finish reason、JSON
解析/顶层 key/题目计数、截断、重试轮次及 request/thread ID，不记录题干、密码、token 或学生资料。
如果校验成功但事务未能落库，或落库函数返回空结果，服务端只发送 `error` 事件
（`QUIZ_FINALIZE_FAILED` / `QUIZ_FINALIZE_INVALID`），不会发送 `succeeded`。

### 错题本（用户级鉴权 + 限流）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/errors | 错题列表；筛选 `state`(new\|reviewing\|mastered\|archived\|active) / `cause`(五分类) / `kc_id`，分页 `page`/`page_size`；含题干·选项·答案·解析·我的作答·错误次数·下次复习时间 |
| GET | /api/errors/{id}/tutor-context | 仅本人可读的错题 Tutor 上下文，返回有界 `problem/error_item_id/kc_ids`；他人或不存在的 ID 统一 404，题干不放进 URL |
| GET | /api/errors/stats | 仅统计 `verification_status=verified` 的错题：{total, by_cause(五分类补零), by_state, by_kc(top10)}；待核验数量单列 `pending_verification` |
| POST | /api/errors/{id}/archive | 手动归档：state→archived，退出今日复习队列 |
| POST | /api/errors/{id}/cause | {cause} 错因修正（五分类之一，越界 422） |
| GET | /api/reviews/today | 到期复习队列；计划入口使用 URL 中的 `plan_id/task_id` |
| POST | /api/reviews/{id}/answer | `{response, plan_id?, task_id?, review_duration_ms?}`；客户端必须为一次提交生成并在网络重试间复用 `Idempotency-Key`（≤128 字符）；仅真实到期复习作答可幂等完成 `review_errors/mixed` 计划任务 |

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

`ErrorItem.verification_status` 的状态只能从 `pending` 前进到 `verified`，不能回退；待核验错题不会进入复习、FSRS、掌握度、画像、报告或推荐。题目资料错题的“查看原题资料”链接使用 `/materials?resource_id=<resource_id>&question_id=<resource_question_id>`，页面会通过受保护接口打开资料并定位题目；无效题目参数不会跨资料读取，返回列表时会清理这两个查询参数。
Explain-back 的来源校验错误包括 `SOURCE_ATTEMPT_NOT_FOUND`、`QUESTION_NOT_FOUND` 和
`SOURCE_QUESTION_MISMATCH`。

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
后续迁移 0024 增加 ErrorItem 的 Attempt/信心快照，0025 增加用户级 FSRS-6 shadow 参数产物，
0027 增加 Explain-back 的来源、rubric 和 shadow 质量分字段；这些 schema 变化不代表已经完成真实
环境验收或启用个体参数/数值评分。

账号导出的 `uploads_manifest.json` 保留原始对象元数据，同时把可读取的资源文件写入
`uploads/resources/{resource_id}/{safe_filename}`，旧错题导入文件写入
`uploads/error_imports/{import_id}/{safe_filename}`。缺失对象记录
`available=false,error=SOURCE_NOT_FOUND`，不阻断其他数据导出。

### 分级辅导附件与题目草稿

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tutor/drafts/capabilities` | 格式、文件/PDF/单次识别页限制及平台视觉计费提示 |
| POST | `/api/tutor/drafts` | multipart 上传 PNG/JPG/JPEG/WebP/PDF/DOCX/PPTX；PDF 先返回真实页数，不自动识别 |
| POST | `/api/tutor/drafts/manual` | 创建手工题目草稿，不产生学习证据 |
| GET/PATCH | `/api/tutor/drafts/{id}` | 查询状态、多题列表，选择并编辑题干、选项、已知条件、当前思路 |
| POST | `/api/tutor/drafts/{id}/recognize` | 按 PDF 页码幂等入队；运行中重复请求不重复入队 |
| POST | `/api/tutor/drafts/{id}/confirm` | 保存不可变题目快照并分配新的 `thread_local_id` |
| GET | `/api/tutor/drafts/{id}/source` | 鉴权预览/下载原件；非本人和跨租户均返回 404 |
| DELETE | `/api/tutor/drafts/{id}` | 删除未确认草稿并事务写对象清理 outbox；已确认返回 `TUTOR_DRAFT_LOCKED` |

状态为 `uploaded -> recognizing -> review -> confirmed`，任何解析/模型错误进入 `failed`，
但原件、选页和手工草稿均保留。`POST /api/tutor` 带 `tutor_draft_id` 时忽略客户端
`problem`，只使用服务端确认快照，并校验快照绑定的 thread。未确认草稿和单纯确认操作不会
写 `Attempt/ErrorItem/MasteryLog/LearnerInsight/Report`；只有原有 tutor graph 完成
`tutor_summary` 后只将误解原文写入可删除的 `learner_insights`，不写
`MasteryRecord/MasteryLog`。辅导任务完成也不是掌握度证据。

辅导 PDF 只处理用户选择页，并综合文字质量、正文覆盖、图片面积/数量和图形对象判断页面；
完整文本页直接提取，扫描或 mixed 选择页渲染 PNG 并逐页调用视觉模型，真实页码由服务端保留。
默认选择 3 页，单次最多 8 页，总页数 50，144 DPI，单图 4MB，
并发 2，单页视觉 45 秒，整项 120 秒，最多重试 3 次。Word/PPT 沿用本地解析器；检测到
嵌入图片时返回 `EMBEDDED_IMAGES_NOT_RECOGNIZED`，不得臆造图片内容。

worker job 只携带 `tenant_id/draft_id`；图像 Base64 只在一次平台视觉请求的进程内存中构造，
不进入 Redis、checkpoint、日志或 trace。Graph state 只保存草稿/资源 ID、确认文本和必要页码。
视觉请求固定 `credential_policy=platform`，不会把用户 DeepSeek Key 发送给视觉模型；文本辅导
仍按 BYOK 配置调用用户 DeepSeek。视觉识别可能消耗平台额度，前端在识别前明确展示此边界。

共享网关拒绝任何未显式声明 `credential_policy=platform` 的 `image_url`/视觉模型请求。
视觉/OCR 请求完全不读写共享 cassette；解析到用户 BYOK 的文本请求在 record/live fallback/
replay 各路径也不读写共享 cassette，即使用户允许失败后回退平台。cassette key 不包含 Key、
Key 哈希、密文或 nonce。
