# EduAgents 从零部署手册

> 适用: 一台全新 Linux 主机 (8C16G 参考), Docker + Docker Compose v2。
> 开发机无 Docker 时的替代路径 (conda 版基础设施) 见仓库根 README「快速开始」。
> 本手册每一步都给验证命令; 跑不通就没完成。

## 0. 前置

```bash
git clone <repo> eduagents && cd eduagents
docker --version && docker compose version   # 需要 Docker 20+ / Compose v2
```

国内网络若拉不动镜像, 先配 daemon (需要 sudo, 与根 README「Docker 修复」一致):

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "dns": ["223.5.5.5", "119.29.29.29"],
  "registry-mirrors": ["https://docker.m.daocloud.io", "https://docker.1ms.run"]
}
EOF
sudo systemctl restart docker
```

## 1. 配置 .env (必填清单)

```bash
cp .env.example .env
```

| 变量 | 必填? | 说明 |
|---|---|---|
| `JWT_SECRET` | **必填** | ≥32 位随机串: `openssl rand -hex 32`; 禁止用默认值上生产 |
| `STANDARD_API_KEY` | **必填** | DeepSeek 平台 key (deepseek-chat, 交互主力) |
| `STRONG_API_KEY` | **必填** | DeepSeek key (deepseek-reasoner, 仅异步后台链路) |
| `CHEAP_API_KEY` | **必填** | SiliconFlow key (Qwen2.5-7B, 路由/校验等轻任务) |
| `EMBEDDING_API_KEY` | **必填** | SiliconFlow key (BGE-M3 + bge-reranker-v2-m3) |
| `TAVILY_API_KEY` | 建议 | 联网搜索; 缺失时自动降级纯知识库模式 (§9.3) |
| `GATEWAY_MODE` | **必填** | 生产 `live`; 断网演示 `replay`; `stub` 仅限 dev/test (decisions.md D2) |
| `APP_ENV` | **必填** | 生产必须 `prod` (dev 档会放行 stub 兜底等开发行为) |
| `EMBEDDING_MODE` / `RERANK_MODE` | **必填** | 生产 `live` |
| `QDRANT_LOCAL_PATH` | **必须留空** | 非空走单进程本地模式, 容器部署必须用 Qdrant 服务 (D11) |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | **必填** | 改掉示例值; 与 compose 中 MinIO 环境一致 |
| `BCRYPT_ROUNDS` | 默认 12 | 不要调低 |
| `AUTH_RATE_LIMIT_PER_MINUTE` 等限流/预算/上传约束 | 默认即可 | 语义见 `.env.example` 注释 |

数据库/Redis/Qdrant/MinIO 的地址在 compose 内已按服务名注入
(`deploy/docker-compose.yml` api/worker 的 environment 段), .env 中的本机地址仅供
无容器开发, 无需为容器部署修改。

## 2. 起栈

```bash
make up          # = docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps    # 全部 healthy 才算起来
curl -s http://127.0.0.1:8000/api/health/ready    # PG/Redis/Qdrant 任一挂返 503
```

服务清单: postgres:16 / redis:7 / qdrant / minio / api (FastAPI) / worker (ARQ) /
caddy (:8080 反代)。

## 3. 迁移与 seed

迁移当前内联在 api 容器启动命令里 (`alembic upgrade head &&
uvicorn ...`), 单副本下无需手动执行; 手动重放/验证:

```bash
docker compose -f deploy/docker-compose.yml exec api alembic upgrade head
docker compose -f deploy/docker-compose.yml exec api alembic current
```

> 注意: 扩多副本前必须把迁移抽成独立一次性服务 (docs/scaling.md「compose→K8s
> 迁移要点」第 1 条, 生产化审查 P1-14), 否则并发迁移抢 DDL 锁。

演示数据 (demo 账号 + 高数知识库 + 语料摄取, 幂等):

```bash
docker compose -f deploy/docker-compose.yml exec api python scripts/seed_demo.py
# 输出 demo 账号: demo@eduagents.cn / demo-pass-123 与 tenant/kb id
```

## 4. 备份 cron

备份三件套脚本已就位 (`scripts/backup.sh`: pg_dump -Fc + 对象目录 tar + Qdrant
snapshot API, 保留最近 14 份轮转; 恢复用 `scripts/restore.sh <stamp>`, 有二次确认)。

宿主机安装 cron (crontab 由你安装, 脚本不代装), 每天 03:30:

```bash
crontab -e
# 加入 (compose 部署: PG 走宿主映射端口, 对象存储用 mc mirror 见脚本头注①):
30 3 * * * /bin/bash /path/to/eduagents/scripts/backup.sh >> $HOME/.local/share/eduagents/backup.log 2>&1
```

验证: 手动跑一次 `make backup`, 确认 `~/.local/share/eduagents/backups/<stamp>/`
下有 .dump / objects tar / qdrant snapshot 三类产物与 MANIFEST.txt。

## 5. 密钥轮换步骤

`.env` 中的 4 个 LLM key 与 JWT_SECRET 属最高敏感级。轮换流程 (建议上线前执行一次,
之后按季度或人员变动时):

1. **DeepSeek**: 登录 platform.deepseek.com → API Keys → 删除旧 key → 新建 key,
   更新 `.env` 的 `STANDARD_API_KEY` / `STRONG_API_KEY`。
2. **SiliconFlow**: 登录 cloud.siliconflow.cn → API 密钥 → 重置/新建, 更新
   `CHEAP_API_KEY` / `EMBEDDING_API_KEY`。
3. **Tavily**: app.tavily.com → API Keys 同理, 更新 `TAVILY_API_KEY`。
4. **JWT_SECRET**: `openssl rand -hex 32` 生成新值写入 `.env`。注意: 轮换即全员
   下线 (存量 access/refresh token 全部失效), 选低峰执行。
5. 生效: `docker compose -f deploy/docker-compose.yml up -d api worker`
   (env_file 重读)。验证: `curl -s localhost:8000/api/health` + 发一条真实 chat。
6. 纪律: 密钥只存 `.env` (已 gitignore), 禁止进代码/镜像/日志; CI 不需要任何
   真实 key (stub 模式跑全部测试, `.github/workflows/ci.yml`)。

## 6. Caddy TLS 域名接法

当前 `deploy/Caddyfile` 监听 `:8080` (纯 HTTP, 演示口径)。接真实域名启用自动
HTTPS (Let's Encrypt) 的改法:

```caddyfile
# deploy/Caddyfile — 把首行 :8080 换成你的域名即可, Caddy 自动签发/续期证书
edu.example.com {
	encode gzip

	handle /api/* {
		reverse_proxy api:8000 {
			flush_interval -1     # SSE: 关闭缓冲, 逐事件透传 (必须保留)
		}
	}

	handle {
		reverse_proxy web:3000    # 前端容器接入后启用; 当前 Caddyfile 是占位响应
	}
}
```

配套改动:
1. compose 的 caddy 服务补 80/443 端口映射与证书卷:
   `ports: ["80:80", "443:443"]`, `volumes: [caddy_data:/data]`
   (80 端口必须可达, ACME HTTP 挑战用; 域名 A 记录先指到本机)。
2. DNS: `edu.example.com A <服务器公网 IP>`, 生效后 `docker compose ... up -d caddy`。
3. 验证: `curl -sI https://edu.example.com/api/health` 返回 200 且证书为 LE 签发;
   SSE 验证 `curl -N https://edu.example.com/api/chat ...` 逐事件到达 (flush_interval
   -1 缺失时的症状是整段一次性吐出)。
4. 上 TLS 后把 cookie Secure 与生产开关一并核对 (生产化审查 P2-10:
   `APP_ENV=prod` 应关闭 `/api/docs` 公开与 admin 演示端点——该项在并行修复清单)。

## 7. 15 分钟复现核对单 (M5-D4 口径)

```bash
cp .env.example .env && $EDITOR .env   # 填 §1 必填项            (~3 min)
make up                                # 构建 + 起栈全绿          (~5 min, 视网络)
docker compose -f deploy/docker-compose.yml exec api python scripts/seed_demo.py
curl -s http://127.0.0.1:8000/api/health/ready                  # ready
bash scripts/preheat.sh                # 三服务+API+登录+chat 预热状态表
make demo-m2 demo-m3 demo-m4 demo-m5   # 逐里程碑 PASS/FAIL
```

已知阻塞如实声明: 原开发机 Docker daemon DNS 污染导致 `make up` 未在该机验证
(STATUS M1-D1 ⛔, decisions.md D5); 本手册的 compose 路径在新机器执行时若有出入,
以 `docker compose ps` 的 healthcheck 结果为准逐服务排查。
