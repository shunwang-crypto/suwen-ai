# 向量可靠性与学习算法实施计划（2026-08-08）

> 状态：实施中（代码切片已落地；真实环境验收未完成）
> 分类：Reference
> 目标读者：后端、Worker、数据、测试、运维和负责实现的 AI 编程代理
> 技术事实优先级：运行时代码、Alembic migration 和自动化测试高于本文

当前状态核对（2026-08-08）：

- A0、A1、A2、A3/A4 和 B0 已有代码实现；A2 默认 dry-run，尚未执行真实修复。
- B1、B3、B4、B5、B7 是离线/shadow 实现；B2 的来源/信心快照已接入，B6 的结构化评分默认仍为 shadow，均不等于线上算法或生产验收。
- A1 的真实 `pending_cleanup` 积压根因诊断、A0/A2 的真实环境对账和任何生产外部残留核对尚未完成；当前缺少经确认的真实环境只读凭据时，不得用本地结果替代。
- 0023–0027 的本地迁移门禁已添加；生产迁移、清理、向量修复、embedding/LLM 和付费调用均未执行。

## 1. 结论与依赖顺序

本计划不按最初的 A1、A2、A3 顺序直接执行。现有向量对账只比较每个知识库的点数，
无法识别“缺一个正确点、同时多一个旧点”的等量错配；现有复习链路也没有持久化
FSRS ReviewLog。因此先补两个基础项：

- `A0`：向量对账升级为 chunk/point ID 与 payload 的精确集合对账；
- `B0`：新增可重放的 FSRS 复习事件和统一学习事件。

依赖关系：

~~~text
向量线：A0 精确对账 → A1 cleanup 根因 → A2 定点修复 → A3 定时告警 → A4 健康摘要

学习线：B0 事件底座
          ├─→ B2 超校正闭环
          ├─→ B6 Explain-back 升级
          ├─→ B1 FSRS-6 个体化
          ├─→ B3 稳定题目身份/Elo → B5 目标正确率 0.75
          └─→ B4 AFM/PFA、B7 KC edge 离线验证
~~~

## 2. 全局实施约束

- 每个 PR 只承担一个明确目标；migration、线上行为和一次性数据修复尽量分开。
- 已提交 migration 不得修改；开始前先确认当前 Alembic head，再顺序分配新 revision。
- 新用户数据表必须包含 `tenant_id`、用户外键、RLS、注销、导出和留存口径。
- 所有批处理默认 dry-run；真实写入必须显式 `--execute`，并带数量/成本上限。
- 所有算法先 offline 或 shadow；达到预先定义的时间序列评估门槛后才影响线上行为。
- 保留默认算法和上一参数版本作为回滚路径。
- 工作区中的既有修改属于用户；不得覆盖、回滚或顺带提交。
- 不实现深度 KT、教学序列 RL/bandit、85% rule、CBM 学习干预或任何“2 sigma”宣传。

## 3. 第一波：可并行实现的基础项

第一波由一个主审查智能体加三个实现智能体并行完成。三项必须全部通过定向测试、
迁移链检查和主智能体代码审查后，才能进入第二波。

### A0：精确向量对账

#### 目标

把当前 `PG done 数 - Qdrant point 数` 的净差额升级为以下精确集合：

- `missing_points`：PG child chunk 存在，但同 ID Qdrant point 不存在；
- `extra_points`：Qdrant point 存在，但 PG child chunk 不存在；
- `orphan_kbs`：Qdrant payload 指向已经删除的 KB；
- `wrong_tenant_points`：KB 存在，但 payload tenant 与 PG owner 不一致；
- `malformed_points`：point ID 或关键 payload 缺失/非法；
- `document_inconsistencies`：ready/enriched 文档没有 done child chunks 等。

#### 实现清单

- 在 QdrantStore 增加不读取正文和向量的分页 point 引用扫描。
- point 引用至少包含 `point_id/tenant_id/kb_id/doc_id`。
- PG 侧按租户读取 `embedding_status='done' AND parent_id IS NOT NULL` 的 chunk ID。
- 以 `(tenant_id, kb_id, point_id)` 比较集合，不再用数量差替代身份一致性。
- 保留旧 API 的 `missing_vectors/orphan_points/kb_drifted` 聚合字段一版。
- 全量反向扫描使用预期 `(tenant_id, kb_id)`，不能只比较 kb_id。
- CLI 严格按 `report.status` 返回：`ok=0`、`drift=1`、`error=2`。
- 增加超时、分页上限和错误不中断其余 KB 的行为。

#### 必测场景

- 两边数量相等但 ID 不同；
- 只有 orphan KB；
- kb_id 正确但 tenant_id 错；
- payload 缺字段或 UUID 非法；
- collection 不存在；
- 单个 KB 失败而其余 KB 仍有结果；
- 旧 API 聚合字段兼容。

#### 验收

