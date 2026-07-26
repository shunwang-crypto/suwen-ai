# EduAgents 压测包 (M5)

Locust 驱动的 SSE 压测。目标端点是 `POST /api/chat` (带 `kb_id` 走 qa 子图, sse_starlette 流式返回),
配套三个只读端点构成"已登录学生"的行为画像。

```
loadtest/
  locustfile.py         # 压测画像 + TTFE/TTFT 手动计时与自定义事件上报
  accounts.json         # stub 档账号池 (seed_loadtest.py 生成; 含预签 token, 已 gitignore)
  accounts_live.json    # live 档账号池 (可选, 同上)
  README.md             # 本文件
scripts/
  seed_loadtest.py  # 幂等种子: N 个账号 + 各自知识库 + 摄取语料 + 预签 access token
  run_loadtest.sh   # 健康检查 + gateway_mode 断言 + headless locust
```

依赖: `locust 2.46.2` (已装在 `/home/shunw/miniforge3/envs/eduagents`)。

---

## 一、两档口径

| | **stub 档 (容量档)** | **live 档 (体验档)** |
|---|---|---|
| `GATEWAY_MODE` | `stub` | `live` |
| `EMBEDDING_MODE` / `RERANK_MODE` | `stub` / `stub` | `live` / `live` |
| 限流 | 故意调高 (auth 1000, api 100000) | 故意调高 (同左) |
| 并发 | 100 并发阶梯 (30 → 60 → 100) | 10 并发恒定 |
| 时长 | 每档 90s ~ 180s | 300s |
| 主指标 | **TTFE p95 < 500ms**, 错误率 < 1% | **TTFT p95 < 3s**, 断流率 = 0 |
| 端口 | 8001 (专用实例) | 8002 (专用实例) |
| 账号池 | `loadtest/accounts.json` | `loadtest/accounts_live.json` |
| 意义 | 系统自身能扛多少并发 | 真实用户体感有多快 |

两档都必须起**独立的 API 实例**。`GATEWAY_MODE` / `EMBEDDING_MODE` 是进程启动时读的配置,
不重启进程无法切换; 8000 端口上正在跑的 replay 实例是评估用的, **不要动它**。

---

## 二、完整运行命令

### 0) 公共前置: 基础设施

```bash
make dev-infra          # 本地 conda 版 PG/Redis
make migrate            # alembic upgrade head
```

### 1) stub 档

**起 API (终端 A, 仓库根目录):**

```bash
cd /home/shunw/eduagents
LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib \
GATEWAY_MODE=stub EMBEDDING_MODE=stub RERANK_MODE=stub \
AUTH_RATE_LIMIT_PER_MINUTE=1000 API_RATE_LIMIT_PER_MINUTE=100000 \
  /home/shunw/miniforge3/envs/eduagents/bin/python -m uvicorn eduagents_api.main:app --port 8001
```

`LD_LIBRARY_PATH` 必须带 (conda libstdc++ 优先, pymupdf/qdrant ABI 冲突, 见 `tests/conftest.py`)。

**种子账号池 (终端 B, 只需一次, 幂等):**

```bash
cd /home/shunw/eduagents
LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib \
GATEWAY_MODE=stub EMBEDDING_MODE=stub RERANK_MODE=stub \
  /home/shunw/miniforge3/envs/eduagents/bin/python scripts/seed_loadtest.py \
    --count 10 --corpus corpus/calculus.md --out loadtest/accounts.json
```

> **种子必须与被压测的 API 实例同档 (env 一致)。** 仓库根 `.env` 里 `EMBEDDING_MODE` 默认是
> `live`, 直接裸跑会用真实 embedding 把语料向量写进 Qdrant; 而 stub 档 API 的查询向量由 stub
> 生成 —— 两边落在完全不同的向量空间里, 召回近似随机, 测出来的 TTFE/TTFT 不再对应真实检索路径。
> 所以 **stub 档和 live 档要各种一份账号池**, 用 `--out` 分开存, 跑 locust 时用
> `LOADTEST_ACCOUNTS` 环境变量指定。`accounts.json` 顶层的 `settings` 字段就是用来记录
> "这批向量是哪种 embedding 生成的"; `scripts/run_loadtest.sh` 会读它, 发现不是 `stub` 时
> 打警告并提示重种 (只警告不拦截 —— 有时你就是想在旧向量上看纯 SSE 通道开销)。
> `gateway_mode != stub` 则是硬拦截, 直接退出。
>
> JWT 部分没有这个问题: 预签 token 只依赖 `JWT_SECRET` / `JWT_ALGORITHM`, 两档一致即可。

