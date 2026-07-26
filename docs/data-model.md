# 数据模型（M0 契约 · 冻结后只增不改）

权威定义在 `apps/api/src/eduagents_api/models.py`（SQLAlchemy 2.0 ORM），
本文档描述语义与约束。首个迁移 `alembic/versions/0001_initial_schema.py`。

## 租户语义（全项目唯一定义 · Wave3 起泛化, D14）
`tenant_id = owner_id`。个人数据 owner_id = user_id（个人即租户）；机构库
（`knowledge_bases.owner_type='org'`）owner_id = `orgs.id`，其下游 documents/chunks
与 Qdrant payload 沿用同一 owner_id。物理列名保留 `tenant_id` 不改（存量零回填、
向量零迁移，见 decisions.md D14）。`organizations` 表与 `users.org_id` 为 M0 遗留
占位（无读写路径）；Wave3 实际机构模型是 `orgs` + `org_members`。
所有租户表带 `tenant_id UUID NOT NULL` + 索引。

## RLS（行级安全）
- 租户表清单：`models.TENANT_TABLES`（16 张）。
- 每张 `ENABLE ROW LEVEL SECURITY` + `FORCE`（owner 也受约束）。
- 策略：`tenant_id = current_setting('app.tenant_id', true)::uuid`；
  GUC 未设置时 current_setting 返回 NULL → 默认拒绝一切行。
- 注入方式：应用层每个事务内 `SELECT set_config('app.tenant_id', :tid, true)`
  （见 `db.tenant_session`）；**必须事务级**，防 asyncpg 连接池复用泄漏（U-A1）。
- Wave3 三 GUC（0002，D14）：`app.tenant_id` + `app.org_ids`（成员机构逗号表）+
  `app.is_admin`。`knowledge_bases/documents/chunks` 策略拆分为
  `tenant_read`（owner 匹配 **OR** owner ∈ org_ids，成员只读挂载）与
  `tenant_write`（仅 owner 匹配）；org 库写事务由 service 校验 org admin 后以
  `tenant_session(org_id)` 开启（worker 摄取同构）。
- `orgs`/`org_members` 自身 RLS：成员可读本机构行与同组名单；INSERT 仅限
  `created_by/user_id = app.tenant_id`；凭邀请码 join 的跨行查询走事务级
  `app.is_admin`（D7 惯例）；无 UPDATE/DELETE 策略（默认拒绝）。
- 例外：`usage_log` 的读策略放行 `app.is_admin='true'`（运营面板，D7）。
- 0003 补 `org_members` 的 UPDATE/DELETE 策略（P1-4）：DELETE 放行
  `user_id = app.tenant_id`（成员退出删自己行）**或** `app.is_admin`（org admin
  踢人；service 层先校验 admin 身份再以事务级 is_admin 开事务——同表自引用
  子查询会触发 RLS 递归，故沿用 D7 惯例）；UPDATE 仅 `app.is_admin`（预留改
  角色）。`orgs` 补 UPDATE 策略（邀请码轮换）：`created_by = app.tenant_id OR
  app.is_admin`。
- 非 RLS 表：users、refresh_tokens、organizations、eval_runs、pending_cleanup
  （认证层/全局层/内部运维层）。

## 用户数据生命周期（0003，P0-3/P0-4/P1-5）
- **user_id 外键**：`attempts / chat_sessions / error_items / mastery_log /
  mastery_records / plans / quizzes / org_members` 8 张表 `FK(users.id) ON DELETE
  CASCADE`——注销 = `DELETE FROM users` 一行，学情/会话/成员关系级联清空
  （chat_messages/questions/plan_adjustments 经既有二级外键继续级联）。
  **usage_log 是唯一例外：`ON DELETE SET NULL`（user_id 可空）**。理由：成本
  对账/审计数据必须留档（日志留存义务 + 与上游供应商账单核对），CASCADE 会把
  已发生的成本抹掉；SET NULL 在注销事务内自动去标识化（users 行删除后 email 等
  身份信息不存在，残留 tenant_id UUID 无法回指自然人），同时保留行级 token/费用。
  ORM 侧不声明这些 FK（models.py 注释）：0001 从零重放用 create_all(当前元数据)，
  若进 ORM 会在 0001 提前建出约束与 0003 相撞。
- **注销路径**（`DELETE /api/account`，重输密码）：单事务 = 删个人
  knowledge_bases（级联 documents/chunks/knowledge_components/kc_edges）+ 删
  users 行（级联上表）+ 写 `pending_cleanup(kind=tenant)`；worker 消费后清
  Qdrant 两 collection / 对象存储前缀 / Redis 前缀（profile_summary/budget/
  reflect/orgs/export）/ checkpoint 三表（`thread_id LIKE '%:{uid}:%'`）。
