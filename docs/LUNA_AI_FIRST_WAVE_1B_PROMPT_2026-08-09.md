# Luna 多智能体实施提示词：AI-first Wave 1B（2026-08-09）

> 分类：Implementation handoff
> 用法：新建一个 GPT-5.6 Luna 主会话，把“可复制提示词”章节完整发送。
> 本文只授权本地开发与本地验收，不授权部署、修改生产数据或提交 Git。

## 当前交接事实

- Wave 1A 已实现并暂停，不要重复实现或回滚：Quiz outcome v2、脱敏生成诊断、前端错误分类均已有测试。
- 本地真实 OpenCode Zen 已验证：`deepseek-v4-flash-free` 能生成并落库 2/2 题；初始生成预算为 8400 tokens，`GATEWAY_TIMEOUT_SECONDS=180`。
- 本地路由只使用 OpenCode Zen；embedding/rerank 为 stub。不得重新接入 DeepSeek 官方或 SiliconFlow。
- 当前真实缺口是生成题目的 `kc_ids=[]`。Wave 1B 的目标是让 stub 与 live 模式都建立真实、稳定、可审计的 KC 闭环。
- 当前工作区有 Wave 1A、OpenCode 路由、验收脚本、证据和用户删除旧 Figma PNG 等未提交修改，全部必须保留。
- 本地数据库曾出现 revision 已到 head、旧表结构却漏迁移的情况。新 migration 必须在全新数据库和当前本地数据库分别验证；不能用 `stamp` 掩盖结构缺失。
- OpenCode 公共免费端可能返回 429。自动化测试优先 fake/replay；最多做一次必要的真实 smoke，不以反复请求碰运气作为验收。

## 可复制提示词

你正在 `/home/shunw/eduagents` 仓库工作。请严格实施
`docs/AI_FIRST_LEARNING_UX_IMPLEMENTATION_SPEC_2026-08-09.md` 的 **Wave 1B：KC 冷启动**。

Wave 1A 已完成，本次不要重新实现 Wave 1A，也不要进入 Wave 2、OCR、辅导/计划、字号治理。
目标是完成以下闭环：

~~~text
stub 摄取 → 章节 KC（稳定 ID）→ child chunk 回写 KC
→ 冷启动 3 题每题 kc_ids 非空 → 作答
→ LearningEvent / MasteryRecord / MasteryLog 使用同一批 KC
→ 画像至少出现一个真实 KC
~~~

同时保证 live/record/replay 模式在 enrichment 完成前不会把空 KC 的 Quiz 宣称为可用于画像。

### 角色、模型与并行方式

你是 Luna 主审查智能体。保留一个并发槽给主会话，使用三个子智能体并行完成独立文件域。
子智能体继承主会话 Luna 模型；reasoning effort 使用可用最高档 `ultra`。如果工具不允许显式
指定 Luna override，就使用完整上下文继承，不要替换为其他模型。

子智能体不得创建 Git commit。主智能体负责：迁移 revision 分配、跨域接口决定、完整 diff 审查、
冲突整合、联合测试和最终结论。子智能体只能修改自己的责任文件；发现范围外问题只报告。

### 开始前必须完成

1. 完整阅读：
   - `docs/AI_FIRST_LEARNING_UX_IMPLEMENTATION_SPEC_2026-08-09.md`，重点第 3、7、12–17 节；
   - `docs/development.md`、`docs/data-model.md`、`docs/api-contract.md`；
   - `/home/shunw/.codex/AGENTS.md` 和目标目录下全部 `AGENTS.md`。
2. 运行并记录：
   - `git status --short --branch`；
   - `git diff --stat`；
   - `python -m alembic current`；
   - `python -m alembic heads`；
   - `rg` 核实 KC/Chunk/Document/Quiz/Mastery/LearningEvent 的真实模型与调用链。
3. 现有修改均属于用户。禁止 `reset`、`checkout --`、覆盖、删除或顺带格式化无关文件。
4. 如果 migration head 仍为 `0027`，主智能体预留下一 revision 给 Wave 1B；如果已变化，以实际
   单一 head 的下一个空闲 revision 为准，并在启动子智能体前广播。不得修改既有 migration。
5. 先写失败测试。禁止删测试、降低断言或用硬编码题目/KC 制造绿灯。

### 并行任务 A：ORM 与 additive migration

责任文件：

- `apps/api/src/eduagents_api/models.py` 中 `KnowledgeComponent` 相关区域；
- 一个新的 Alembic revision；
- migration/model/RLS 的定向测试；
- 必要时只更新直接受影响的数据模型文档，最终文档仍由主智能体审查。

实现要求：