**跑压测 (终端 B):**

```bash
# 一把梭 (带健康检查 + gateway_mode 断言 + 账号池检查)
USERS=30  SPAWN_RATE=5  RUN_TIME=90s  bash scripts/run_loadtest.sh
USERS=60  SPAWN_RATE=10 RUN_TIME=90s  bash scripts/run_loadtest.sh
USERS=100 SPAWN_RATE=10 RUN_TIME=180s bash scripts/run_loadtest.sh
```

等价的裸 locust 命令:

```bash
cd /home/shunw/eduagents && mkdir -p docs/evidence/m5
/home/shunw/miniforge3/envs/eduagents/bin/locust -f loadtest/locustfile.py --headless \
  -u 100 -r 10 -t 180s --host http://127.0.0.1:8001 \
  --csv docs/evidence/m5/loadtest_stub_$(date +%Y%m%d-%H%M%S) --csv-full-history \
  --html docs/evidence/m5/loadtest_stub_$(date +%Y%m%d-%H%M%S).html --only-summary
```

### 2) live 档

**起 API (终端 A):**

```bash
cd /home/shunw/eduagents
LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib \
GATEWAY_MODE=live EMBEDDING_MODE=live RERANK_MODE=live \
AUTH_RATE_LIMIT_PER_MINUTE=1000 API_RATE_LIMIT_PER_MINUTE=100000 \
  /home/shunw/miniforge3/envs/eduagents/bin/python -m uvicorn eduagents_api.main:app --port 8002
```

**种子 live 档账号池 (终端 B, 单独一份, 真实 embedding 会调外部供应商):**

```bash
cd /home/shunw/eduagents
LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib \
GATEWAY_MODE=live EMBEDDING_MODE=live RERANK_MODE=live \
  /home/shunw/miniforge3/envs/eduagents/bin/python scripts/seed_loadtest.py \
    --count 10 --corpus corpus/calculus.md --out loadtest/accounts_live.json
```

**跑压测 (终端 B):** `run_loadtest.sh` 会断言 `gateway_mode == stub`, live 档请直接用裸命令,
并用 `LOADTEST_ACCOUNTS` 指向 live 档账号池:

```bash
cd /home/shunw/eduagents && mkdir -p docs/evidence/m5
LOADTEST_ACCOUNTS=loadtest/accounts_live.json \
/home/shunw/miniforge3/envs/eduagents/bin/locust -f loadtest/locustfile.py --headless \
  -u 10 -r 2 -t 300s --host http://127.0.0.1:8002 \
  --csv docs/evidence/m5/loadtest_live_$(date +%Y%m%d-%H%M%S) --csv-full-history \
  --html docs/evidence/m5/loadtest_live_$(date +%Y%m%d-%H%M%S).html --only-summary
```

live 档并发只给 10: 真实网关有供应商侧的 QPS/TPM 配额, 打高了测出来的是"被上游限流",
而且会烧真金白银的 token。

---

## 三、测什么 / 不测什么

### stub 档 **测**的是

- FastAPI/uvicorn 请求处理与 JWT 鉴权开销
- LangGraph qa 子图的编排开销 (节点调度、状态传递、checkpoint 写入)
- 检索链路: Qdrant 向量检索 + PostgreSQL (RLS + 会话/消息写入) + Redis
- **SSE 通道自身的开销**: sse_starlette 的事件封帧、每 24 字符一块的 token 分片、
  长连接在高并发下的内存与 fd 占用
- 应用层在并发压力下的排队与退化曲线

### stub 档 **不测**

- **上游 LLM 供应商的延迟与稳定性** —— `GATEWAY_MODE=stub` 下模型响应是本地桩, 没有网络往返
- **embedding / rerank 供应商的延迟** —— `EMBEDDING_MODE=RERANK_MODE=stub`, 向量是桩生成的
- **限流器** —— 压测实例故意把 `AUTH_RATE_LIMIT_PER_MINUTE` 调到 1000、
  `API_RATE_LIMIT_PER_MINUTE` 调到 100000。压测的目的是量应用容量, 不是量限流器的准确性;
  限流行为由单测覆盖。**一旦压测中出现 429, 说明实例没按本口径起, 该轮数据作废**
  (locustfile 会在结束时 ERROR 级别打出 429 计数)
