# EduAgents 生产化差距审查报告

> [!WARNING]
> **历史评审快照。** 本文记录评审当时的代码与风险，发现项可能已经修复，也可能已被新的实现取代，不可直接用作当前上线判断。
> 当前状态见 [`STATUS.md`](../STATUS.md)，文档入口见 [`docs/README.md`](README.md)。

> 评审视角：SRE / 应用安全 / 数据合规 / C 端增长。方法：6 个方向对真实代码逐行取证，其中数据方向直连线上 PG 库实测。所有发现均标注 `文件:行号`，可自证真伪。
> 结论一句话：**多租户 RLS 隔离、refresh 轮换、跨租户缓存这三块做得扎实；但「钱包没有闸、数据删不掉、输出没人管、限流被反代打穿」四条是上线阻断级，且都不在你的自曝清单里。**

---

## 一、新发现（已知清单之外）

### P0 — 上线阻断（500 人内测开放前必须修）

---

**P0-1　反向代理之后，登录/注册限流桶坍缩成「全平台每分钟 5 次」的单桶**

- 一句话：`_auth_rate_limit` 用 `request.client.host` 做 key（`apps/api/src/eduagents_api/routers/auth.py:32`），但 `deploy/docker-compose.yml:85-87` 的 uvicorn 启动命令没有 `--proxy-headers`/`--forwarded-allow-ips`，Caddy → uvicorn 后所有请求的 client host 都是 Caddy 容器 IP，于是 `auth_rate_limit_per_minute=5`（`config.py:49`，login/register 还共桶）变成**整个公网每分钟总共 5 次**。
- 事故场景：任意匿名用户 6 秒内发 6 次登录，**全站所有人的登录和注册在这一分钟内全部 429**——一条 `while true; do curl ...; done` 就能让内测开天窗；同时「每攻击者 5 次/分」的撞库防护根本不存在。若用最常见的快修把 `FORWARDED_ALLOW_IPS=*` 打开，XFF 变攻击者可控 → 无限撞库，防护彻底旁路。
- 方案：uvicorn 加 `--proxy-headers --forwarded-allow-ips="<Caddy 容器网段>"`（**不要用 `*`**）；login 桶**同时按账号（email 归一化）分桶** `acct:{email}:login`；`_auth_rate_limit` 里断言解析出的 IP ≠ 反代自身才计数。
- 工作量：0.5 人天（代理头）+ 1 人天（账号维度桶 + 锁定 + 失败日志）。
- 验证：两台机器分别打 `POST /api/auth/login`，修复前第二台同样吃 429；修复后互不影响。

---

**P0-2　钱包没有闸：worker 后台任务不接预算/不计量 + 无全局成本熔断 + 配额按字节而成本按 chunk**

三个洞叠加，构成「一个下午烧四位数、无任何自动止损」的攻击面。

- (a) **worker 三个后台任务的网关裸构造**：`enrichment.py:88`、`reflection.py:121`、`study_guide.py:192` 均为 `OpenAIGateway(settings)`，对比 `main.py:55-59` 少了 `usage_sink` 和 `budget_guard`；`openai_gateway.py:159-161` 明确 `guard is None 就 return`。KC 抽取（`kc/extraction.py:158-200`）连 `tenant_id/user_id` 都不传，`usage.py:17-20` 因此直接丢弃该行。→ 摄取/反思/学习指南的全部支出**既不扣预算，也不进 usage_log**，成本看板系统性少报。
- (b) **无任何全局/租户日成本熔断**：全仓 grep `global_budget|daily_cost|cost_limit|kill_switch` 零命中；唯一预算是每用户日预算（`budget.py:34`）。`user_daily_budget_cny=5.0`（`config.py:56`）× 500 人 × 30 天 = **理论月上限 ¥75,000**，是中性预期（≈¥2,200）的 35 倍；账号数无上限时上界是 ∞。`admin.py:62` 的「一键熔断」是可用性断路器，与成本无关。
- (c) **配额按字节、成本按 chunk**：配额只有 `max_docs_per_user=200` / `max_storage_mb_per_user=1024` / `max_upload_mb=50`（`config.py:138-140`），`.md/.txt` 在白名单；摄取侧无页数/chunk/token 上限（`ingestion.py:73-78` 无条件切全文，`enrichment.py:198` 对每个子块发 2~3 次 LLM）。有效步长 620 字符、每 chunk ≈¥0.00123 → 上传 200 个 ~930KB 纯文本（1500 chunks/个）= **¥368/合法账号**；配合开放注册（P0-1），10 账号一下午 ≈ ¥3,700，脚本化注册 + 灌库 24h 可达 **¥100,000+**。文件卡在 `job_timeout=600`（`worker/main.py:58`）且 arq 未设 `max_tries`（默认重试 5 次）→ 每文档烧 5 遍还永远不 ready。
- 方案（按顺序，合计 4.5 人天）：① Redis 全局键 `budget:global:{date}`，`precheck` 先查全局、`settle` pipeline 同时 INCR 用户键与全局键，软限 80% 全站强降 cheap / 硬限 100% 拒非 admin / 150% 触发全档熔断（1 天）；② worker `startup()` 建共享带 `usage_sink`+`budget_guard` 的 gateway 放进 `ctx["gateway"]`，三任务改从 ctx 取，extraction 补 tenant/user（1.5 天）；③ 上传按 `size_bytes` 预估 chunk 数超 `max_chunks_per_doc=3000` 直接 400，加 `max_chunks_per_user`，WorkerSettings 显式 `max_tries=1`（2 天）。
- 验证：record 模式跑 `enrich_document`，断言 `usage_log` 出现 `feature in (enrich_prefix,kc_extract_chunk,study_guide)` 且 `sum(cost_cny)>0`；预置全局键到阈值后任意用户任意 feature 抛 `BudgetExceeded`；造 10MB .txt 断言返回 `CHUNK_QUOTA_EXCEEDED`。