1. 为 `knowledge_components` 增加：
   - `source_kind: enrichment | chapter_fallback`；
   - `stable_key`（nullable）；
   - `superseded_by_id`（nullable self FK，删除策略必须避免破坏历史证据）。
2. 历史 KC 默认并回填为 `source_kind=enrichment`；新列上线后约束不能为空。
3. 增加 `(kb_id, stable_key)` 的条件唯一索引，只约束 `stable_key IS NOT NULL`。
4. 增加 `source_kind` check constraint；self FK、索引和 downgrade 必须完整。
5. migration 必须 additive，不删除已有 KC、MasteryRecord、Question 或学习事件。
6. 测试覆盖：空库 upgrade、带历史 KC upgrade、唯一索引、非法 source_kind、self FK、downgrade、
   `edu_app` 角色可通过原 RLS 路径读写新增字段。

禁止修改 Worker、Quiz graph、Web 或 Wave 1A 文件。

### 并行任务 B：Worker 章节 KC bootstrap 与 live 升级策略

责任文件：

- 新增 `apps/worker/src/eduagents_worker/kc_bootstrap.py`；
- `apps/worker/src/eduagents_worker/ingestion.py`；
- `apps/worker/src/eduagents_worker/enrichment.py`；
- Worker/KC bootstrap 定向测试。

实现要求：

1. stub 模式在 child chunks 已落库后幂等运行章节 KC bootstrap，不再直接以 `ready` 结束却留下空 KC。
2. 从 `heading_path` 取最深非空章节名；stable key 使用规范化完整路径：
   `chapter:<normalized-full-heading-path>`。
3. 空 heading 使用“文档名 · 未分章节”，stable key 必须包含 doc ID，避免不同文档碰撞。
4. KC ID 使用 `uuid5(kb_id, stable_key)`；同一 KB 重跑 ID 不变。
5. 每个 child chunk 至少绑定一个真实存在且属于同 tenant/KB 的章节 KC ID。
6. 章节数设置明确上限；超出时稳定合并到父级或“其他章节”，禁止一段一个 KC。
7. 并发/重复 job 必须幂等；不能先查后插留下竞态，需依赖唯一约束并正确处理冲突。
8. live/record/replay 保留 enrichment 入队；文档状态必须能区分“基础 ready 但 KC 尚未可用”和
   “enrichment KC 可用”。不得因为词表里只有 `chapter_fallback` 就声称真实 enrichment 完成。
9. fallback 切 live 时优先原位提升章节 KC，保留已有 KC ID 和学习证据；只有高置信映射才能
   `superseded_by_id`。未命中且已有证据的 fallback KC 不删、不换 ID。
10. chunk 回写、KC 升级/归并需事务化、可审计、可重复执行；日志不得包含文档正文或模型原文。

测试至少覆盖：相同标题幂等、同名不同路径、空 heading、跨文档、数量上限、并发重复、
tenant/KB 隔离、child chunk 全覆盖、fallback 原位提升、无法高置信匹配时保留历史 KC。

禁止修改 ORM/migration revision、Web 和 Wave 2 recommendation UI。

### 并行任务 C：API 不变式、错误契约与闭环集成测试

责任文件：

- `apps/api/src/eduagents_api/services.py`；
- `apps/api/src/eduagents_api/routers/quiz.py` 的 Wave 1B 最小范围；
- KC/Quiz/LearningEvent/Mastery 相关 unit/integration tests；
- 如需新增只读 helper，可放 API 服务层，但不要提前实现 Wave 2 的 `/api/practice/next`。

实现要求：

1. `_resolve_kc_ids` 继续“只解析、不自由创建”。错别字或未命中不得污染 KC 词表。
2. 增加明确的 KC readiness/归属检查：目标 KC 必须真实存在并属于当前 tenant/KB。
3. 用于画像冷启动的 Quiz 在任何 Question 的 `kc_ids` 为空时不得落成可用于画像的成功卷；返回
   具体、可测试的错误码，不得退化为 `succeeded` 或静默跳过。
4. live/record/replay 在 enrichment KC 尚未可用时返回 preparing/not-ready 语义；stub 在章节 KC
   bootstrap 完成后才允许冷启动。不得只在前端禁按钮。
5. `record_attempts`、LearningEvent、MasteryRecord 和 MasteryLog 必须使用同一批 KC ID；跨 tenant、
   跨 KB 或被 supersede 后的错误映射必须拒绝或按明确规则解析。
6. 新增闭环集成测试：stub ready → 章节 KC → 3 题（每题 KC 非空）→ submit → learning event
   → mastery record/log → profile 至少一个 KC。