- **缓存失效**（P0-3）：删文档/重建索引在删除事务内 `kb_version = kb_version+1`
  （答案缓存 L1 键与 L2 filter 同时落空）；删库额外调 `AnswerCache.delete_by_kb`
  清 L2 点位。
- **导出**（P1-8）：`POST /api/account/export` → worker 打包 12 张表 JSON +
  chunk 文本 + 上传清单 → `exports/{uid}/{ts}.zip`，`GET /api/account/export/
  download` 经 API 流式转发。

## 表清单

### 认证与账户
| 表 | 关键字段 | 说明 |
|---|---|---|
| users | email(uniq), password_hash(bcrypt≥12), role(student/teacher/admin), org_id(预留), status(active/disabled, 0003 P1-3), birth_year+guardian_consent(0003, 未成年线), plan(free/…)+daily_budget_cny_override(0003, P2-14 套餐骨架) | status≠active → 403 ACCOUNT_DISABLED（deps 60s 缓存） |
| refresh_tokens | token_hash(uniq,sha256), expires_at, revoked, rotated_from | 轮换单次有效；重放→撤销全家 |
| organizations | name | M0 遗留占位（勿用） |
| pending_cleanup | kind(doc/kb/tenant), payload jsonb, attempts, done | 删除 outbox（0003 P1-5）：与删除事务同提交，worker 每分钟消费；无 RLS（内部运维表） |

### 机构（Wave3 企业后台共享知识库，0002）
| 表 | 关键字段 | 说明 |
|---|---|---|
| orgs | name, invite_code(uniq, 8位免混淆码), created_by, storage_quota_mb(默认512) | id 即机构库 owner_id |
| org_members | (org_id, user_id) PK, role(admin/member), joined_at | 入会即挂载机构库（无 mount 表）；成员只读，仅 admin 可写 |

### 知识库（F2，M2 启用）
| 表 | 关键字段 | 说明 |
|---|---|---|
| knowledge_bases | owner_type(user/org), name, subject, kb_version, config_json | kb_version 进语义缓存键; owner_type='org' 时 tenant_id=orgs.id |
| documents | kb_id, filename, content_hash, size_bytes, status, parse_backend, error_msg, version, uploaded_by | 状态机: pending→parsing→chunking→embedding→ready→enriching→enriched / failed; uploaded_by=最小审计 |
| chunks | doc_id, parent_id(父子分块), text, context_prefix, page_start/end, heading_path, kc_ids[], content_hash, embedding_status | 子块命中、父块喂 LLM |

### 知识点体系（§5.8，M2）
| 表 | 关键字段 | 说明 |
|---|---|---|
| knowledge_components | kb_id, canonical_name, aliases[], description | 受控词表；kc_id 是全系统外键 |
| kc_edges | from_kc, to_kc, type=prerequisite | 先修 DAG，环检测在应用层 |

### 学习者状态引擎 VLSE（F8/§5.6，M4）
| 表 | 关键字段 | 说明 |
|---|---|---|
| mastery_records | user_id+kc_id(uniq), p_mastery, confidence, fsrs_card_json, last_seen | BKT 状态 |
| mastery_log | old_p, new_p, evidence_type, evidence_id, weight | 每次变化可审计（F8-A3） |

### 测评与错题（F5/F6，M3/M4）
| 表 | 关键字段 | 说明 |
|---|---|---|
| quizzes | user_id, kb_id, blueprint_json, status | Blueprint 先确认后生成 |
| questions | quiz_id, type, stem, options_json, answer, explanation, kc_ids[], source_chunk_id, difficulty | 考点出处可溯源（F5-A2） |
| attempts | question_id, response, is_correct, llm_score, scored_reason | 主观题 0.5 权重进 BKT |
| error_items | question_id, kc_ids[], cause(五分类), state(new→reviewing→mastered→archived), variant_of, fsrs_card_json | |

### 规划（F3，M3）
| 表 | 关键字段 | 说明 |
|---|---|---|
| plans | goal, budget_hours, structure_json, version | 结构含阶段→周→日 |
| plan_adjustments | plan_id, reason, diff_json | 每次调整附理由（F3-A3） |

### 会话与观测
| 表 | 关键字段 | 说明 |
|---|---|---|
| chat_sessions | user_id, title | thread_id = user_id:session_id（M3） |
| chat_messages | session_id, role, content, meta_json | 展示层历史（D4） |
| usage_log | request_id, user_id(可空, 注销后 SET NULL 留审计), feature, model, tier, prompt/completion/cached_tokens, cost_cny, latency_ms, mode | 三维归因（F10-A1） |
| eval_runs | config_json, metrics_json, corpus_version, golden_set_version | 黄金集版本绑定 |
