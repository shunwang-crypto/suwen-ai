# 决策与偏差记录

> §0 执行协议要求：任何超出实现提示词的技术决定、或与提示词冲突的现实约束，必须记录于此，禁止静默偏离。

## D1 · 开发环境用 conda + 国内源（2026-07-26）
用户指示。conda env `eduagents`（Python 3.12，miniforge），pip 走 TUNA 源。
锁定方式：`make lock` 生成 `requirements.lock`（pip freeze，排除 editable 包）。
提示词的"lockfile 固定"由此满足；部署镜像内 pip 同样走 TUNA。

## D2 · 网关新增第四种模式 stub（2026-07-26）
提示词 §9 只定义 live|record|replay。用户明确指示"开发期先不真实调用模型（时间花费大）"，
而 replay 需要先有录制。故增加 `GATEWAY_MODE=stub`：确定性开发桩，输出带
「【开发桩响应 · 非真实模型输出】」横幅，永远无法冒充真实回答（§14 反造假）。
约束：stub 仅限 dev/test；`app_env=prod` 且关闭 fallback 时全链路失败会直接报错。
黄金演示路径一律用 record→replay，stub 不出现在任何演示与评测数字中。

## D3 · 初始迁移用 metadata.create_all（2026-07-26）
`alembic/versions/0001` 用 `Base.metadata.create_all(bind)` 而非手写 20 张表的 DDL，
保证迁移与 ORM 模型零漂移；RLS（ENABLE+FORCE+策略）仍是显式 SQL。
后续所有 schema 变更一律 `alembic revision --autogenerate` 增量，禁止再用 create_all。

## D4 · 增加 chat_messages 表（2026-07-26）
提示词 §6 未列此表。理由：F1-A6 要求会话列表/历史加载/重命名/删除，需要独立于
LangGraph checkpointer 的可查询消息存储。M3 引入 checkpointer 后二者并存：
chat_messages 做展示与历史 API，checkpointer 做图执行状态。

## D5 · 本地开发用 conda 版 PostgreSQL/Redis 代替容器（2026-07-26）
现实约束：本机 Docker daemon 的 DNS 被污染（docker.io 解析到 Facebook IPv6 段），
且无免密 sudo，无法当场改 `/etc/docker/daemon.json`。
处置：开发与集成测试用 conda-forge 的 postgresql/redis-server 本地实例
（`make dev-infra`）；docker-compose 仍是唯一部署路径，修复 daemon 后即可 `make up`。
待办（需用户执行，见 README「Docker 修复」）：写入 daemon.json 的 dns + registry-mirrors。

## D6 · 密码哈希直接用 bcrypt 库（2026-07-26）
不用 passlib（已停止维护，与 bcrypt>=4.1 不兼容）。cost 默认 12（.env 可配），
测试环境降为 4 以提速（tests/conftest.py）。

## D7 · usage_log 的 RLS 策略放行 is_admin（2026-07-26）
F10 运营面板需要管理员跨租户聚合。仅 usage_log 一张表的 SELECT 策略追加
`OR current_setting('app.is_admin', true) = 'true'`；写入仍强制租户匹配。

## D8 · Qdrant/MinIO 推迟到 M2 接入（2026-07-26）
M1 骨架不依赖二者；compose 中已定义服务与健康检查，M2 摄取流水线落地时启用。
