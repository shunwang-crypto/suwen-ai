# 苏问 AI课堂文档中心

> 状态：Active
> 目标读者：产品、开发、测试、运维、安全审查和 AI 编程代理
> 维护原则：代码、迁移和自动化测试是最终技术事实；本目录只保留当前有效文档。

## 从哪里开始

| 读者/任务 | 首先阅读 | 然后阅读 |
|---|---|---|
| 第一次了解项目 | [根 README](../README.md) | [当前状态](../STATUS.md)、[系统架构](architecture.md) |
| 本地开发 | [开发手册](development.md) | [API 契约](api-contract.md)、[数据模型](data-model.md) |
| 前端开发 | [开发手册](development.md) | [Web 子项目说明](../apps/web/README.md)、`apps/web/AGENTS.md` |
| 后端/Worker 开发 | [系统架构](architecture.md) | [API 契约](api-contract.md)、[数据模型](data-model.md) |
| 学习算法 | [VLSE](vlse.md) | [可执行学习计划](learning-plan.md)、[技术取舍](tradeoffs.md) |
| 测试与验收 | [当前状态](../STATUS.md) | [开发手册](development.md)、`tests/` |
| 生产部署 | [部署手册](../deploy/README-deploy.md) | [安全](safety.md)、[扩展性](scaling.md) |
| AI 编程代理 | 本文件 | 当前代码、仓库级指令及目标目录中的 `AGENTS.md` |

## 权威文档

| 信息 | 主要来源 |
|---|---|
| 项目入口与快速启动 | [README](../README.md) |
| 已实现、部分实现和上线门槛 | [STATUS](../STATUS.md) |
| 产品概念、组件和信任边界 | [architecture.md](architecture.md) |
| 本地环境、测试和迁移 | [development.md](development.md) |
| HTTP/SSE 业务语义 | [api-contract.md](api-contract.md) |
| 数据实体、关系和生命周期 | [data-model.md](data-model.md) |
| 长期技术决策 | [decisions.md](decisions.md) |
| 学习计划任务契约 | [learning-plan.md](learning-plan.md) |
| 安全、隐私和残余风险 | [safety.md](safety.md) |
| 扩展路径 | [scaling.md](scaling.md) |
| 可替换组件的技术取舍 | [tradeoffs.md](tradeoffs.md) |
| 学习状态引擎 | [vlse.md](vlse.md) |
| 生产配置、备份和恢复 | [部署手册](../deploy/README-deploy.md) |
| Web 工程约定 | [Web 子项目说明](../apps/web/README.md) |

## 文档治理

1. `docs/` 只保留上表列出的当前文档，不存放旧提示词、交接稿、阶段计划、评审快照、
   答辩稿、截图或一次性测试结果。
2. Git 历史就是归档；不要创建 `archive`、`legacy-docs`、`.old` 或 `.bak` 副本。
3. 新的长期决策写入 `decisions.md`；当前状态写入 `STATUS.md`；操作命令以 Makefile 和
   `development.md` 为准。
4. 评估、压测、截图和浏览器验收输出写入 `/tmp/eduagents-evidence` 或外部制品存储，
   不写回仓库。
5. `prompts/` 是产品运行时模型提示词，`corpus/` 是示例语料；两者不属于待清理的开发提示词。
6. 已提交 migration 不修改；schema 变化增加新 migration 和专项测试。
7. 不把计划写成已实现，不把 stub/replay 或旧截图写成生产能力。

同一变更必须同步检查：

| 代码变化 | 文档 |
|---|---|
| 页面、角色或产品边界 | `README.md`、`STATUS.md`、`architecture.md` |
| API、状态机或错误码 | `api-contract.md` |
| ORM、外键、RLS 或 migration | `data-model.md`、`safety.md` |
| 环境、启动或测试方式 | `development.md`、必要时部署手册 |
| 长期技术取舍 | `decisions.md` 或 `tradeoffs.md` |

运行 `make docs-check` 会检查正式文档索引、相对链接，并拒绝 `docs/` 下重新出现未纳入
治理的文件或生成物。
