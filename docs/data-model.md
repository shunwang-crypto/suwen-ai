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
- 非 RLS 表：users、refresh_tokens、organizations、eval_runs（认证层/全局层）。

## 表清单

### 认证与账户
| 表 | 关键字段 | 说明 |
|---|---|---|
| users | email(uniq), password_hash(bcrypt≥12), role(student/teacher/admin), org_id(预留) | |
| refresh_tokens | token_hash(uniq,sha256), expires_at, revoked, rotated_from | 轮换单次有效；重放→撤销全家 |
| organizations | name | M0 遗留占位（勿用） |

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
| usage_log | request_id, user_id, feature, model, tier, prompt/completion/cached_tokens, cost_cny, latency_ms, mode | 三维归因（F10-A1） |
| eval_runs | config_json, metrics_json, corpus_version, golden_set_version | 黄金集版本绑定 |
