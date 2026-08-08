# GPT-5.6 Luna 多智能体实施提示词（2026-08-08）

> 分类：Historical / implementation handoff
> 用法：新建一个模型为 GPT-5.6 Luna 的 Codex session，把“可复制提示词”章节完整发送。
> 本文描述实施任务，不代表功能已经完成。

> 第一波 A0、A1、B0 已由提交 `5735174` 完成；后续 session 不得重复实现第一波，
> 应先核对当前真实 migration head 和工作区状态，再按实施计划处理剩余范围。

## 可复制提示词

你正在 `/home/shunw/eduagents` 仓库工作。请完整实现并审查
`docs/VECTOR_LEARNING_IMPLEMENTATION_PLAN_2026-08-08.md` 中的计划，先完成第一波基础项，
不要把计划文档当成已经实现的事实。

### 角色与模型要求

你是主审查智能体，必须保留一个并发槽给自己，负责架构取舍、共享文件协调、代码审查、
迁移链整合和最终验证。使用三个子智能体并行实现。主 session 已选择 GPT-5.6 Luna；
创建子智能体时让它们继承主 session 的 Luna 模型，并将 reasoning effort 设置为可用最高档
`ultra`。如果工具不接受显式 Luna override，就使用完整上下文继承且不要替换为 Sol/Terra。

不要让子智能体自行提交 git。所有改动先由主智能体审查；除非用户另行授权，否则本次不提交。

### 开始前必须做

1. 完整阅读：
   - `docs/VECTOR_LEARNING_IMPLEMENTATION_PLAN_2026-08-08.md`
   - `docs/development.md`
   - `docs/data-model.md`
   - `docs/safety.md`
   - 目标目录内的全部 `AGENTS.md`
2. 运行并记录：
   - `git status --short --branch`
   - `git log -5 --oneline --decorate`
   - `alembic heads`
3. 工作区既有修改属于用户。不得 reset、checkout、覆盖、删除或顺带提交。
4. 所有文件修改使用 `apply_patch`。搜索优先使用 `rg`。
5. 如果当前 migration head 不再是 `0020`，重新分配连续 revision，并把变化同步给所有子智能体。

### 第一波并行分工

#### 子智能体 1：A0 精确向量对账

负责范围：

- `packages/core/src/eduagents_core/vectorstore/base.py`
- `packages/core/src/eduagents_core/vectorstore/qdrant_store.py`
- `apps/api/src/eduagents_api/vector_reconcile.py`
- `scripts/check_vector_drift.py`
- `tests/unit/test_vector_reconcile.py`
- 必要的新定向测试

要求：

- 从按 KB 计数升级为 PG child chunk ID 与 Qdrant point ID/payload 的精确集合核对；
- 识别 missing、extra、orphan KB、wrong tenant 和 malformed payload；
- Qdrant 扫描不读取正文或向量；
- 保留现有 API 聚合字段兼容；
- 修复 orphan-only CLI 退出 0；
- 不修改 ORM 和 migration；
- 运行定向测试和 Ruff，报告设计取舍与风险。

#### 子智能体 2：A1 cleanup outbox

负责范围：

- `apps/api/src/eduagents_api/models.py` 中 PendingCleanup 附近
- 新 migration（若 head=0020，使用 `0021_pending_cleanup_observability.py`）
- `apps/worker/src/eduagents_worker/cleanup_outbox.py`
- cleanup 相关 unit/integration tests
- 必要的数据模型/运维文档小范围更新

要求：

- 增加 last/next attempt、bounded error、completed、lease 字段；
- 使用可恢复租约或 `FOR UPDATE SKIP LOCKED`，支持多 worker；
- 指数退避、payload 校验、脱敏错误持久化；
- 保持原 done 行为兼容；
- 不改向量对账文件和 review 路由；
- migration upgrade/downgrade、并发领取、外部失败恢复必须有测试。

#### 子智能体 3：B0 FSRS/learning events

负责范围：

- `apps/api/src/eduagents_api/models.py` 中学习模型相关区域
- 新 migration（若 A1 使用 0021，则使用 `0022_learning_events.py`，down_revision=0021）
- `packages/core/src/eduagents_core/mastery/scheduler.py`
- `apps/api/src/eduagents_api/routers/reviews.py`
- quiz/assessment 的 learning event 写入所需最小代码
- 新的 FSRS/learning event tests
- 导出、留存、数据模型文档的必要更新

要求：

- 新增 `fsrs_review_events` 和 `learning_events`，带 tenant、RLS、用户生命周期；
- 不再丢弃 py-fsrs ReviewLog；
- 业务 card 身份使用稳定 ErrorItem UUID，不依赖毫秒时间戳；
- 复习写 card、FSRS event、learning event 必须同事务；
- 增加幂等键，重复提交不能二次推进 card；
- quiz/review/assessment 写统一事件；历史 review 不允许猜测回填；
- 默认 FSRS 参数和现有行为保持不变；
- 不实现个体参数拟合，那属于后续 B1。

### 并行协调规则

- 三个智能体共享工作区。A1 和 B0 都会修改 models/migrations，主智能体必须提前告知固定
  revision 链，并在合并阶段检查两者没有互相覆盖。
- 子智能体发现职责外问题时只报告，不擅自扩大修改范围。
- 每个子智能体完成后必须报告：修改文件、核心设计、运行命令、测试结果、未决风险。
- 主智能体不能只接受报告；必须亲自阅读完整 diff 和关键文件。

### 主智能体审查清单

对 A0：

- 验证等量错配、wrong tenant、malformed、orphan-only 退出码；
- 确认扫描没有正文/向量，也没有新增无租户业务检索入口；
- 检查旧 API 字段和管理员端点兼容。

对 A1：

- 检查租约领取是否真正原子；
- 检查 worker 崩溃后锁能过期恢复；
- 检查错误字段不会泄露 URL 密码、token 或对象存储 secret；
- 检查退避不会让旧毒行永久阻塞正常行；
- 检查迁移角色权限和 downgrade。

对 B0：

- 检查复习事务原子性和重复提交幂等；
- 检查业务 Clock、time-travel 和 UTC；
- 检查 rating、card ID、event source 的可重放性；
- 检查 RLS、注销、导出、留存；
- 检查没有提前引入 FSRS-7、个体参数或线上算法行为变化。

### 第一波验证门禁

先运行各自定向测试，再由主智能体统一运行：

1. `git diff --check`
2. `alembic heads`，必须只有一个 head
3. 从第一波之前的 head 顺序 upgrade 到新 head
4. 新增 migration 的定向 upgrade/downgrade 测试
5. 向量、cleanup、review/event 定向 tests
6. `make test-unit`
7. Ruff 和 compileall
8. `make docs-check`

不要因为全仓既有无关失败而修改无关代码。若失败是既有问题，给出基线对照证据。

### 第一波完成标准

- A0、A1、B0 全部实现且通过上述门禁；
- 主智能体已经逐项审查，不只是汇总子智能体答案；
- 工作区无未知改动；
- 向用户报告实际完成项、测试结果、migration head、剩余风险和第二波建议；
- 未经用户确认，不自动执行真实向量修复、不调用付费 embedding、不删除任何生产数据。

第一波完成后，如果上下文和运行时间允许，可以准备第二波的实现计划，但不要跳过汇报直接执行
付费或生产数据变更。第二波严格按实施计划中的 A2、A3/A4、B2、B6 顺序推进。
