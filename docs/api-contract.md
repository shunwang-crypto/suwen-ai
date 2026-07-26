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

## 规划中（占位，实现前须先在此登记冻结）

- M2: `/api/kb` CRUD、`/api/kb/{id}/documents` 上传/列表/删除/重建索引、
  `/api/kb/{id}/debug/retrieve` 检索调试、`/api/chat` 增加 kb_id 与 citation 事件
- M3: `/api/threads/{thread_id}/resume`（interrupt 恢复协议）、`/api/quiz`、`/api/plans`
- M4: `/api/profile`（AI 眼中的我）、`/api/errors`（错题本）、`/api/reports`、
  `/api/admin/usage`、`/api/admin/time-travel`