- **前端渲染** —— 只打 HTTP API, 不含 Next.js 首屏、SSE 在浏览器端的增量渲染成本
- **文档摄取链路** —— 上传/切分/向量化是 ARQ 异步任务, 由种子脚本一次性跑完, 不在压测画像里

### live 档补的是

端到端真实用户体验 (真实 LLM 首 token 延迟 + 真实 embedding 检索质量与耗时),
代价是并发只能开到 10 —— 所以 **live 档的数字不能用来推容量, stub 档的数字不能用来说体验**。
两档必须一起看。

---

## 四、TTFE 与 TTFT

带 `kb_id` 的 `/api/chat` 走 qa 子图, SSE 事件顺序固定为:

```
event: node_update    ┐
event: trace_event    ├─ 若干过程事件 (检索、自省环等)
event: citation       ┘
event: token          ← 答案正文, 每 24 字符一块, 逐块下发
event: token
...
event: done           ← 正常收尾 (出错则是 event: error)
```

| 指标 | 定义 | 含义 |
|---|---|---|
| **TTFE** (Time To First Event) | 收到**第一个** `event:` 行的时刻 − 请求发出时刻 | 页面开始有反馈 (进度条/检索中提示可以动了) |
| **TTFT** (Time To First Token) | 收到**第一个** `event: token` 行的时刻 − 请求发出时刻 | 用户开始看到答案文字 |
| **stream_total** | 整流读完 (`event: done`) 的时刻 − 请求发出时刻 | 完整答案生成耗时 |

TTFE 和 TTFT 在本系统里**差距显著** —— 中间隔着整个检索 + 自省环。只报一个数会误导:
只看 TTFE 会高估体验 (以为很快, 其实只是转圈), 只看 TTFT 会低估响应性 (以为很慢, 其实早有反馈)。

### 为什么必须手动上报

locust 原生的响应时间统计在**响应头返回时**就结束计时。SSE 是长连接, 响应头几乎是瞬时返回的
(sse_starlette 一进入 handler 就把 200 + `text/event-stream` 头发出去了), 之后所有内容才逐块流下来。
所以 locust 原生数字恒等于"握手耗时", 在 SSE 场景下**毫无意义**。

实现方式 (`loadtest/locustfile.py`): `stream=True` + `iter_lines()` 逐行读, 在关键行到达的瞬间
`time.perf_counter()` 打点, 再用 `events.request.fire(request_type="SSE", name=...)` 把三个指标
作为独立的"伪请求"上报, 从而复用 locust 的百分位统计与 CSV 导出:

```python
events.request.fire(
    request_type="SSE",
    name="chat_ttfe",            # 或 chat_ttft / chat_stream_total
    response_time=ttfe_ms,
    response_length=0,           # chat_stream_total 上报累计字节数
    exception=None,
    context={},
    response=None,
)
```

同时保留一条 `POST /api/chat (SSE)` 的原生统计作为成功率口径:
收到 `event: error`、或流结束但没收到 `event: done` (断流)、或收到 `done` 却全程无 token,
都判 `response.failure(...)`。整个流式读取被 `try/except` 包住并设 `timeout=(5, 120)`
(连接 5s / 两次读之间 120s), 异常时 fire 一个带 `exception` 的 `chat_stream_total`,
locust user 不会崩 —— 崩一个 user 就少一份并发, 后半程并发数会悄悄缩水, 数据就不可信了。

---

## 五、账号池: 唯一决定与理由

**决定: 预置账号池 + 预签 access token, `on_start` 不做注册/登录。**

1. **auth 限流是 5 次/分钟/IP** (`auth_rate_limit_per_minute=5`, register 与 login 共用同一桶)。
   压测机对 API 而言就是一个 IP。30 并发在 `on_start` 里登录, 第 6 个用户起全是 429 +
   `Retry-After` —— 最终测出来的是限流器的行为, 不是问答链路的性能。