---

**P0-3　删文档不失效答案缓存，且 `kb_version` 从头到尾从未自增——被删文档的原文摘录还能被吐 7 天**

- 一句话：`models.py:143` `kb_version default=1`，全仓 grep **无任何一处赋值/自增**；`answer_cache.py:11` 注释声称「重新摄取旧缓存天然失效」——该机制是**死代码**。`ingestion.py:185-198` 的 `cleanup_document` 只删向量和文件，不碰缓存；缓存 payload 里 `citations[].highlight` 就是文档正文 160 字（`chat.py:335`）。
- 事故场景：家长发现上传的体检报告 PDF 被 AI 引用，点删除，接口返回 204、文件和向量确实没了——但接下来 7 天内同一个问题仍从 Qdrant `answer_cache` 命中，答案连原文摘录带页码一起吐出来。这是可截图上网的隐私事故。
- 方案：`delete_document`/`delete_kb`/`reindex_document` 三处统一 `UPDATE knowledge_bases SET kb_version = kb_version + 1`（一改就让所有旧缓存 filter 落空）；`AnswerCache` 补 `delete_by_kb`，接进删除路径。
- 工作量：2 人天。
- 验证：`psql -c "select count(*) from knowledge_bases where kb_version<>1"` 删过文档后应 >0，当前恒为 0；删文档后重复提问断言不再命中缓存。

---

**P0-4　个保法删除权在物理层不可执行：无注销接口 + 业务表对 users 零外键 + checkpoint 明文问答无 RLS 无 TTL 无删除路径**

- 一句话：`auth.py` 仅 register/login/refresh/logout/me，**无注销**；实测除 `refresh_tokens` 外，`attempts/chat_sessions/error_items/mastery_log/mastery_records/plans/quizzes/usage_log/org_members` 的 `user_id` **全是裸 UUID 无外键**，`DELETE FROM users` 会静默孤儿化全部学情数据。更糟：LangGraph 的 `checkpoints/checkpoint_blobs/checkpoint_writes` 三表**不在 Alembic 里、无 RLS、无 user_id 列**（实测 `relrowsecurity=false`，用 `edu` 超级用户角色连接，`graph_runtime.py:41-52`），JSONB 里明文存着 `query`/`answer`/`user_id` 原文；`chat.py:260` 每轮 QA 都 `uuid4().hex` 新建 thread 永不复用，checkpoint 已 **77,348 行 vs chat_messages 2,988 行（26 倍）**，删会话（`chat.py:533-541`）与 checkpoint 无任何关联键，删了也残留。
- 事故场景：用户投诉至网信办要求删号，运维执行 `DELETE FROM users`，PG 成功返回，但学情/错题/会话/用量/checkpoint 明文问答全部留库，下次导数据被当成有效样本；同时 checkpoint 以每轮 ~26 行速度膨胀，几周后单盘写满 → PG 拒写 → 全站不可用。
- 方案：① 建 `DELETE /api/auth/me`，事务内清 12 张 PG 表 + 入队 `purge_tenant` job 清 Qdrant 两 collection + 对象存储前缀 + Redis 前缀；② 迁移 0003 给 8 张表加 `FK(user_id) ON DELETE CASCADE`；③ `chat.py:260` 改用 `session_id` 作 thread local_id，让 checkpoint 与会话一一对应，删会话时一并 `DELETE FROM checkpoints WHERE thread_id='qa:{uid}:{sid}'`；④ checkpoint 池改用 `edu_app` 非超级角色，并加 30 天 cron 清理。
- 工作量：5（注销 + 外键）+ 3（checkpoint 归属与清理）= 8 人天。
- 验证：`select tc.table_name ... where ccu.table_name='users'` 当前只回 1 行，应 ≥9 行；`select count(*) from checkpoints where split_part(thread_id,':',3)::uuid not in (select id from chat_sessions)` 应为 0（当前是全部）。

---

**P0-5　模型输出侧零审核 + 无未成年人保护——面向学生（含未成年）的合规下架级组合**

