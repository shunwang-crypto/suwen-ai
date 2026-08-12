# 扩展性演进路径：现在 → 十万级 → 亿级

> 纪律：**绝不宣称“支撑亿级”**，只描述可演进的架构原则，
> 并用当前量级的真实数据证明每一个决策。本文“现状”列如实描述单机事实，
> 不把论证写成已实现。
>
> 历史压测和评测输出已在上线前清除。当前没有可引用的生产并发基线；正式环境建立后，
> 必须重新生成带日期、环境和命令的基线，不能引用旧 stub/replay 数字。

## 分层演进表

| 层 | 现状 (诚实) | 十万级用户 | 亿级 | 迁移为什么不是架构手术 |
|---|---|---|---|---|
| PostgreSQL | 单实例 (开发用 conda 版, 部署用 compose 单容器); **无主从、无 HA**; 备份三件套本轮刚补 (`scripts/backup.sh`, cron 建议行见脚本头注) | 主从 + 读写分离 (只读报表/评估查询走从库); PgBouncer 事务池; 迁移拆独立 job (评审 P1-14) | Citus 按 `tenant_id` 分片——所有业务表自带 tenant_id 且查询强制携带 (RLS + 中间件双保险), 分片键零改造 | schema 全程 Alembic (`alembic/versions/`), 表设计从第一天按租户列分片友好 |
| Redis | 单实例, appendonly (compose) / 本地 conda 版; 限流/预算/缓存/黑名单/队列共用 db0 | 按用途拆库或拆实例 (队列与缓存分离); 开启 RDB+AOF 混合持久化 | Redis Cluster; 键空间已按前缀设计 (`bl:` `budget:` `qc:` `arq:`), hash tag 即可保证同槽 | 所有键带业务前缀且有 TTL 治理 (生产化审查"已验证扎实"清单) |
| Qdrant | 单实例 (dev-infra 静态二进制 / compose 单容器); 单 collection `eduagents_chunks` + `answer_cache`; **无副本** | 单机竖向扩 + 快照备份 (`scripts/backup.sh` 已接 snapshot API); `tenant_id` keyword 索引物理聚簇 (官方多租户方案) | 分布式分片 + 副本; 十亿向量量级再评估 Milvus——因业务只依赖 `VectorStore` Protocol (`packages/core/src/eduagents_core/vectorstore/base.py`), 换库是实现替换 | 接口收敛: 检索/写入/删除全走 Protocol, 测试可用内存实现替身 |
| API (FastAPI) | uvicorn 单进程; **无副本无粘性** | 多副本水平扩——服务无状态 (会话在 PG, 图状态在 PG Checkpointer, 限流/黑名单在 Redis), SSE 断线携 thread_id 任一实例可续传 | 同左, 副本数量级增加 + 就绪探针摘流量 (`/api/health/ready`, PG/Redis/Qdrant 任一挂返 503) | 无本地状态是写死的架构铁律 (§7.1); 越权测试覆盖连接池复用路径 |
| Worker (ARQ) | 单进程; 摄取/Reflection/学习指南全走队列, API 只做 O(1) 落存储 | 多 worker 进程 + 按队列深度手动扩; 成本敏感任务 max_tries 收紧 (评审 P1-7 方案) | K8s + KEDA 按 Redis 队列深度自动伸缩 (ScaledObject 示例见下) | 任务协议是纯 JSON 入队, worker 无状态 |
| 队列 | Redis + ARQ (asyncio 原生, 与 FastAPI 同并发模型, §4.2 选型) | 仍是 Redis——单 Redis 队列在此量级远未饱和 | 迁 Kafka: 任务 payload 协议不变, 生产/消费两端各换一个适配器; 保序需求按 tenant_id 分区 | enqueue 调用点集中在 API/worker 边界, 无业务代码直连队列实现 |
| 对象存储 | 开发用本地目录 (`~/.local/share/eduagents/objects`), 部署用 MinIO | MinIO 分布式模式 (4 节点纠删码) | 直接换云对象存储——S3 协议零改动 (§4.2 选 MinIO 的理由), 按 `tenant_id/kb_id/` 前缀 + 预签名 URL 不变 | 存储层已抽象 (`packages/core/src/eduagents_core/storage.py` / `storage_minio.py`) |
| 租户模型 | `tenant_id = user_id` (个人即租户); 机构库已实装 `owner_type` 判别列 + tenant_id 语义泛化为 owner_id (decisions.md D14, **迁移零回填、向量零迁移**) | org 作为一等租户: 配额/计费/审计按 org 聚合; 邀请码轮换与成员移除补齐 (评审 P1-4) | org 分级 (学校→年级→班级) 仍复用同一 owner 语义; 分片键 tenant_id 语义不变 | D14 的判别列设计使 documents/chunks/Qdrant payload 传播链一行不改 |
| 模型网关 | 自研三档路由/熔断/降级链，单进程内存态熔断器（**多实例下失效**） | 熔断/预算计数状态外置 Redis；多 Provider 分流 | 瓶颈在上游 LLM：多供应商配额池 + 语义缓存 | 所有调用必经 `ModelGateway` Protocol，计量/缓存/降级/录制在同一入口 |

## 现状边界

- 单机部署，全部组件**无 HA**。
- 已提供 `scripts/backup.sh` / `scripts/restore.sh` 和 14 份轮转；生产恢复演练仍需执行。
- 当前没有可引用的生产并发基线，目标环境需重新运行阶梯压测。
- 进程内熔断器多实例失效、迁移内联在启动命令里存在多副本竞态，属于扩副本前必修项。

## compose → K8s 迁移要点

1. **迁移独立化**: compose 里 `alembic upgrade head` 内联在 api 启动命令
   (`deploy/docker-compose.yml` api.command), 多副本会并发抢 DDL 锁——K8s 化第一步
   是抽成 Job/initContainer, api/worker `depends_on` 其完成 (对应评审 P1-14)。
2. **探针拆分**: 已有 `/api/health/live` (恒 200) 与 `/api/health/ready`
   (PG/Redis/Qdrant 任一挂 → 503), 直接映射 livenessProbe / readinessProbe
   (`apps/api/src/eduagents_api/routers/health.py`)。
3. **配置与密钥**: 全部配置已走 env (`.env.example` 全量注释), 平移为
   ConfigMap + Secret; 密钥轮换步骤见 `deploy/README-deploy.md`。
4. **worker 伸缩**: KEDA ScaledObject 监听 Redis list 长度 (示例, 不必真跑):

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: eduagents-worker
spec:
  scaleTargetRef:
    name: eduagents-worker
  minReplicaCount: 1
  maxReplicaCount: 20
  triggers:
    - type: redis
      metadata:
        address: redis:6379
        listName: arq:queue          # ARQ 默认队列
        listLength: "10"             # 每 10 个待处理任务扩 1 副本
```

5. **SSE 与负载均衡**: SSE 无会话粘性 (断线携 thread_id 从 PG Checkpointer 续传),
   Ingress 只需关闭响应缓冲 (nginx `proxy_buffering off` / Caddy 默认流式)。

## 单位经济学 (口径声明)

成本归因数据面已就位：`usage_log` 按 request_id/tenant_id/user_id/feature/model/tier
落库。但当前运营面板数字是 **stub 桩伪定价，不是真实成本**；真实单位成本必须在 live
环境完成全链路计量和压测后再引用。