2. **现场注册的新账号没有知识库**、没有文档, `kb_id` 为空就进不了 qa 子图,
   占 70% 权重的主任务直接空转, 压测等于白跑。
3. **注册不属于被测画像**。压测目标是"已登录学生持续问答"; 注册要做 bcrypt (rounds=12,
   每次几百毫秒) + 写 users 表, 混进来只会污染 p95, 测出来的是 bcrypt 的耗时。

`scripts/seed_loadtest.py` 用 `create_access_token(settings, user_id=..., tenant_id=..., role=...)`
预签, 与 `/api/auth/login` 签发的是同一套 payload (`sub`/`tid`/`role`/`jti`/`iat`/`exp`),
所以直接当 `Authorization: Bearer` 用即可, 完全绕开 auth 限流。

**兜底**: access token TTL 是 900s, 长时间压测或 `JWT_SECRET` 变更会导致 401。
locustfile 在任何业务请求收到 401 时**降级为一次真实 login** 重取 token,
并用 `_relogin_done` 标志限制每个 user 最多重登一次 (否则 30 个 user 同时重登会把自己打成 429)。
这是异常路径的兜底, 不是常规路径 —— 正常一轮压测里它一次都不该触发。

**账号分配**: `on_start` 用锁保护的全局自增索引对账号池取模。user 数 > 账号数时多个 user
共用一个账号 —— api 限流是按 `user_id` 的 120 次/分钟, 压测实例已调到 100000, 共用不会撞限流。

---

## 六、指标口径 (怎么读结果)

`--csv <前缀>` 会产出:

- `<前缀>_stats.csv` —— **主结果**, 每个 name 一行
- `<前缀>_failures.csv` —— 失败明细 (断流、error 事件、429 都在这里)
- `<前缀>_stats_history.csv` —— `--csv-full-history` 下的逐秒时间序列, 用来画退化曲线
- `<前缀>.html` —— 图表报告

**p50 / p95 从 `_stats.csv` 的对应行读取**, 关注这三行 (Type 列均为 `SSE`):

| Name | 读哪几列 | 目标 |
|---|---|---|
| `chat_ttfe` | `50%`, `95%`, `Request Count` | stub 档 p95 < 500ms |
| `chat_ttft` | `50%`, `95%` | live 档 p95 < 3s |
| `chat_stream_total` | `50%`, `95%`, `Average Content Size` | 观测整流耗时与答案体量 |

成功率看 `POST /api/chat (SSE)` 那一行的 `Failure Count / Request Count`:
stub 档要求 < 1%; live 档断流 (未收到 `event: done`) 要求为 0。

快速摘取 (示例):

```bash
/home/shunw/miniforge3/envs/eduagents/bin/python - <<'PY'
import csv, glob
path = sorted(glob.glob("docs/evidence/m5/loadtest_stub_*_stats.csv"))[-1]
for row in csv.DictReader(open(path)):
    if row["Name"] in ("chat_ttfe", "chat_ttft", "chat_stream_total") or "chat" in row["Name"]:
        print(f'{row["Name"]:<24} n={row["Request Count"]:>6} '
              f'p50={row["50%"]:>7} p95={row["95%"]:>7} fail={row["Failure Count"]}')
PY
```

> 注意: locust 的百分位列名随版本略有差异 (`50%` / `95%` vs `50%ile`), 以实际表头为准。

---

## 七、任务权重 (行为画像)

| 权重 | 任务 | 端点 |
|---|---|---|
| 70 | 知识库问答 (SSE) | `POST /api/chat` (带 `kb_id`, 不带 `session_id`) |
| 15 | 会话列表 | `GET /api/chat/sessions` |
| 10 | 学习画像 | `GET /api/profile` |
| 5 | 今日复习队列 | `GET /api/reviews/today` |

问题文本从 10 条高数问题里随机取 (问法风格对齐 `eval/golden_set/v1.jsonl`)。

**每次请求都不带 `session_id`** (即每次新建会话): 复用同一 session 会让服务端把历史消息
不断拼进上下文, 请求体与 prompt 长度随压测时长单调增长 —— 那测出来的是"历史累积效应",
p95 会越跑越差, 而不是稳态问答性能。代价是会话表持续增长, 压测后按需清理
(`loadtest-%@eduagents.cn` 这批租户的数据可整体删除)。