- 一句话：`moderation.py:171-178` 的 `check_output` 写好了但**全仓零生产调用方**（grep 只命中定义与 test）；`middleware.py:85` 只调 `check_input`。SSE 逐 token 直推（`chat.py:290-293`、`qa_graph.py:455-473`），输入过审的困扰表达（如「我最近好累不想活了」不命中任何词表项）→ 模型生成消极/自残内容**逐 token 直流**到可能是未成年的学生，全程无拦截、无危机干预、无留证。叠加：`web_search.py:49-56` 联网结果只过防注入（`sanitize`）不过内容审核，外部违法/成人内容被「洗」成带 `[1][2]` 引用的可信答案；产品对年龄/监护/防沉迷全部 grep 为空（`auth.py`/`schemas.py`/`models.py`）。
- 事故场景：国内教育类 AI + 面向未成年 + 输出无拦截，命中未成年人保护法/防沉迷强监管，出事即下架；这不是「加强」，是发版阻断。
- 方案：① SSE 侧分句缓冲审核——攒到句末标点或 N 字先送 `check_output`，通过才 flush，命中则截流替换为安全兜底文案 + 危机资源；危机语义接云审核（本地词表挡不住）；② web 结果进上下文前逐条过 `check_output`，命中整条丢弃；③ 注册加年龄声明 + 未成年模式（更严阈值 + 时段/时长限制）+ 监护人同意入库；系统提示词加未成年安全约束段。
- 工作量：4（输出+web 审核）+ 4~6（未成年人保护）= 8~10 人天。
- 验证：构造 20 条越过输入侧的自残/违法/成人用例，断言 SSE 不出现原文且收到拦截事件；未成年账号跑通时段限制与阈值收紧。

---

**P0-6　流被打断（断网/切后台/切会话）→ 已生成的助手答案永不落库、刷新后消失，且漏计费**

- 一句话：用户消息在**流开始前**已入库（`chat.py:141-148`），助手消息只在流跑完后由 `_persist_assistant` 落库（`chat.py:375/456`，**不在 finally**）；客户端断开 → sse_starlette 取消生成器 → GeneratorExit → 持久化永不执行。网关的 `_emit_usage` 同样只在流完整结束时结算（`openai_gateway.py:394-407`）→ 上游 token 已消耗但预算不记账。前端 SSE 是一次性 async generator，**零重连/零续传**，`id:`/`retry:` 被显式忽略（`sse.ts:44`）。
- 事故场景：手机用户问完切去微信 10 秒回来、或地铁 wifi 抖一下，已生成 95% 的回答中断；刷新进同一会话只看到自己的提问，回答凭空消失且无任何提示——这是问答产品最刺眼的高频事故。用户重发则再烧一次全额 token，历史里还多一条重复问题。
- 方案：`_persist_assistant` 移入 `finally`/`except CancelledError`，取消时也落已生成部分并标 `truncated=true`，`_emit_usage` 同步在取消时结算；前端对 truncated 消息给「回答未完成，点此重问」入口。
- 工作量：1.5 人天。
- 验证：kb 问答生成到一半时 DevTools 右键该 `/api/chat` 请求 Cancel，刷新进同一会话，断言半截答案仍在且 usage_log 有记账。

---

### P1 — 上线一月内必须

---

**P1-1　请求体在鉴权前被完整读入内存，无任何大小上限 → OOM / 磁盘打满**
`middleware.py:75` `await request.body()` 之前只检查 `Authorization` 头**存在**不校验有效性；`kb.py:146` `await file.read()` 先全量读盘再判大小；Caddyfile 无 `request_body max_size`。匿名者带 `Authorization: Bearer x` + 2GB body → 中间件缓进内存后才 401，几个并发即 OOM；`POST /api/kb/<uuid>/documents` 传 100GB multipart 可写满宿主机磁盘。**方案**：Caddyfile `handle /api/*` 加 `request_body { max_size 60MB }`；`middleware.py` 在读 body 前按 `Content-Length` >1MB 直接 413；`kb.py` 改分块读累计。**0.5 人天**。验证：`head -c 200MB /dev/zero | curl -XPOST .../api/chat -H "Authorization: Bearer x" --data-binary @-` 应立刻 413 而非内存暴涨。

**P1-2　健康检查依赖挂掉仍返 200，就绪探针无法摘除故障实例，且不检 Qdrant/worker**
`health.py:25-30` 无论 checks 结果都返 dict（恒 200），只探 PG+Redis，漏 Qdrant/MinIO/worker；compose healthcheck 用 `urlopen` 只看是否抛异常。PG 故障时探针仍绿、Caddy 继续打流量。**方案**：依赖不 ok 返 `503`；补 Qdrant/worker 探测；拆 `/live` 与 `/ready`。**1 人天**。验证：停 PG 后 `curl -w '%{http_code}' /api/health` 应得 503。

**P1-3　无封号/禁用用户能力，users 表无 status 字段**
`admin.py` 仅 time-travel + faults；`models.py:108-117` User 无 `status/disabled`；权限只判 role。薅羊毛账号出现时运维只能 SSH 进 PG 改，且删用户后 refresh token 仍有效 15 分钟。**方案**：加 `users.status` 列 + `POST /api/admin/users/{id}/disable`，`current_user` 查 status 拒绝并拉黑该用户全部 jti。**2 人天**。验证：disable 后被禁用户任意受保护请求返回 401/403。

