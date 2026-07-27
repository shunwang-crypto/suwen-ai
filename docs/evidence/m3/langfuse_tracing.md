# M3-D1: Langfuse 自托管 + 完整 trace 树 (2026-07-27)

Docker 代理修复后解锁。自托管 + SDK 接线 + 真实流量 trace 三步全通。

## 1. 自托管部署 (deploy/langfuse/)

改编官方 docker-compose（v3 系）：langfuse-web/worker + PG17 + ClickHouse 25.12 +
Redis7 + MinIO 六容器；web 仅发布 127.0.0.1:13000，其余全内网；TELEMETRY_ENABLED=false；
密钥全随机走 .env（gitignored，模板 .env.example）；`LANGFUSE_INIT_*` 无头初始化
org/project/API keys/管理账号，零 UI 手工步骤。

```
langfuse-clickhouse-1        Up (healthy)
langfuse-langfuse-web-1      Up (healthy)      # /api/public/health = 200
langfuse-langfuse-worker-1   Up
langfuse-minio-1             Up (healthy)
langfuse-postgres-1          Up (healthy)
langfuse-redis-1             Up (healthy)
```

API key 对经 `/api/public/projects` 验证有效（项目 eduagents-dev / 组织 EduAgents）。

## 2. SDK 接线（langfuse==4.14.1, OTel 后台批量导出）

- 汇点：`packages/core/.../gateway/openai_gateway.py::_emit_usage` —— chat/stream/
  断流结算/stub/replay 所有完成调用唯一出口，API 与 worker 全覆盖；
  `chat.py` 在 qa 图完成后把 `state.trace` 阶段挂为 span 树。
- 门控：LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST 三者齐全才启用；未启用不 import、
  热路径一次布尔短路；初始化失败只试一次。flush 挂 API lifespan 与 worker on_shutdown。
- **PII 纪律（P2-7）**：prompt/completion 默认只上 `<pii:hash8:len=N>` 指纹
  （LANGFUSE_LOG_CONTENT=1 才上原文）；user/tenant 一律 sha256 前 16 位；
  画像键（weak_points/mastery）无条件只上数量。
- 单测 10 个（mock、零网络）：未配置零开销 / 字段正确性 / 上报异常不影响业务。
  合入后 tests/unit 全量 412 passed。

## 3. 真实流量验证（截图见本目录）

28000 端口起带 LANGFUSE env 的 API，demo 账号发 4 条真实请求（3 条通用 + 1 条绑
高等数学讲义库）：

- `lf-2-traces.png`：traces 列表 —— chat.qa / gateway.chat ×3 / gateway.qa_rewrite,
  Input/Output 列全部为 pii 指纹，无原文出境。
- `lf-3-trace-detail.png`：chat.qa trace 详情 —— 完整 span 树
  （qa_query_plan → qa_rewrite ×2 → qa_generate → qa_followups + 9 个 chat.qa.* 阶段），
  token 聚合 3,941 prompt → 385 completion (Σ4,326)，latency 2.85s，User ID 哈希
  `u_7ff2f72858313dff`，metadata 含 feature/mode=stub(如实标注)/requested_tier/
  used_tier/degraded/cache_hit/cost_cny/latency_ms/request_id（可与 usage_log 对账）。

## 4. 附带解锁: U-A2 容器重启数据完整

`docker compose restart postgres api` 后 `/ready` 恢复 200，重启前注册的
m1-validate@eduagents.cn 登录成功（命名卷持久化生效）。

## 运维入口

- 看板: http://127.0.0.1:13000 （账号见 deploy/langfuse/.env）
- 应用侧启用: .env 配 LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY 即可，删掉即整体关闭
