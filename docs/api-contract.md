# API 契约（M0 初版 · M1 冻结后只增不改）

约定：资源命名 `/api/{resource}`；错误统一信封 `{code, message, request_id}`；
鉴权三档：公开 / 用户(Bearer) / 管理员。SSE 端点用 fetch+ReadableStream 消费
（原生 EventSource 无法带 Authorization 头）。

## 已实现（M1）

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
| POST | /api/chat | {message, session_id?} → SSE 流（见下）；无 session_id 自动建会话 |
| GET | /api/chat/sessions | 会话列表（按更新时间倒序，≤100） |
| GET | /api/chat/sessions/{id}/messages | 历史消息（他人会话 → 404，不泄漏存在性） |
| PATCH | /api/chat/sessions/{id} | {title} 重命名 |
| DELETE | /api/chat/sessions/{id} | → 204，级联删消息 |

### 系统（公开）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/health | {status, gateway_mode, checks:{postgres, redis}} |
| GET | /api/docs | OpenAPI 文档 |

## SSE 事件协议（七类，M1 实现四类，M3 补全）

```json
{"event":"token","data":{"text":"泰勒"}}
{"event":"node_update","data":{"node":"generate","status":"running","detail":"生成回答中"}}
{"event":"trace_event","data":{"type":"router","route":"kb","reason":"…"}}          // M3
{"event":"citation","data":{"idx":1,"doc_id":"…","chunk_id":"…","page":42,"highlight":"…","doc_name":"…"}}  // M2
{"event":"interrupt","data":{"kind":"blueprint_confirm","payload":{},"resume_url":"/api/threads/{id}/resume"}}  // M3
{"event":"error","data":{"code":"GENERATION_FAILED","message":"…"}}
{"event":"done","data":{"session_id":"…","usage":{"cost_cny":0.002,"mode":"live","latency_ms":1234}}}
```

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
| GET | /api/quiz · /api/quiz/{id} | 卷列表 / 题目详情（kc_ids 已归一化） |
| POST | /api/quiz/{id}/attempts | {answers} → 判分（客观本地/简答 LLM 附理由）+ 错题自动入本 + BKT 直写 + mastery_log 审计；正确率<0.6 时附 `plan_adjustment`（自动补强，F3-A3） |
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
| GET | /api/errors/stats | 聚合：{total, by_cause(五分类补零), by_state, by_kc(top10)} |
| POST | /api/errors/{id}/archive | 手动归档：state→archived，退出今日复习队列 |
| POST | /api/errors/{id}/cause | {cause} 错因修正（五分类之一，越界 422） |

> 错因自动分类：`POST /api/reviews/{id}/answer` 判错时以 cheap 档 json_mode 重标错因并回传 `cause`；
> 网关 stub 模式不发请求、异常一律回落 `knowledge_gap`（入本初值），绝不冒充分类结果。

## 规划中（占位，实现前须先在此登记冻结）

- M3: `/api/threads/{thread_id}/resume`（interrupt 恢复协议）、`/api/quiz`、`/api/plans`
- M4: `/api/profile`（AI 眼中的我）、`/api/reports`、
  `/api/admin/usage`、`/api/admin/time-travel`