**P1-4　机构成员只能进不能出：无退出/踢人/解散接口，org_members 无 UPDATE/DELETE 策略**
`orgs.py` 仅 create/join/list/members/kb；`0002_org_knowledge_base.py:92` 注释自认「无 UPDATE/DELETE 策略（成员移除是 P1）」→ 连 `edu_app` 都删不掉，必须上 superuser；`invite_code` 无有效期/次数/轮换（`models.py:86`），泄露即永久有效。员工离职/学生转学后仍能读机构付费库。**方案**：加 `DELETE /{org}/members/{uid}`（禁删最后一个 admin）+ `POST /{org}/leave` + `DELETE /{org}`（含 purge_org job）+ `POST /{org}/invite-code/rotate`；补迁移加 org_members UPDATE/DELETE 策略；写操作后调 `invalidate_org_cache`（否则 60s 缓存残留，`deps.py:18`）。**2~4 人天**。验证：踢人后该用户 `GET /api/kb` 里机构库应消失（当前接口直接 405）。

**P1-5　删除走「PG 先提交、ARQ 后入队」，中间失败即永久孤儿，无对账任务**
`kb.py:317-326` 事务退出即 COMMIT，之后才 `enqueue_job`；无 outbox/补偿/reconcile cron（worker 唯一 cron 是清缓存）。Redis 抖 3 秒 + 20 人点删除 → PG 行没了、接口 500、用户放弃 → 向量和原始文件永久留在 Qdrant/MinIO 无人知晓。**方案**：outbox 模式（删除事务内写 `pending_cleanup` 表，worker 消费）+ 每日 reconcile 扫孤儿。**3 人天**。验证：mock `enqueue_job` 抛异常后比对 Qdrant `doc_id` 集合与 `select id from documents`。

**P1-6　摄取失败无重试无回收，实测 76.6% 文档 failed 仍占配额；超时 CancelledError 接不住 → 永久卡死**
实测 441 篇文档 **338 篇 failed**；`kb.py:171-174` 配额按 `sum(size_bytes)` **不区分 status**，失败文件继续吃配额；`worker/main.py:58` `job_timeout=600` 超时抛 `CancelledError`（BaseException），`ingestion.py:179` 的 `except Exception` 接不住 → 状态永久停在 parsing。前端轮询无超时、「重建索引」按钮在 active 态被禁用（`document-table.tsx:123`）→ 用户只能删了重传，且重传命中哈希去重直接返回那条卡死行、不重新入队（`kb.py:175-182`）。**方案**：配额加 `status<>'failed'`；`reap_stale_ingestion` cron 把 >30min 未变的 processing 置 failed 并重入队（≤3 次）；`except BaseException` 兜住 CancelledError；前端超时提示 + 允许对 stuck 文档 reindex。**2~3 人天**。验证：`select status,count(*) from documents group by 1` 修复后 failed 占比应 <5%。

**P1-7　worker 无死信队列 + reflect 非幂等 → 重复扣费 + 重复写画像**
`reflection.py:100-211` 无幂等键，成功即追加 mastery_log 并推 BKT；`main.py` WorkerSettings 无 `max_tries`（默认重试 5 次）无 DLQ；enqueue 无 `_job_id`。LLM 调用后 DB 提交前抛错 → 重跑再花一次钱且重复写画像；崩溃后超时重投同样重复。**方案**：`_job_id=reflect:{session}:{turn}` 让 ARQ 天然去重；`_update_mastery` 以 (evidence_type,evidence_id) upsert；成本敏感步骤 `max_tries=1` + `on_job_fail` 写死信表。**1.5 人天**。验证：同 session 连投两次，mastery_log 只增一组、usage_log 只记一次。

**P1-8　无数据导出能力，个保法可携带权无法响应**
全站唯一 export 是教师组卷（`teacher.py:96`，require_teacher 且只导试卷）。用户依个保法第 45 条要「我的全部数据」，30 天法定期限内交付不了，手拼 SQL 必漏 checkpoint 和向量库。**方案**：`POST /api/me/export` → ARQ 生成 ZIP 落对象存储返 24h 预签名 URL，复用 `tenant_session` 保证不越权。**3 人天**。验证：`curl -XPOST .../api/me/export` 返回 job_id，产物逐表核对行数。

