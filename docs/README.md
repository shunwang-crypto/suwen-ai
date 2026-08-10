# 苏问 AI课堂文档中心

> 状态：Active
> 目标读者：产品、开发、测试、运维、安全审查和 AI 编程代理
> 维护原则：代码、迁移和自动化测试是最终技术事实；本文负责文档导航和治理规则。

## 从哪里开始

| 读者/任务 | 首先阅读 | 然后阅读 |
|---|---|---|
| 第一次了解项目 | [根 README](../README.md) | [当前状态](../STATUS.md)、[系统架构](architecture.md) |
| 本地开发 | [开发手册](development.md) | [API 契约](api-contract.md)、[数据模型](data-model.md)、[可执行学习计划](learning-plan.md) |
| 前端开发 | [开发手册](development.md) | [Web 子项目说明](../apps/web/README.md)、`apps/web/AGENTS.md` |
| 后端/Worker 开发 | [系统架构](architecture.md) | [API 契约](api-contract.md)、[数据模型](data-model.md) |
| 测试与验收 | [当前状态](../STATUS.md) | [开发手册](development.md)、`tests/` |
| 生产部署 | [部署手册](../deploy/README-deploy.md) | [安全](safety.md)、[扩展性](scaling.md) |
| 安全审查 | [安全](safety.md) | [数据模型](data-model.md)、[API 契约](api-contract.md) |
| 比赛答辩 | `docs/defense/` | [VLSE](vlse.md)；历史 evidence 已清除，不能作为当前验收凭据 |
| AI 编程代理 | 本文件 | [AI 优先学习体验实施规格](AI_FIRST_LEARNING_UX_IMPLEMENTATION_SPEC_2026-08-09.md)、[向量/学习实施计划](VECTOR_LEARNING_IMPLEMENTATION_PLAN_2026-08-08.md)、仓库级指令及目标目录中的 `AGENTS.md` |

## 权威来源

每类事实只指定一个主要维护位置，其他文档使用链接，不复制大段内容。

| 信息 | 主要来源 | 说明 |
|---|---|---|
| 项目入口与快速启动 | [README](../README.md) | 面向第一次接触项目的人 |
| 已实现、部分实现、计划中 | [STATUS](../STATUS.md) | 发布状态，不替代代码事实 |
| 产品概念、角色和组件边界 | [architecture.md](architecture.md) | 系统级语义 |
| 本地开发、测试、迁移规范 | [development.md](development.md) | 开发操作手册 |
| HTTP/SSE 业务语义和错误码 | [api-contract.md](api-contract.md) | 字段细节以运行时 OpenAPI 为准 |
| 数据实体、关系和生命周期 | [data-model.md](data-model.md) | ORM 与 migration 为最终定义 |
| 重要技术取舍 | [decisions.md](decisions.md) | 已接受、替代或废弃的决定 |
| 安全、隐私和残余风险 | [safety.md](safety.md) | 与实现和测试同步 |
| 生产配置、备份、恢复、TLS | [部署手册](../deploy/README-deploy.md) | 不复制开发启动说明 |
| 可扩展性路线 | [scaling.md](scaling.md) | 规划不等于当前能力 |
| 学习状态引擎 | [vlse.md](vlse.md) | 专项设计与证据 |

## 文档分类

- **Active**：当前行为或操作规范，功能变化必须同步更新。
- **Reference**：专项设计、研究或取舍，提供背景但不单独证明功能已实现。
- **Presentation**：答辩和演示材料，必须引用 Active 文档，不能反向定义产品事实。
- **Evidence**：某次实验、截图或验收记录；保留当时口径，不随当前实现重写。
- **Historical**：阶段性提示词、交接、旧计划和审查报告，仅供追溯。
- **Runtime content**：`prompts/` 是模型运行提示词，`corpus/` 是示例语料，都不是开发规范。

### 当前目录归类

| 路径 | 分类 | 维护方式 |
|---|---|---|
| `docs/api-contract.md`、`data-model.md`、`learning-plan.md`、`decisions.md`、`safety.md` | Active | 随相关代码变更 |
| `docs/vlse.md`、`scaling.md`、`tradeoffs.md` | Reference | 重大设计变化时复核 |
| `docs/VECTOR_LEARNING_IMPLEMENTATION_PLAN_2026-08-08.md` | Reference | 分阶段实施规格；不得写成已完成功能 |
| `docs/AI_FIRST_LEARNING_UX_IMPLEMENTATION_SPEC_2026-08-09.md` | Reference | AI 优先学习体验的分阶段实施规格；不得写成已完成功能 |
| `docs/research/`、`docs/design/` | Reference | 明确“设计不等于实现” |
| `docs/defense/` | Presentation | 上台前对照 STATUS 和 evidence |
| `docs/evidence/` | Evidence | 仅保留清理说明；新证据须针对明确日期和环境重新生成 |
| `docs/*PROMPT*.md`、`HANDOFF.md`、`PRODUCTION_GAP_REVIEW.md`、`TODO_COMPETITION.md` | Historical | 不作为当前实施事实；仅供提示词和过程追溯 |

## 维护规则

同一变更必须同步下列文档：

| 代码变化 | 必须检查的文档 |
|---|---|
| 新增/改变页面、角色或产品边界 | `README.md`、`STATUS.md`、`architecture.md` |
| 新增/改变 API、状态机、错误码 | `api-contract.md` |
| ORM、外键、RLS、migration | `data-model.md`、`safety.md` |
| 环境变量、启动命令、测试方式 | `development.md`、必要时部署手册 |
| 权限、文件、导出、注销 | `safety.md`、`api-contract.md` |
| 重要且长期有效的技术取舍 | `decisions.md` |

进一步约束：

1. 已提交 migration 不得修改；schema 变化增加新 migration 和迁移测试。
2. 不把计划写成已实现。`STATUS.md` 使用“已实现 / 部分实现 / 计划中”三档。
3. 不在 Active 文档维护固定测试总数、临时提交号或单机绝对路径。
4. 命令必须来自当前 Makefile、package scripts 或实际 CLI。
5. 文档与代码冲突时先确认代码行为，再在同一提交修正文档。
6. 运行 `make docs-check` 检查 Active 文档索引和相对链接。
7. 历史实验结果已清除；新结果写入明确日期和环境的 evidence，并注明生成命令。

## 文档审查清单

- [ ] 新概念是否已有唯一名称和权威定义？
- [ ] 是否错误混用了知识库、题目资料、错题本或真实作答？
- [ ] 是否把待核验数据写成会影响学习状态？
- [ ] 是否包含已经不存在的路径、枚举、命令或服务？
- [ ] 是否把 stub/replay 数据描述为真实模型效果？
- [ ] 是否泄露生产密钥、个人信息或对象存储路径？
- [ ] `make docs-check`、lint 和相关测试是否通过？
