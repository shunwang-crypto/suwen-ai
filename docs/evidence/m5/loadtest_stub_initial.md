# M5 压测初测报告 — stub 档 (缩水规模)

> **规模声明 (如实)**: 本轮是 **30 并发 / 90 秒** 的缩水规模初测, 不是口径定义的
> stub 档正式规模 (100 并发阶梯 / 5 分钟)。缩水原因: 本机 8G 内存上同时跑着
> PG / Redis / Qdrant / 8000 replay 实例 / 8001 压测实例 / locust, 100 并发有 OOM
> 风险。完整 100 并发阶梯留终版在专用环境跑。本报告数字仅代表 30 并发下的表现。

## 环境

| 项 | 值 |
| --- | --- |
| 执行时间 | 2026-07-27 00:38 (+08:00) |
| 机器 | Intel Core i7-14650HX, 12 vCPU (WSL2), 7.6 GiB RAM |
| 平台 | WSL2 (Linux 6.6.87.2-microsoft-standard) |
| 被测实例 | uvicorn 单进程, 端口 8001, `GATEWAY_MODE=stub EMBEDDING_MODE=stub RERANK_MODE=stub` |
| 限流 | 压测实例调高 (`AUTH=1000/min, API=100000/min`), 本轮不测限流器 |
| 依赖 | PG 5432 / Redis 6379 / Qdrant 6333 (与 8000 replay 实例共用) |
| 压测器 | Locust 2.46.2, HttpUser, 同机发压 |
| 档位 | 30 users, spawn rate 5/s, 90s |
| 账号池 | 10 个预置账号 (stub embedding 摄取 calculus.md, 预签 token) |
| 任务权重 | 70 知识库问答(SSE) / 15 会话列表 / 10 画像 / 5 复习队列 |

## 四个关键数字

| 指标 | 值 |
| --- | --- |
| **TTFE p95** | **87 ms** (p50 = 25 ms) |
| **TTFT p95** | **160 ms** (p50 = 81 ms) |
| **RPS** | HTTP 合计 **13.7 req/s** (其中 chat SSE 9.8/s; 1222 个 HTTP 请求 / 90s) |
| **错误率** | **0%** (0 失败 / 全部请求; 无 429, 无断流, 无空答案) |

TTFE p95 = 87 ms **< 500 ms 目标**, 错误率 0% **< 1% 目标** — stub 档口径在 30 并发下达标。

## 全量明细 (来自 `loadtest_stub_20260727-003839_stats.csv`)

| Type | Name | 请求数 | 失败 | p50 | p95 | p99 | avg | RPS |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SSE | chat_ttfe (首个 event 行) | 875 | 0 | 25 ms | 87 ms | 130 ms | 34.4 ms | 9.81 |
| SSE | chat_ttft (首个 token 事件) | 874 | 0 | 81 ms | 160 ms | 190 ms | 89.9 ms | 9.80 |
| SSE | chat_stream_total (整流) | 874 | 0 | 170 ms | 520 ms | 810 ms | 218.5 ms | 9.80 |
| POST | /api/chat (SSE, 至响应头) | 874 | 0 | 16 ms | 37 ms | 53 ms | 19.0 ms | 9.80 |
| GET | /api/chat/sessions | 176 | 0 | 12 ms | 32 ms | 50 ms | 15.2 ms | 1.97 |
| GET | /api/profile | 110 | 0 | 16 ms | 37 ms | 48 ms | 18.6 ms | 1.23 |
| GET | /api/reviews/today | 62 | 0 | 12 ms | 33 ms | 44 ms | 15.0 ms | 0.70 |

注: locust 汇总行的 43.1 RPS 含 3 条自定义 SSE 指标的重复计数 (每次 chat 上报
ttfe/ttft/total 三条), **真实 HTTP RPS 按 1222/90 ≈ 13.7 计**, 报告口径以此为准。

## 测什么 / 不测什么 (本轮)

- **测**: API 层 (FastAPI + auth + 会话读写) + LangGraph qa 子图编排 + 真实混合检索
  (Qdrant dense+sparse+RRF+rerank, stub 向量口径) + SSE 通道逐事件下发开销。
  冒烟确认单次流完整: node_update/trace_event ×N → 5 citation → 19 token → followups → done。
- **不测**: 上游 LLM / embedding 供应商延迟 (stub 网关零网络), 限流器 (阈值已调高),
  前端渲染。live 档 (10 并发恒定, 真实网关) 另测, 见 `loadtest/README.md`。

## 观察

1. TTFE (34 ms avg) 与 TTFT (90 ms avg) 之间 ~55 ms 差 = qa 子图 router→retrieve→
   (grade)→generate 各节点在 stub 网关下的纯编排+检索开销, 与设计一致
   (token 事件在图收敛后才按 24 字符/块下发)。
2. 整流 p99 = 810 ms, 尾部主要来自同机发压 + 双 API 实例共享 12 vCPU 的调度抖动;
   30 并发下无任何失败/断流, 队列未见堆积 (响应头 p95 仅 37 ms)。
3. 内存: 压测中系统 available 从 1.4 GiB 降至 ~0.9 GiB 后回升, 无 OOM; 这也印证
   100 并发需换更大内存环境再跑。

## 复现

```bash
# 1) 起 stub 档实例 (8001; 8000 的 replay 实例不动)
cd /home/shunw/eduagents && export LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib && \
GATEWAY_MODE=stub EMBEDDING_MODE=stub RERANK_MODE=stub \
AUTH_RATE_LIMIT_PER_MINUTE=1000 API_RATE_LIMIT_PER_MINUTE=100000 \
/home/shunw/miniforge3/envs/eduagents/bin/python -m uvicorn eduagents_api.main:app --port 8001
# 2) 种账号池 (与压测实例同 stub env, 幂等)
GATEWAY_MODE=stub EMBEDDING_MODE=stub RERANK_MODE=stub \
  /home/shunw/miniforge3/envs/eduagents/bin/python scripts/seed_loadtest.py --count 10
# 3) 压测
USERS=30 SPAWN_RATE=5 RUN_TIME=90s bash scripts/run_loadtest.sh
```

原始产物: `loadtest_stub_20260727-003839_{stats,failures,stats_history}.csv` + 同名 `.html`。