- 能输出真实 missing chunk ID 和 extra point ID；
- orphan-only CLI 返回 1；
- 全量连续运行两次结果稳定；
- 单元测试和 Ruff 通过。

### A1：pending_cleanup 根因与可观测性

#### 一次性诊断

- 按 `kind/attempts/age` 统计未完成行；
- 校验各 kind 的 payload 必填字段；
- 关联 `cleanup_outbox_retry/stuck` 日志；
- 分别检查 Qdrant、对象存储、Redis 和 checkpoint 的实际残留；
- 区分“Qdrant 已删但对象存储失败”与真正的向量删除失败；
- 禁止直接把未知行改成 done。

#### Schema

为 `pending_cleanup` 增加：

- `last_attempt_at`、`next_attempt_at`；
- `last_error_code`、有界且脱敏的 `last_error_text`；
- `completed_at`；
- `locked_at`、`locked_by`；
- 可选 `progress_json`，记录多阶段外部清理进度。

第一版保留原 `done` 字段，避免不必要的兼容破坏。

#### Worker

- 使用 `FOR UPDATE SKIP LOCKED` 或显式租约领取任务；
- 同一行不能被两个 worker 同时执行；
- 失败采用指数退避；过期锁可以恢复；
- payload 在执行前按 kind 校验；
- 每个外部清理步骤保持幂等；
- 错误落库但不得包含 secret；
- 输出 pending 数、oldest age、success/failure/stuck 指标。

#### 验收

- 正常任务两分钟内完成；
- 两 worker 并发不会重复领取；
- 外部服务恢复后积压自动完成；
- 超过 SLO 自动产生结构化 stuck 日志；
- migration upgrade/downgrade 和集成测试通过。

### B0：FSRS 复习事件与统一学习事件

#### `fsrs_review_events`

建议字段：

~~~text
id, tenant_id, user_id, error_item_id,
rating, correct, reviewed_at, review_duration_ms,
card_before_json, card_after_json, params_version,
idempotency_key, created_at
~~~

- 用户外键 CASCADE、RLS、rating CHECK；
- `(user_id, idempotency_key)` 唯一；
- 复习事务内同时更新 card 和插入事件；
- 不再丢弃 py-fsrs 返回的 ReviewLog；
- 使用 ErrorItem UUID 作为稳定业务 card 身份，避免毫秒时间戳碰撞；
- 重复提交不能重复推进 card。

#### `learning_events`

建议字段：

~~~text
id, tenant_id, user_id, source_type, source_id,
question_id, item_key, kc_ids_json,
correct, score, rating, difficulty_label,
occurred_at, created_at
~~~

- 支持 `quiz_attempt/review/assessment`；
- 题目 KC 和难度保存作答时快照；
- 通过 `(source_type, source_id)` 或更细粒度键保证幂等；
- tutor summary 不伪造成有标准答案的作答；
- 历史 Attempt/Assessment 可精确回填；历史 review 不从当前 card 反推；
- 纳入注销、导出、留存和 RLS 测试。

#### 验收

- 每个新 Attempt 有且只有一个 learning event；
- 每次 review 有一个 FSRS event 和一个 learning event；
- 时间使用业务 Clock；
- 重复请求幂等；
- 能按事件时间重放一张 FSRS 卡；
- 默认调度结果在未启用个体参数时保持不变。

## 4. 第二波：修复、监控和低风险学习闭环

### A2：缺失向量原位修复

新增 `scripts/repair_missing_vectors.py`：

- 默认 dry-run，支持 `--execute/--kb/--doc/--max-points/--batch-size/--concurrency`；
- 只读取 A0 给出的缺失 chunk ID；
- 保持原 chunk ID，不删除 PG、不重解析文件、不触发 enrichment；
- embedding 文本与现有摄取一致：有 context prefix 时使用
  `prefix + heading + text`，否则 `heading + text`；
- 重建 dense、sparse 和完整 payload，以原 point ID 幂等 upsert；
- dry-run 输出点数、字符数、API 批次和费用上限；费用未知时拒绝大批量执行；
- 修复后自动再次精确对账，并按 KB 跑 retrieval smoke。

只有 PG chunk 损坏、需要更换分块算法/模型时才走完整 reindex。

### A3/A4：定时审计和缓存健康摘要

- 定时任务使用分布式锁，禁止多个 worker 同时全量扫描；
- 持久化或缓存最近一次 `status/counts/started_at/finished_at/code_version`；
- 连续两次 drift 才告警，wrong tenant/orphan/error 立即高优先级告警；
- 审计结果过期产生 stale 告警；
- `/api/health/ready` 只读取最近结果，不实时跑全量扫描；
- drift/error/stale 只置为 `degraded` 并保持 HTTP 200；连接硬依赖失败仍返回 503；
- 健康接口只返回聚合信息，具体 ID 只在管理员接口展示。

