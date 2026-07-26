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

## 快速开始（本机开发, 无 Docker 依赖）

```bash
# 0) conda 环境 (已建好: eduagents / Python 3.12, pip 走清华源)
# 1) 依赖
make install
# 2) 本地基础设施 (conda 版 PostgreSQL 18 + Redis, 幂等)
make dev-infra
# 3) 迁移 (从零建 21 张表 + RLS + edu_app 应用角色)
make migrate
# 4) 起 API
make api          # http://127.0.0.1:8000/api/docs
# 5) 测试
make test-unit          # 69 个, 无外部依赖
make test-integration   # 5 个, 需要 dev-infra (含 RAG e2e/越权/认证全链路)
# 6) 演示数据 + 检索评估
make seed               # demo 账号 + 高数知识库 + 摄取语料 (输出 tenant/kb id)
make eval-retrieval TENANT=<tenant_id> KB=<kb_id>   # 黄金集评估 -> docs/evidence/m2/
```

## 模型网关模式（.env `GATEWAY_MODE`）

| 模式 | 行为 | 用途 |
|---|---|---|
| stub | 确定性开发桩, 带「非真实模型输出」横幅 | 开发/CI, 零成本零网络 |
| live | 真实调用 (DeepSeek/SiliconFlow), 重试+熔断+三档降级链 | 生产/演示 |
| record | live + 录制到 cassettes/ | 录制黄金演示路径 |
| replay | 只回放录制 (可选回退 stub) | 断网演示 / CI 回归 |

当前默认 `stub`（用户指示：开发期不真实调用）。切 `live` 即用 .env 中的 DeepSeek key。

## Docker 修复（需要 sudo, 一次性）

本机 Docker daemon 的 DNS 被污染（docker.io 解析到错误 IPv6）。shell 网络本身正常。
执行以下两条命令后 `make up` 即可用完整容器栈：

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
tests/            unit (22) + integration (3, 越权/RLS/SSE/认证全链路)
docs/             data-model / api-contract / decisions / evidence/
prompts/ eval/ loadtest/   (M2+ 填充)
```

## 与直接问 ChatGPT 的区别（答辩标准答案）

ChatGPT 优化的是**这一轮对话**；EduAgents 优化的是**这一个学生**——
学情闭环（时间维度的状态积累）+ 私域知识库（可溯源）+ 掌握度驱动的个性化，三点组合。
