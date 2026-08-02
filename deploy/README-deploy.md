# 部署与上线手册

> 状态：Active。`docker-compose.yml` 保持开发参考栈；公网小规模上线使用
> [`docker-compose.prod.yml`](docker-compose.prod.yml)。
> 目标读者：部署运维、安全负责人和发布负责人
> 本地开发请阅读[开发手册](../docs/development.md)。

## 部署形态

生产路径是单机 Ubuntu VPS + Docker Compose + Caddy：Caddy 是唯一公网入口，Web、API、
Worker 和四个数据服务都在 Compose 网络内。PostgreSQL、Redis、Qdrant、MinIO 不发布宿主
端口；Caddy 将同一域名的 `/` 转给 Web，将 `/api/*` 转给 API，并对 SSE 使用
`flush_interval -1`。证书数据保存到命名卷，由 Caddy 自动申请和续期。

开发参考栈不具备这些公网边界，不能通过映射端口或 `:8080` 入口代替生产部署。

## 当前部署能力与限制

`deploy/docker-compose.yml` 当前包含：

- PostgreSQL 16
- Redis 7
- Qdrant
- MinIO
- FastAPI API
- 一个 ARQ Worker
- Caddy HTTP API 网关

当前 compose **不包含 Next.js Web 服务**，Caddy 只监听 8080 HTTP 且根路径返回占位文本；
PostgreSQL、MinIO 和应用数据库 URL仍使用开发示例凭据，基础设施端口直接映射到宿主机。
因此它适合本机/内网验证，不能原样作为公网生产部署。

生产 Compose 已覆盖 Web production build、独立 migration job、健康检查、重启策略、
日志轮转、持久卷和私有数据网络；仍需在目标 VPS 上补齐防火墙、备份恢复、监控告警、
模型供应商审计和灰度策略。

## 参考环境要求

- Linux 主机；单机受控内测建议从 8C16G 起，根据 live 压测调整
- Docker 20+ 与 Compose v2
- 可访问模型、嵌入、重排和可选网络搜索供应商
- 域名、TLS 和受控 SMTP
- 独立、加密、可恢复的持久存储

~~~bash
git clone <repo-url> eduagents
cd eduagents
docker --version
docker compose version
~~~

## 生产配置

生产从模板生成服务器本地配置，不要把真实值写回 Git：

~~~bash
cp .env.production.example .env.production
openssl rand -hex 32  # 为数据库应用密码、迁移密码、Redis、MinIO、JWT 分别生成
~~~

`.env.production` 必须只存在服务器上。`scripts/deploy_prod.sh` 会拒绝 placeholder、
开发环境、stub/replay fallback、私有 URL 导入、模拟支付、故障注入和正文观测配置。

### 必须审计的变量

| 范围 | 要求 |
|---|---|
| `APP_ENV` | 生产必须为 `prod` |
| `GATEWAY_MODE` | 正式服务使用 `live`；生产不会回退 stub |
| `JWT_SECRET` | 使用密钥系统生成和注入的高熵随机值 |
| 模型/嵌入/重排 key | 最小权限、预算限制、可轮换，不进入镜像和日志 |
| `DATABASE_URL` | 使用非超级、`NOBYPASSRLS` 应用角色 |
| `MIGRATION_DATABASE_URL` | 只供一次性 migration job，应用进程不可持有 |
| Redis/Qdrant/MinIO | 使用生产地址、认证、TLS/网络隔离和独立凭据 |
| `WEB_ORIGIN` | 精确生产 HTTPS 域名 |
| `REGISTRATION_MODE` | `closed` 关闭公开注册；`invite` 要求邮箱验证码和独立邀请码；`open` 只要求邮箱验证码。生产显式设置，默认不开放 |
| SMTP_HOST/PORT/USER/PASSWORD/FROM | Resend SMTP：`smtp.resend.com:465`、用户 `resend`、API Key 作为密码；发件人使用已验证 `mail.adfgt.xyz` 域名 |
| SMTP_REPLY_TO | 服务器端真实联系邮箱；邮件使用 `Reply-To`，不要写进 Git |
| 配额和预算 | 根据套餐、供应商限额和压测结果设定 |
| `WEB_IMPORT_ALLOW_PRIVATE_HOSTS` | 生产必须为 false |
| `LANGFUSE_LOG_CONTENT` | 默认关闭正文记录，确需开启先完成隐私审查 |

生产 Compose 使用 `edu_migration`（仅 migration job）和 `edu_app`（`NOSUPERUSER`
`NOBYPASSRLS`）两个角色。API/Worker 的 `MIGRATION_DATABASE_URL` 和迁移密码会被明确置空；
迁移不会在 API/Worker 启动命令中执行。
同一个一次性 migration job 还会用 `edu_migration` 创建 LangGraph checkpoint schema；
API/Worker 只使用 `edu_app` 的表级 DML 权限，不会在启动时执行 DDL。