**P1-9　BudgetExceeded 在多数入口是 500，注释承诺的「429 + Retry-After」在代码里不存在**
`BudgetExceeded` 继承 `GatewayError(Exception)` 非 HTTPException（`gateway/base.py:22`），`middleware.py` 只注册了 2 个 handler → 落 `except Exception` 变 500 `INTERNAL_ERROR`。kb 路径 SSE 正确发 `BUDGET_EXCEEDED`（`chat.py:343`），但通用路径被降级成 `GENERATION_FAILED`「请重试」（用户一重试又触发前置节点烧钱），出题/计划/报告直接 500 白屏。更隐蔽：`qa_graph.py:178` 的 `except GatewayError: return None` 把 BudgetExceeded 在 6 个节点静默吞掉当解析失败继续跑。**方案**：`main.py` 注册 `@app.exception_handler(BudgetExceeded)` 返回 429 + `Retry-After`；`qa_graph` 收窄 except 让 BudgetExceeded 冒头；通用路径补 `except BudgetExceeded`。**0.5 人天**。验证：预置预算满额后打 3 类端点，断言 SSE 首个 error 为 `BUDGET_EXCEEDED`、非流式 429 且带 Retry-After。

**P1-10　降级链把 cheap 降到 standard = 降级即涨价 5.7~23 倍**
`openai_gateway.py:45-49` `cheap → [cheap, standard]`；cheap ¥0.35/¥0.35 vs standard ¥2/¥8（`config.py:81-88`），而 qa 自省环 9 次调用里 7 次是 cheap。SiliconFlow 抖 30 秒（`breaker_open_seconds=30`）期间单次问答成本 ¥0.015→¥0.055（3.7 倍），恰在高峰。另 `openai_gateway.py:329` 的 `app_env!="prod"` 分支：若内测沿用 `APP_ENV=dev`，全失败时会把 **stub 假答案当真答案返回**且不计费。**方案**：cheap 降级目标改为换一家便宜供应商或 `[cheap]` + 确定性降级；降级到更贵档时打 `degraded_upcost` 指标告警；startup 断言 `app_env=='prod' or gateway_mode!='live'`。**1 人天**。验证：故障注入打挂 cheap，断言 usage_log 无 `tier='standard'` 的 `qa_` 行。

**P1-11　语义缓存实际按「用户」隔离 + 有画像即禁用 → 真实命中率 ≤10%，成本模型必须重算**
缓存作用域是 `ctx.tenant_id`，而签发 token 时 `tenant_id = user.id`（`auth.py:108`）→ 跨用户命中恒 0，机构库 10 人问同题写 10 份互不命中；可缓存条件 `chat.py:201-206` 还要求 `not profile.summary`——用户做过任何测评就永久失去缓存，恰是最活跃最费钱那批。行业 30-40% 命中假设完全不成立。若按 35% 命中报给评委，实际高 1.4 倍。**方案**：作用域从 `tenant_id` 改为 `kb.tenant_id`（知识库所有者），个人库不变、机构库立刻跨成员共享；命中率进 ops 看板真实观测。**0.5~1.5 人天**。验证：org KB 下 A 提问后 B 问同题，断言 `usage_log.mode='cache'` 无新增 live 行。

**P1-12　预算是「事后记账 + check-then-act」竞态，可超发 1.7~2 倍；虚拟时钟 time-travel 会清零全站预算**
`budget.py:63-68` GET 后判断、`:86-89` 调用完成后才 INCR，自认「并发会少量超支」；限流 120/分是按请求数不按 token。单用户开 120 并发全在 spent=0 通过 → 实际配额变 ~¥8.6/日。更严重：`budget_key` 的日期读 `timectl.now` 虚拟时钟（`budget.py:56`），`admin.py:27` 的 time-travel 推进全局 Redis 偏移 → **一次演示按钮把全站日预算清零**。**方案**：precheck 改 Lua 原子预扣 + settle 返还（`ratelimit.py:8` 已有 register_script 范式）；预算键日期改用真实系统时钟而非虚拟时钟。**1.5 人天**。验证：100 并发后 `budget:{user}:{date}` ≤ 预算×1.05；`timectl.advance(3)` 后断言预算键不变。

**P1-13　虚拟时钟 timectl 生产无护栏，一次调用全局且持久推进全体 FSRS 到期/预算日界**
`admin.py:27-34` 仅 `require_admin` 无 `app_env=='prod'` 判断；偏移存单 Redis 键无 TTL（永久，`timectl.py:29`）；是 FSRS/复习/预算的唯一时间源。令牌泄露或误点即把全体学生复习到期整体前移、预算翻天，偏移永久残留。**（好消息：审计时间戳走 PG `func.now()` 真时钟，不被污染。）方案**：入口加 `if app_env=='prod': raise 403`，或独立 env `ENABLE_TIME_TRAVEL` 默认关。**0.5 人天**。验证：prod 配置下 `POST /api/admin/time-travel` 返回 403。

**P1-14　迁移内联在 api 启动命令里，多副本并发 alembic + checkpointer.setup 竞态；chunks 换 RLS 策略取排他锁**
`docker-compose.yml:85-87` 每个 api 副本都跑 `alembic upgrade head && uvicorn`；`0002:66-76` 对最大表 chunks `DROP/CREATE POLICY` 需 ACCESS EXCLUSIVE 锁；`checkpointer.setup()` 也是启动期 DDL。扩到 2+ 副本时并发迁移抢锁、DDL 撞 already exists。**（好消息：两迁移均有 downgrade。）方案**：迁移抽成独立一次性 `migrate` service，api/worker `depends_on migrate: completed`。**1 人天**。验证：`docker compose up --scale api=3` 无迁移竞态。

