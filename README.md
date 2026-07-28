# EduAgents — 教育智能体平台

面向真实用户、按可演进至亿级用户的架构原则设计的教育智能体平台。
完整规格见 [docs/IMPLEMENTATION_PROMPT.md](docs/IMPLEMENTATION_PROMPT.md)；
执行状态唯一事实源：[STATUS.md](STATUS.md)；决策与偏差：[docs/decisions.md](docs/decisions.md)。

## 架构（M1 已落地部分加粗）

```
L1 前端      Next.js + shadcn/ui + KaTeX                    (M4)
L2 接入      Caddy 2                                        (compose 已配)
L3 应用      **FastAPI 全 async + JWT 轮换认证 + 租户中间件 + Redis 令牌桶限流 + SSE**
L4 编排      LangGraph 四子图 (qa/quiz/plan/tutor; 路由在 API 层, 学情诊断在服务层)
L5 模型网关  **自研 ModelGateway: 三档路由/重试/熔断/降级链/用量计量/录制回放/stub**
L6 异步      **ARQ Worker 骨架** → 摄取流水线               (M2)
L7 数据      **PostgreSQL(RLS 多租户) + Redis**; Qdrant/MinIO (M2)
L8 观测      **structlog JSON + request_id 贯穿**; Langfuse (M3)
```

## 快速启动（推荐：本机开发模式）

这是目前测试完整 Web UI 最直接的方式。默认使用 `stub` 模型网关，不需要任何模型
API Key，也不会产生模型调用费用。当前开发机请统一使用 `127.0.0.1`，不要使用
`localhost`（本机 IPv6 配置可能把它解析到不可用的 `::1`）。

环境要求：Python 3.12、Node.js 20+、npm，以及 PostgreSQL、Redis、Qdrant。
仓库当前的 `Makefile` 默认使用
`/home/shunw/miniforge3/envs/eduagents/bin/python`；在其他机器上可通过
`make PY=/path/to/python PIP=/path/to/pip <target>` 覆盖。

### 1. 首次初始化

```bash
cd /home/shunw/eduagents

# 仅在 .env 不存在时复制；不会覆盖已有配置
[ -f .env ] || cp .env.example .env

# Python 与前端依赖
make install
cd apps/web && npm ci && cd ../..

# 启动本地 PostgreSQL、Redis、Qdrant（幂等，可重复执行）
make dev-infra

# 建表/升级迁移，并写入可直接登录的演示数据
make migrate
make seed
```

`make seed` 会幂等校正两个演示账号的角色和密码：

- 学生端（含预置知识库和历史作答）：`demo@eduagents.cn` / `demo-pass-123`
- 管理端（运营、评估页面）：`admin@eduagents.cn` / `admin-pass-123`

日常学习功能请使用学生账号；管理员账号不重复持有学生语料。若本地曾手动修改
演示账号角色或密码，重新运行 `make seed` 即可恢复上述基线。

如果仓库已经存在 `.env`、依赖和数据库，日常启动只需再次执行
`make dev-infra`，无需重复安装；拉取到新迁移后再执行一次 `make migrate`。

### 2. 启动三个进程

分别打开三个终端，且都不要关闭：

终端 A — API（端口 8000）：

```bash
cd /home/shunw/eduagents
make api
```

终端 B — Worker（上传摄取、反思等异步任务）：

```bash
cd /home/shunw/eduagents
make worker
```

终端 C — Web（端口 3000）：

```bash
cd /home/shunw/eduagents/apps/web
npm run dev
```

启动后访问：

- Web：<http://127.0.0.1:3000>
- API 文档：<http://127.0.0.1:8000/api/docs>
- 健康检查：<http://127.0.0.1:8000/api/health/ready>

健康检查返回 HTTP 200 即可。三个硬依赖应为 `ok`；未启动 Worker 时状态会显示
`degraded`，但同步 API 仍可使用。

### 3. 运行回归测试

```bash
cd /home/shunw/eduagents

make test-unit          # 当前 441 个单元测试
make test-integration   # 当前 98 个集成测试；需先 make dev-infra
make lint               # Python Ruff

cd apps/web
npm run lint            # ESLint
npm run build           # TypeScript + Next.js 生产构建
```

也可在仓库根目录运行 `make test`，依次执行单元测试和集成测试。

### 4. 常见启动问题

- `Qdrant` 未就绪：先检查 `curl http://127.0.0.1:6333/healthz`。本机服务不存在时，
  可单独执行 `docker compose -f deploy/docker-compose.yml up -d qdrant`。
- 端口被占用：执行 `ss -tlnp | rg ':3000|:8000|:5432|:6379|:6333'` 查找占用进程。
- API 报缺表：重新执行 `make migrate`。
- Python 报模块不存在：重新执行 `make install`，并确认使用的是 `eduagents` 环境。
- 页面出现“开发桩”提示：这是默认 `GATEWAY_MODE=stub` 的正常行为；功能流程可测试，
  但回答不是来自真实模型。

## 模型网关模式（.env `GATEWAY_MODE`）

| 模式 | 行为 | 用途 |
|---|---|---|
| stub | 确定性开发桩, 带「非真实模型输出」横幅 | 开发/CI, 零成本零网络 |
| live | 真实调用 (DeepSeek/SiliconFlow), 重试+熔断+三档降级链 | 生产/演示 |
| record | live + 录制到 cassettes/ | 录制黄金演示路径 |
| replay | 只回放录制 (可选回退 stub) | 断网演示 / CI 回归 |

当前默认 `stub`（用户指示：开发期不真实调用）。切 `live` 即用 .env 中的 DeepSeek key。

## Docker 后端栈（可选）

本地模式和 Docker 模式二选一，不要同时启动，否则 5432/6379/6333 端口会冲突。
当前 `docker-compose.yml` 包含 PostgreSQL、Redis、Qdrant、MinIO、API、Worker 和
Caddy，**尚未包含 Next.js Web 服务**；要测试页面，仍需在宿主机执行
`cd apps/web && npm run dev`。

```bash
cd /home/shunw/eduagents
make up
docker compose -f deploy/docker-compose.yml ps
curl http://127.0.0.1:8000/api/health/ready

# 停止并保留数据卷
make down
```

如果当前开发机拉取 Docker 镜像时出现错误 IPv6/DNS，可一次性配置 daemon：

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "dns": ["223.5.5.5", "119.29.29.29"],
  "registry-mirrors": ["https://docker.m.daocloud.io", "https://docker.1ms.run"]
}
EOF
sudo service docker restart   # 或 systemctl restart docker
```

## 目录结构

```
packages/core/    领域核心: config / clock / logging / gateway (Protocol 先行)
apps/api/         FastAPI: routers(auth,chat,health) / middleware / models / db(RLS)
apps/worker/      ARQ 后台任务 (M2: 摄取流水线)
alembic/          迁移 (0001: 全表 + RLS + edu_app 角色)
deploy/           docker-compose 全套 + Caddyfile + Dockerfile
scripts/          dev_infra.sh / demo_m0.sh / demo_m1.sh
tests/            unit + integration（越权/RLS/SSE/认证/完整业务闭环）
docs/             data-model / api-contract / decisions / evidence/
prompts/ eval/ loadtest/   (M2+ 填充)
```

## 与直接问 ChatGPT 的区别（答辩标准答案）

ChatGPT 优化的是**这一轮对话**；EduAgents 优化的是**这一个学生**——
学情闭环（时间维度的状态积累）+ 私域知识库（可溯源）+ 掌握度驱动的个性化，三点组合。