## 发布命令

在目标服务器上固定代码提交后执行：

~~~bash
scripts/deploy_prod.sh
~~~

`PRODUCTION_ENV_FILE` is wired into the Compose file so validation can use a template copy
without changing the production default path. The deploy script sets it to the exact server-local
file being validated.

脚本只做本机发布编排：校验配置、构建 API/Worker/Web 镜像、启动基础设施、执行一次
`alembic upgrade head`，再启动并等待 API、Worker、Web、Caddy 健康。它不会运行 `make seed`。
若 `REGISTRATION_MODE=closed`，首个管理员只能在服务器上通过交互式一次性流程创建：

~~~bash
docker compose --env-file .env.production \
  -f deploy/docker-compose.prod.yml run --rm --no-deps migrate \
  python /app/scripts/create_first_admin.py \
  --email admin@your-domain.example \
  --instance-name=eduagents-prod \
  --confirm-instance=eduagents-prod \
  --expected-db-host=postgres \
  --expected-database=eduagents_prod
~~~

该流程使用 migration job 的高权限连接，只在 API 镜像中携带脚本；密码通过容器内
`getpass` 交互输入两次，不出现在参数、shell history 或日志中。它拒绝已有管理员、
校验实例名和精确数据库目标，完成后容器退出。创建后应立即退出服务器 shell 并保管
生成账号的审计记录。
从服务器之外的网络执行验收：

~~~bash
scripts/verify_prod.sh https://your-domain.example
~~~

该验收检查 HTTPS live/ready、Worker 心跳、HTTP→HTTPS、证书以及数据服务端口未公开；
localhost、RFC1918 私网地址会被脚本拒绝。

## 旧运行时数据清理

[`scripts/purge_runtime_data.py`](../scripts/purge_runtime_data.py) 默认只读，输出数据库、
Redis DB、Qdrant collection、对象存储和所有业务表数量。它保留 Alembic 与 LangGraph
schema-migration 元数据，删除运行时 checkpoint 行；不会删除源代码、migration、tests 或
corpus；历史 cassettes、运行对象和 `docs/evidence` 已在上线前单独清除。

执行前必须确认输出是专用 EduAgents 旧测试实例，而不是含真实用户的实例，并使用精确目标：

~~~bash
PURGE_EXPECTED_DB_HOST=127.0.0.1 \
PURGE_EXPECTED_REDIS_HOST=127.0.0.1 \
PURGE_EXPECTED_QDRANT_URL=http://127.0.0.1:6333 \
python scripts/purge_runtime_data.py

PURGE_EXPECTED_DB_HOST=127.0.0.1 \
PURGE_EXPECTED_REDIS_HOST=127.0.0.1 \
PURGE_EXPECTED_QDRANT_URL=http://127.0.0.1:6333 \
python scripts/purge_runtime_data.py --execute --confirm-instance=eduagents-local-dev
~~~

生产实例必须同时设置 `PURGE_INSTANCE_NAME`、期望数据库名/端口、Redis DB、Qdrant URL 和
对象存储 bucket；`APP_ENV=prod` 还需要显式 `--allow-prod`。任何目标不匹配或外部存储
失败都会中止并报告边界，不伪造“全部删除”。清理后应再次运行 dry-run，并检查
`alembic current`/`alembic heads`；不要运行 demo seed。

## 数据库迁移

当前单机 compose 在 API 启动命令中先执行 `alembic upgrade head`。这只适合单副本：

~~~bash
make up
docker compose -f deploy/docker-compose.yml exec api alembic current
docker compose -f deploy/docker-compose.yml exec api alembic heads
~~~

多副本或滚动发布必须把 migration 拆成一次性发布步骤：

1. 备份并校验；
2. 使用 migration 角色执行升级；
3. 验证只有一个预期 head；
4. 再启动/滚动 API 和 Worker；
5. 执行只读健康检查和黄金路径；
6. 禁止多个 API 副本并发抢 DDL。

已提交 migration 不修改。需要兼容滚动发布时优先采用 expand/migrate/contract 分阶段方案。

## 启动单机参考栈

~~~bash
make up
docker compose -f deploy/docker-compose.yml ps
curl -fsS http://127.0.0.1:8000/api/health/live
curl -fsS http://127.0.0.1:8000/api/health/ready
curl -fsS http://127.0.0.1:8080/api/health
~~~

`ready` 依赖 PostgreSQL、Redis、Qdrant 和 Worker 心跳。只有所有预期服务 healthy、只有预期
Worker 数量且异步队列无积压，才算参考栈启动完成。