**P1-15　主回答生成无 max_tokens 上限 + 熔断器是进程内状态多实例失效**
`qa_graph.py:436-447` generate 不传 max_tokens（quiz_generate 同），最坏路径一次问答 9~10 次模型调用、输出可打满 8000 token。`openai_gateway.py:75` breakers 是实例字典、`circuit.py:36` events 进程内 deque，4 API + 10 arq 并发时供应商挂掉要浪费 84 次失败（含 tenacity 重试达 252 次网络请求）才全部熔断，worker 每次新建 gateway → **熔断永不生效**。**方案**：generate 传 `max_tokens=1500`、quiz 按题数动态设；断路器状态存 Redis 或至少 worker 复用 `ctx["gateway"]` 单例。**1~1.5 人天**。验证：最坏路径 usage_log 该 request_id 行数 ≤9；故障注入后并发 20 请求实际出站 <10。

---

### P2 — 迭代（compact）

| # | 问题（文件:行号） | 事故 | 方案 | 人天 |
|---|---|---|---|---|
| P2-1 | 无客服/反馈/工单入口（apps/web、apps/api 路由 grep=0） | 用户遇 AI 答错/误封无处提交只能弃用，报障无法关联 request_id | `POST /api/feedback` 存表带 request_id + 前端悬浮入口 + admin 列表 | 2 |
| P2-2 | 无公告/维护模式/feature flag（grep=0） | 迁移/大故障时无法挂维护页、无法灰度关单个功能，只能整站下线 | Redis `sys:maintenance` 中间件 + `feature_flags` 表 + `get_flag()` | 2 |
| P2-3 | 简答 LLM 判分无置信度/无申诉，解析失败静默判 0 且级联写掉掌握度（`services.py:272-330`） | 学生答对被误判 0 → 写错 BKT → 驱动「薄弱点出题」持续放大成错误画像 | 判分附申诉入口触发复核并回滚 mastery；解析失败标「需人工复核」不判 0（客观题已是规则匹配，无需改） | 3 |
| P2-4 | 输入审核只认 `message` 字段，topic/query/problem 绕过（`middleware.py:52,82`） | 用 `POST /api/quiz` 的 topic 当敏感内容通道，模型照常生成落库 | `_MODERATED_PATHS` 扩到 quiz/plans，改遍历字段白名单；ResumeIn.value 收紧为显式模型 | 0.5 |
| P2-5 | sanitize 仅 13 条字面正则、红队仅 5 条明文（`sanitize.py:25-117`、`test_prompt_injection.py:18`） | 上传教材藏「忽 略 指 令」拆字/base64/多语言注入均不命中，间接注入进 LLM | 匹配前去空白/零宽字符 + base64/hex 解码复检；红队扩变体；高价值场景叠语义分类器 | 3~4 |
| P2-6 | moderation 词表缺失 fail-open（`moderation.py:106-109`），自残仅 1 soft 词 | 词表文件被清空 → 审核静默全放行；「不想活了」无覆盖 | 接云审核作主审、本地词表前置快筛；词表缺失应告警而非放行 | 2~3 |
| P2-7 | 学情画像发往第三方 LLM 且进前端 trace，无脱敏无同意（`qa_graph.py:426,481`） | 掌握度/薄弱点属可推断学习能力的敏感数据，未经同意随每次问答出境 | 出境前脱敏（去标识只传抽象标签）；隐私政策明示并取同意；trace 只返计数 | 2 |
| P2-8 | 结构化日志打印 query 原文 + worker 日志无 request_id 断链（`answer_cache.py:200`、`logging.py`） | 「我妈得了乳腺癌…」进 ELK 全员可搜；worker 处理同会话无法串起来查 | structlog 脱敏 processor 哈希化 query/content/answer；worker 入口 bind session/tenant/user | 1.5 |
| P2-9 | usage_log/checkpoint 无留存上限（`models.py:362`，实测 6846 行只增） | 监管问「留存多久怎么删」答不上；usage_log 可还原某人每天用量画像 | `purge_retention` cron：usage_log 180 天 / checkpoint 30 天 / mastery_log 2 年 | 1 |
| P2-10 | dev/prod 未分离：cookie 非 Secure、`/api/docs` 公开、admin 端点带进生产零审计（`auth.py:49`、`main.py:127`、`admin.py` 全文无 log） | `.env` 照抄上线 → refresh cookie 明文经 HTTP 可嗅探；openapi 泄漏全端点；time-travel 误点无人可查 | prod 关 docs、admin router 包 `app_env!='prod'`、每个 admin 写操作补 `log.warning` | 1 |
| P2-11 | 报表按 UTC 日切，与预算 Asia/Shanghai 矛盾（`reports.py:71` vs `budget.py:28`） | 学生北京时间凌晨刷题被算到昨天，趋势图「今天 0 题」打卡断签 | reports 统一 `CN_TZ`；把 CN_TZ 提到 `core/clock.py` 共用 | 0.5 |
| P2-12 | embedding/rerank/Tavily 不计价不计量（`config.py:99-107` 无 price 字段） | 中性场景 rerank 占单次成本 35% 却在看板不存在；看板少报约 45% | Settings 加 aux 单价；provider 接 usage_sink 按 batch 估 token 发 UsageEvent；Tavily 改 Redis 共享缓存 | 1.5 |
| P2-13 | 单价硬编码、`.env` 未覆盖、usage_log 无单价快照、cached_tokens 恒 0（`openai_gateway.py:415`） | DeepSeek 调价后静默按旧价算且无法回溯重算；cache 命中按全价高估成本 | 价格写进 `.env.example` + startup 打印；UsageLog 加单价列快照；透传 `prompt_cache_hit_tokens` 分两档计价 | 1.5 |
| P2-14 | 无套餐/免费额度/用量分层骨架（grep=0，全用户同一档 ¥5） | 不能给种子用户加额度也不能给可疑账号降额，只能改全局 env 重启 | users 加 `plan` + `daily_budget_cny_override`；新号默认 free ¥1、内测名单 pilot ¥5；注册复用 orgs.invite_code | 2 |
| P2-15 | 部分错误码未进信封映射，前端显示英文裸码（`middleware.py:110`，缺 KB_FORBIDDEN/RETRIEVAL_UNAVAILABLE/ORG_READONLY） | 检索服务挂时用户看到红框「RETRIEVAL_UNAVAILABLE」，泄漏内部码 | 补三个码的中文文案 | 0.2 |
| P2-16 | 密码超 72 字节直接 500，中文密码 25 字触发（`security.py:14`、`schemas.py:11` 上限是字符非字节） | 用户用 25 个汉字口令注册得到「服务器内部错误」；无需登录即可稳定触发 500 污染告警基线 | schema 加 `len(v.encode())<=72` 校验返 422；hash 前显式截断 | 0.25 |
| P2-17 | 上传大文件无前端体积预校验、无进度条（`kb/page.tsx:197` 只校验后缀） | 传 60MB PDF 要等全部字节传完 + 后端读完内存才回「文件过大」，白等约 50 秒 | 前端 `file.size` 预校验；`XMLHttpRequest.upload.onprogress` 进度；后端按 Content-Length 提前 413 | 1.5 |
| P2-18 | 流无停滞超时 + 降级对用户不可见 + 注册自动登录竞态 + i18n 英文裸串（多处） | 上游卡住永远「正在思考…」；降级无标注；注册成功但登录失败反复报「已注册」；usage 行显示 cache/standard | 客户端 N 秒软超时；done 透出 degraded/tier；register 失败区分「已建号请登录」；mode 中文映射 | 1.5 |