7. 三题初始诊断分布固定为 `easy=1, mid=2, hard=0`；不得把它冒充已自适应。生成提示中的
   `kc_names` 必须限制在目标 KC 白名单，未命中应进入有界修复或失败。
8. 保持普通高级 Quiz、教师 required confirmation 和 Wave 1A outcome v2 回归。

如果完成闭环必须新增跨域接口，先把最小接口提案发送给主智能体，由主智能体决定；不要自行
创建 Wave 2 recommendation service 或修改学生主页面。

### 主智能体集成责任

三个子任务完成后，主智能体必须亲自阅读完整 diff，不能只引用子智能体总结。按以下顺序整合：

1. 先审查并落定 ORM/migration 契约；
2. 让 Worker bootstrap 使用最终字段名、约束和 revision；
3. 让 API readiness 与 Worker 文档状态、KC source_kind 使用同一语义；
4. 补齐任何跨域测试夹具，但不扩大到 Wave 2；
5. 检查 Wave 1A 文件无意外回退，特别是 outcome v2、生成诊断、8400 初始预算和 OpenCode 路由；
6. 更新 `docs/data-model.md`、`docs/api-contract.md`、`docs/development.md` 仅限已落地事实；
7. 不把本实施提示词或规格中的目标写成已完成，除非测试和本地证据真实通过。

### 本地验收要求

服务已停止，可按仓库标准命令在 WSL 本地启动；不需要部署生产。验收只使用本地数据库、Redis、
Qdrant、API、Worker 和 Web。

自动化门禁：

1. `git diff --check`；
2. `python -m alembic heads`，必须只有一个 head；
3. 全新数据库从 base upgrade 到 head；
4. 当前本地数据库 upgrade 到 head；
5. 新 migration 定向 upgrade/downgrade/upgrade；
6. KC bootstrap unit tests；
7. stub 冷启动闭环 integration test；
8. live enrichment barrier/fallback 升级的 fake/replay tests；
9. Quiz outcome、SSE、provider routing 回归测试；
10. `make test-unit`；
11. `make test-integration`；
12. `make test-web`；
13. `make lint`；
14. `make docs-check`。

本地浏览器验收至少完成：

1. 新建一个专用测试 KB，上传带 3 个不同章节的资料；
2. 在 stub 路径确认生成稳定章节 KC，chunk 预览中每个 child chunk 有 KC；
3. 生成 3 道诊断题，断言每题 `kc_ids` 非空且属于该 KB；
4. 作答提交，断言 LearningEvent、MasteryRecord、MasteryLog 的 KC 一致；
5. 打开画像页，至少展示一个来自该 KB 的真实 KC；
6. 重跑 bootstrap，KC 数量和 ID 不变；
7. 桌面 `1440×1024` 和移动 `390×844` 截图写入新的
   `docs/evidence/ai-first-wave-1b-local-2026-08-09/`，不得覆盖既有证据；
8. 截图、JSON 和日志不得包含邮箱、密码、token、API key 或文档敏感正文。

真实 OpenCode smoke 不是每次测试必跑。若免费端 429，记录为外部限流，不得改回旧 provider、
降低 KC 断言、打开 SiliconFlow fallback 或循环重试消耗额度。

### 必须暂停并报告的条件

- migration 已出现多 head，且无法无损整合；
- fallback → enrichment 需要删除或重写已有 MasteryRecord/Question/LearningEvent；
- 只能通过在 `_resolve_kc_ids` 中自由创建 KC 才能通过测试；
- 当前数据模型无法区分 child/parent chunk，或 heading_path 不足以稳定构造章节；
- 需要修改生产数据、部署生产、调用付费 provider 或删除非空用户表；
- Wave 1A 的已有修改与 Wave 1B 发生无法绕开的冲突。

### 最终交付格式

完成 Wave 1B 后暂停，不进入 Wave 2。报告必须包含：

1. 根因与最终模式分支（stub / live-record-replay）；
2. 修改文件清单和 migration revision；
3. KC 不变式如何被代码与数据库共同保证；
4. 自动化测试命令和真实结果；
5. 本地浏览器验收路径与截图目录；
6. 当前本地/生产分别是否通过，禁止把本地通过写成生产通过；
7. 残余风险和进入 Wave 2 前的明确门槛。

未经用户另行授权，不提交 Git、不部署、不修改生产、不删除既有证据。

## 为什么这样并行

三个任务的主要写入域分别是“ORM/migration”“Worker”“API/tests”，可以同时推进。真正共享的
字段名、revision 和 readiness 语义由主智能体先固定、最后整合。不要让两个子智能体同时改
`models.py`、同一个 migration 或同一批 Worker 文件，否则并行带来的冲突会超过收益。