公网注册的图形验证码是机器人防护，邮箱验证码是邮箱所有权证明；普通登录不要求邮箱验证码。
生产 `APP_ENV=prod` 且公开注册开启时，API 会在启动阶段拒绝缺少完整 Resend SMTP、正式
HTTPS `WEB_ORIGIN` 或非 `mail.adfgt.xyz` 发件人的配置。Resend 域名应保持 Verified，并配置
SPF、DKIM、DMARC。真实 API Key 只放服务器 Secrets 或本地 `.env`，`.env` 不提交 Git。

生产环境不得运行 `make seed`。本地 demo 账号不是生产账号。

## Web、Caddy 与 TLS

当前 Caddyfile 只反代 `/api/*`，并通过 `flush_interval -1` 透传 SSE。生产需要：

1. 构建并运行 `apps/web` 的 production bundle；
2. 将根路径反代到 Web；
3. 域名监听 80/443，持久化 Caddy 证书数据；
4. 保留 API SSE 无缓冲配置；
5. 只信任受控反代网段提供的 Forwarded/X-Forwarded-For；
6. 验证 refresh cookie 为 Secure，`WEB_ORIGIN` 精确匹配；
7. 将 PostgreSQL、Redis、Qdrant 和 MinIO 端口移出公网。

示意配置：

~~~caddyfile
edu.example.com {
    encode gzip

    handle /api/* {
        reverse_proxy api:8000 {
            flush_interval -1
        }
    }

    handle {
        reverse_proxy web:3000
    }
}
~~~

当前 API 文档仍固定暴露在 `/api/docs` 和 `/api/openapi.json`。若生产策略不允许公开，应在
反代层限制，或在后续代码变更中按环境关闭。

## 管理和开发能力的生产边界

- 时间旅行在未显式配置时会在 prod 关闭，但仍需测试配置。
- 模拟支付在 prod 返回拒绝。
- 故障注入端点仍可由平台管理员调用；生产需要网络隔离、额外开关、二次确认和审计告警。
- API 文档、评估运行和管理员批量操作应限制到内网/VPN 或独立管理域。
- 不允许管理员通过共享数据库超级用户完成日常运营。

## 备份

`scripts/backup.sh` 生成 PostgreSQL dump、本地对象目录 tar 和 Qdrant collection snapshot，
并写 SHA-256 manifest。脚本默认包含当前开发机路径，跨机器必须设置：

~~~bash
ENV_BIN=/usr/bin \
PGHOST=127.0.0.1 \
PGUSER=<backup-role> \
PGDATABASE=eduagents \
BACKUP_ROOT=/srv/eduagents/backups \
QDRANT_URL=http://127.0.0.1:6333 \
bash scripts/backup.sh
~~~

compose 使用 MinIO 时，脚本不会自动备份 bucket；必须按脚本说明使用 `mc mirror` 或对象存储
版本化/复制能力，并把对象清单与数据库/Qdrant 快照作为同一恢复点管理。

备份要求：

- 独立 backup 角色和凭据；
- 备份加密、异地复制、访问审计；
- 轮转策略与隐私留存一致；
- 每次备份检查 manifest；
- 定期恢复演练，而不只检查文件存在。

## 恢复

`scripts/restore.sh` 是覆盖式操作，会清理并恢复 PostgreSQL、对象目录和 Qdrant。只允许在
隔离环境演练或正式事故流程中使用。

~~~bash
# 先停止 API 和 Worker，并设置正确环境变量
bash scripts/restore.sh <stamp-or-backup-directory>
~~~

恢复后必须：

1. 校验 manifest；
2. 运行 migration 到当前 head；
3. 检查应用角色/RLS 权限；
4. 核对已注销用户和删除事件，避免旧备份让数据复活；
5. 启动一个 Worker，检查 cleanup/ARQ 积压；
6. 运行 ready、登录、知识库检索、题目资料、导出等黄金路径；
7. 记录 RTO/RPO 和演练结果。

## 监控与告警

正式上线至少监控：

- API live/ready、5xx、P95/P99、SSE 首事件和完整时长；
- PostgreSQL 连接、慢查询、存储、复制/备份；
- Redis 内存、连接、持久化和 ARQ 队列积压；
- Worker 心跳、失败重试、pending cleanup、导出/摄取耗时；
- Qdrant/对象存储可用性、容量和错误；
- 模型供应商错误、降级、token、成本和全局预算；
- 注册/登录异常、越权、管理员操作和内容安全事件。

告警必须有负责人、升级路径和处理手册，不能只展示在仪表盘。

## 发布与回滚

推荐先使用独立预发布环境和白名单内测：

1. 固定提交和镜像 digest；
2. 备份、migration dry run、配置审计；
3. 执行 lint、测试、生产构建和 live 黄金路径；
4. 小流量发布，观察 API、队列、成本、内容安全和数据错误；
5. 扩量前完成至少一次恢复演练；
6. 回滚应用前确认数据库 migration 是否向后兼容，禁止直接回滚 schema。

详细上线门槛见 [STATUS.md](../STATUS.md)，安全要求见
[docs/safety.md](../docs/safety.md)。