### B2：超校正闭环

为 ErrorItem 增加：

- `source_attempt_id`；
- `confidence_at_error`；
- `confidence_probability_at_error`；
- `confidence_scale_version`。

Quiz 判错时在同一事务保存精确 Attempt 来源和信心快照；导入、人工、assessment
没有信心数据时保持 NULL。历史数据仅在唯一可证明时回填。排序改动必须置于 feature
flag/A-B 实验后，FSRS due 仍是硬约束，主要指标为下一次延迟复习正确率。

### B6：Explain-back 评分升级

- 复用现有 ExplainBack，不重复造功能；
- 增加可选 question/source attempt、rubric version 和结构化 score JSON；
- prompt 输入题干、参考答案/解释或材料片段；
- 评分维度为 correctness、reasoning、completeness、evidence、misconception；
- 不评价文风和长度，不写 BKT/FSRS；
- 用至少 100 条双人标注样本验证一致性；未达标时只展示文字反馈，不展示数值分。

## 5. 第三波：算法 shadow 与离线评估

### B1：FSRS-6 个体参数

- 固定当前 21 参数 FSRS-6，不追未发布的 35 参数版；
- 建版本化 `fsrs_params`，保存 algorithm、optimizer version、训练数量、有效跨日
  review 数、data cutoff、fit metrics 和 status；
- 拟合放独立离线环境，不把 torch/pandas 强塞进 API 主运行依赖；
- 16 条只能开始 shadow，不等于可以激活；官方 optimizer 的有效样本门槛必须如实执行；
- 采用 rolling-origin 或严格按时间的 train/holdout；
- 比较默认与个体参数的 log loss、Brier、RMSE 和分桶校准；
- 预先定义激活门槛；不足或未改善的用户继续默认参数；
- Scheduler 改为显式接收参数版本，移除全局默认参数对所有用户的硬绑定；
- 激活/回滚都通过事件重放原子重建 card 状态。

### B3/B5：稳定题目身份、Elo 与 0.75 目标正确率

- 先统计每题作答数、独立用户数和重复曝光率；多数题 unique_users=1 时暂停 item Elo；
- 引入稳定 `canonical_item_id/item_family_id`；不复用的即时生成题不拟合独立 item Elo；
- 能力至少按学科/知识域建表，不能在 User 上放单个全局 ability；
- 低 N 使用带先验的收缩估计，不使用裸 proportion correct；
- 采用 prequential 时间序列评估，与固定难度和简单比例基线比较；
- `practice_target_success=0.75` 与 FSRS `desired_retention=0.9` 分开命名和配置；
- 只有存在校准题池后才按接近 0.75 选题，无匹配时回退当前策略。

### B4/B7：AFM/PFA 与 KC edge

- 从 learning events 生成时间排序数据；明确多 KC credit assignment；
- AFM/PFA 与当前 BKT、简单 logistic、Elo 特征基线比较；第一版只 shadow；
- KC edge 验证控制目标 KC 自身机会数、用户能力和题目难度；
- 使用匹配非边作负对照、多重比较做 FDR；
- 结果标为 supported/contradicted/insufficient/unstable，不自动删除边。

## 6. 兼容清理

- Quiz answers 已有 max 20 行为：补 20 成功、21 返回 422 的契约测试并查访问日志。
- 单值 `kb_id`：先加调用指标，修改 smoke/eval，再观察一个发布周期后删除。
- `PUBLIC_REGISTRATION`：盘点生产 Secret/CI/env，保留新旧冲突检测一版再删别名。
- 旧 `/api/health`：先切内部脚本、compose、负载均衡和外部 monitor，再观察后删除。
- 每个兼容项独立 PR，不在同一发布里完成“迁移调用方+删除兼容”。

## 7. 建议 migration 序列

开始前必须执行 `alembic heads`。当前仓库实测为唯一 head `0027`，链路为：

1. `0021_pending_cleanup_observability.py`
2. `0022_learning_events.py`
3. `0023_vector_audit_access.py`
4. `0024_metacognition_error_linkage.py`
5. `0025_fsrs_params.py`
6. `0027_explain_back_quality.py`

`0026` 有意未使用：B3/B4/B5/B7 是离线/shadow 模型，没有新增 schema。后续 migration 必须从
真实唯一 head 继续，禁止按旧计划重用 `0023` 或制造多 head。

## 8. 每波统一验收门禁

- `git diff --check`
- Alembic 单一 head、从前一 head upgrade 成功、相关 downgrade 测试成功
- 定向 unit/integration tests
- `make test-unit`
- 与数据库相关的隔离集成测试
- Ruff/compileall
- `make docs-check`
- 检查 staged/working diff，确认没有覆盖用户无关改动
- 文档仍明确区分“代码已实现”“离线/shadow”“真实环境待验收”和“权限阻塞”；未完成的生产验收不得改写为完成