---

## 二、已知差距的修正（仅列低估/误判处）

- **#2「密钥明文 .env」严重性被低估**：真正的爆点不是「明文」，而是 `config.py:42` 默认 `dev-secret-change-me`，且 `main.py` lifespan **启动时无任何断言拒绝默认/弱 secret**。HS256 + 已知 secret = 任何拿到 `.env`（CI 日志/镜像层/开发机备份/演示机）的人自签 `role:"admin"` token 直取运营面板与 time-travel。修正动作：startup fail-fast `if app_env=='prod' and (jwt_secret in {默认值} or len<32): raise`，0.5 人天，列为 Day-1。（另：`.env` 里 4 个 LLM key 是明文真实密钥，虽已 gitignore 未入库、镜像也不含，但上线前应轮换并改 secrets 注入。）

- **#8「输出侧审核未接入流式」被低估**：不是「还没做」，而是 `check_output` 函数**已写好却全仓零调用**（死代码），且 `web_search` 外部内容同样零审核被洗成带引用可信答案，叠加**面向未成年无任何保护**——这三者合起来是下架级 P0（见 P0-5），不是迭代项。

- **#10「移动端仅不破版 + 无计费」偏乐观也偏悲观**：偏悲观处——移动端实测抽查 chat/quiz 双抽屉 + 汉堡菜单 + `break-words`/`overflow-x-auto` 都做了，窄屏无明显破版，可从阻断项降级；偏乐观处——「无多设备会话管理」的真实后果是 **access token 过期后点登出不吊销 refresh cookie**（`auth.py:174` 先 401 到不了删 cookie），共享电脑上下一个人打开自动进上一个账号（见下）。

- **#5「无日志留存策略」被低估**：不只是没策略，是**日志会打印用户 query 原文**（`answer_cache.py:200`），进 ELK 即轻度 PII 泄漏；且异步 worker 日志无 request_id，跨服务无法按用户排障。

- **新增一条你没列的登出漏洞（严重）**：access token 过期（>15 分钟）后点「退出登录」，`auth.py:170-176` 依赖 `current_user` 先抛 401 → refresh 撤销与 delete_cookie 一行都不执行，refresh cookie 仍在浏览器且 DB `revoked=false`。图书馆/机房共享电脑上，下一个人打开站点，前端挂载即静默 refresh **直接进入上一个人的账号**，可读全部聊天/错题/画像。修法：logout 改为不依赖有效 access token（先按 cookie 无条件撤销 + delete_cookie，再尽力拉黑 jti），0.5 人天。

