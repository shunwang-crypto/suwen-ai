# M1-D1: docker compose 全绿验证 (2026-07-27)

长期被 Docker 代理问题阻塞（机器仅代理出口、dockerd 不读代理），用户在 daemon.json
配置 `proxies`（Docker ≥23 原生支持）后解除。本文为 compose 全套拉起 + 全链路冒烟证据。

## 验证方式

```bash
cd deploy
docker compose -f docker-compose.yml -f compose.validate.yml up -d
```

`compose.validate.yml` 仅做两件事（容器内拓扑/镜像/健康检查与 docker-compose.yml 完全一致）：
1. 宿主端口平移（15432/16379/16333/18000/18080/19000）——开发机上 conda 版
   PG/Redis/Qdrant 与 dev API 正占用默认端口，验证不打断开发棧；
2. 构建走宿主网络+代理 args（本机 pip 出口只有 127.0.0.1:7890）；运行时
   EMBEDDING/RERANK/GATEWAY 全 stub（离线口径，live 配置由 .env 提供不变）。

## 结果: 7/7 容器全绿

```
NAME                   IMAGE                  STATUS
eduagents-api-1        eduagents-api          Up About a minute (healthy)
eduagents-caddy-1      caddy:2-alpine         Up About a minute
eduagents-minio-1      minio/minio:latest     Up 4 minutes (healthy)
eduagents-postgres-1   postgres:16-alpine     Up 4 minutes (healthy)
eduagents-qdrant-1     qdrant/qdrant:latest   Up 4 minutes (healthy)
eduagents-redis-1      redis:7-alpine         Up 4 minutes (healthy)
eduagents-worker-1     eduagents-worker       Up About a minute

Docker version 29.6.2, build dfc4efb
Docker Compose version v5.3.1
```

（caddy/worker 无 healthcheck 定义故无 healthy 标记；caddy 依赖 api healthy 才启动，
worker 由 api 的 /ready 探针间接监督——`checks.worker: ok`。）

## 全链路冒烟（容器栈全新库，迁移由 api 启动命令 `alembic upgrade head` 完成）

1. **就绪探针**（直连 18000 与经 Caddy 18080 一致）：
   ```json
   {"status":"ok","gateway_mode":"stub","checks":{"postgres":"ok","redis":"ok","qdrant":"ok","worker":"ok"}}
   ```
2. **注册**：POST /api/auth/register → 201, id=2d43d479-…（bcrypt/RLS 全走容器内 PG16）
3. **SSE 对话**（经 Caddy `flush_interval -1` 逐事件透传）：
   ```
   event: node_update
   data: {"node":"generate","status":"running","detail":"生成回答中"}
   event: token
   data: {"text":"【开发桩响应 · 非真实模型输出】\n"}
   event: token
   data: {"text":"档位=standard 指纹=25412ae1\n"}
   ...
   ```
   逐 token 流式 + stub 横幅如实标注，符合 §14 反冒充要求。

## 顺手抓到并修复的真实缺陷

首次 `up` 时 api 容器 `ModuleNotFoundError: No module named 'langgraph'`——
pyproject 依赖声明与开发 conda 环境**漂移**（开发期手装的包未回填声明），
本地一切正常、镜像构建即缺包。这正是 compose 验证的价值。

**修复**（deploy/api.Dockerfile）：镜像依赖改为以 `requirements.lock` 为准
（先 `pip install -r requirements.lock`（剔除 1 行 conda 本地路径），再
`--no-deps -e` 安装本仓三包），从根上执行 D1 锁定纪律，杜绝声明漂移再次逃逸。

## 复现/清理

```bash
cd deploy
docker compose -f docker-compose.yml -f compose.validate.yml up -d   # 拉起
docker compose -f docker-compose.yml -f compose.validate.yml down -v # 清理(含卷)
```