**已验证扎实、不必返工的部分**（省得你花时间重查）：RLS 多租户隔离（FORCE + NULLIF 兜底 + 事务级 SET LOCAL + NOSUPERUSER 角色）、refresh 轮换/重放检测/sha256 入库、越权面（抽查 8 个路由均有显式 owner/tenant 校验非仅靠 RLS，无自助提权接口）、答案缓存**跨租户不泄漏**（tenant+kb+version 三重 filter，查缓存前已做 PG 可见性校验）、客观题**规则精确匹配非模型判分**、苏格拉底禁答是**状态机 + 代码强制注入禁答令**（非仅提示词，越狱话术打不穿）、XSS/KaTeX 流式渲染安全（仅两处 dangerouslySetInnerHTML 均为 KaTeX 输出、文本走 escape）、Redis 键 TTL 治理**整体达标**（限流/预算/缓存/黑名单/机构缓存全部有界，唯一无 TTL 的 clock 偏移键是单键设计使然）、邀请码熵值 39.6bit 爆破不可行、迁移有 downgrade、审计时间戳走真时钟不被虚拟时钟污染。

---

## 三、上线前两周排期（500 人内测最短路径）

原则：先堵「钱 / 法 / 命」三条止血线（成本被打穿、删除权违法、未成年内容），再补「运营能救」，最后收 UX。按此顺序即使中途叫停，已完成的部分也是价值最高的。

**Week 1 — 止血（钱 + 法 + 安全边界）**

| 天 | 任务 | 对应发现 | 人天 |
|---|---|---|---|
| D1 | JWT 启动强校验 + secret/4 个 LLM key 轮换走 secrets；prod 关 `/api/docs`、admin router 包 prod 开关 + 审计日志；logout 修 refresh 不吊销 | 修正#2、P2-10、登出漏洞 | 2 |
| D2 | 反代 `--proxy-headers` 收敛 CIDR + login 账号维度桶 + 失败日志 + 锁定；请求体大小闸（Caddy + 中间件） | P0-1、P1-1、P1-5(auth) | 2 |
| D3 | 全局日成本熔断（Redis `budget:global`，软/硬/熔断三档）；BudgetExceeded → 429 | P0-2(b)、P1-9 | 1.5 |
| D4 | worker 建共享带预算/计量 gateway，extraction 补 tenant/user；WorkerSettings `max_tries=1` | P0-2(a)、P1-7 | 2 |
| D5 | chunk 数配额（doc + user）+ 摄取超时兜底 reap cron + 配额排除 failed | P0-2(c)、P1-6 | 2 |

**Week 2 — 合规删除权 + 输出安全 + 运营能救**

| 天 | 任务 | 对应发现 | 人天 |
|---|---|---|---|
| D6 | 输出侧分句缓冲审核接入 SSE + web 结果进上下文前过审 | P0-5(输出) | 3（跨 D6-D7） |
| D7 | 未成年模式最小版：注册年龄声明 + 监护同意入库 + 未成年更严阈值/时段 | P0-5(未成年) | 承 D6 |
| D8 | 注销接口 + 8 表加 FK CASCADE + purge_tenant job（清 PG/Qdrant/对象/Redis） | P0-4(注销) | 承 D9 |
| D9 | checkpoint 归属改造（thread 绑 session）+ 删会话联动删 checkpoint + 30 天 cron；删文档自增 kb_version + 缓存 delete_by_kb | P0-4(checkpoint)、P0-3 | 3（跨 D8-D9） |
| D10 | 封号能力 + SSE 断线落半截答案（finally 持久化 + 计费）+ 健康检查返 503 + 缓存作用域改 kb.owner | P1-3、P0-6、P1-2、P1-11 | 3 |

**合计约 20.5 人天**；2 名后端 + 1 名全栈两周内可覆盖到 D10。缓冲/联调 + 数据导出（P1-8）、迁移拆独立 job（P1-14）、反馈通道（P2-1）、维护模式（P2-2）滚入内测第一周热修。

**明确排除在两周外的**（需产品/法务决策，非工程能独立闭环）：HTTPS/ICP/算法备案/隐私政策/用户协议、密码找回、邮箱验证真实投递、监控告警（Langfuse/Prometheus）、备份恢复、真人有效性验证——这些是你已知清单里的，工程侧只能预留接口。

---

**三个必须写进决策的数字**：
1. 当前预算体系理论月上限 **¥75,000**（500×¥5×30），是中性预期的 **35 倍**；开放注册下上界是 ∞。全局成本熔断（D3）不是锦上添花，是唯一有效止损。
2. 中性成本里约 **45% 在看板不可见**（rerank/embedding ¥640 + worker ¥340），看板显示 ≈¥1,200 而真实账单 ≈¥2,200。
3. 攻击成本效率 **¥368/账号、注册成本≈0**，是所有条目里唯一能「一个下午造成四位数损失」的路径。
