# Luna 单智能体串行实施提示词：完成 AI-first 剩余工作（2026-08-09）

> 分类：Implementation handoff
> 用法：新建一个 GPT-5.6 Luna 会话，把“可复制提示词”章节完整发送。
> 执行方式：只使用主会话，禁止创建子智能体；按 Wave 串行实现并本地验收。

## 当前还差什么

Wave 1A 已完成；Wave 1B 的数据库、章节 KC、chunk 绑定、KC barrier 和学习证据闭环主体已完成。
剩余工作依次是：

1. Wave 1B 收口：修复 Tutor 单测环境隔离；增加一条真正从 stub ingestion 到画像的组合测试。
2. Wave 2：下一步学习决策器、零配置练习、自动蓝图、推荐理由和事后调整。
3. Wave 3：OCR 逐字段置信度契约、服务端 eligibility、一键确认和低置信字段 UI。
4. Wave 4：辅导三入口；计划页一句话目标和按需追问。
5. Wave 5A：9/10/11px 机械治理和防回归检查。
6. Wave 5B：12px/`text-xs` 按语义审查，不做无脑全局替换。
7. 最终本地桌面/移动浏览器验收、文档同步和全量门禁。

## 可复制提示词

你正在 `/home/shunw/eduagents` 仓库工作。请完成
`docs/AI_FIRST_LEARNING_UX_IMPLEMENTATION_SPEC_2026-08-09.md` 中 Wave 1B 尚未收口的部分，
然后依次完成 Wave 2、Wave 3、Wave 4、Wave 5A、Wave 5B。

### 执行方式

- 只使用当前 Luna 主会话，**不得创建或委派子智能体**，不得调用 `spawn_agent`。
- 使用计划工具维护步骤，一次只能有一个 `in_progress`。
- 严格串行：当前 Wave 的代码、测试和本地证据未通过，不得进入下一 Wave。
- 不需要每个 Wave 等待用户回复；只要门禁通过就继续。只有遇到本文“必须暂停”的情况才停下。
- 不提交 Git，不部署，不操作远端环境，不删除用户数据。
- 所有文件修改使用 `apply_patch`；搜索优先 `rg`；保留当前所有未提交修改。

### 当前事实与禁止回退项

1. Wave 1A 已完成，必须保留：
   - Quiz SSE outcome v2；
   - `succeeded / cancelled_by_user / generation_failed / validation_failed`；
   - 脱敏生成诊断；
   - OpenCode 路由覆盖 `Tier.cheap`；
   - 生成 token 预算与 180 秒本地 gateway timeout；
   - 前端真实错误分类和可重试上下文。
2. Wave 1B 已有 migration `0028`，包括：
   - `source_kind`；
   - `stable_key`；
   - `superseded_by_id`；
   - `(kb_id, stable_key)` 条件唯一索引。
3. stub 已有章节 KC bootstrap；live/record/replay 已有 KC readiness barrier。
4. 本地模型路由只使用 OpenCode Zen；embedding/rerank 使用 stub。不得恢复 DeepSeek 官方或
   SiliconFlow，不得通过 fallback 绕过 429。
5. 工作区包含用户已有修改、旧截图删除、验收证据和脚本。禁止 reset、checkout、覆盖或清理。
6. 服务当前可能未启动；按需要在 WSL 本地启动，任务结束时报告仍在运行的本地进程。

### 开始前审计

完整阅读并记录：

1. `docs/AI_FIRST_LEARNING_UX_IMPLEMENTATION_SPEC_2026-08-09.md`；
2. `docs/data-model.md`、`docs/api-contract.md`、`docs/development.md`；
3. `/home/shunw/.codex/AGENTS.md` 和所有目标目录的 `AGENTS.md`；
4. `git status --short --branch`、`git diff --stat`；
5. `python -m alembic current`、`python -m alembic heads`；
6. 用 `rg` 重新核对实际文件路径和调用方，不把规格中的路径当成绝对事实。

先运行相关测试建立基线。若失败由本地 `.env` 泄漏进单测造成，应修复测试隔离，不能把它标成
“既有无关失败”。

---

## Step 1：Wave 1B 收口

### 1.1 修复 Tutor 测试隔离

文件：`tests/unit/test_tutor_recognition.py`。

- `_settings()` 必须显式设置 `model_routing_enabled=False`，使期待 `fake-vision` 的 legacy 路由测试
  不读取本地 `.env` 的 OpenCode routing 值。
- 另增加或保留一条 routing enabled 的测试，显式断言使用 `opencode_vision_model`。
- 不修改正确的运行时代码来迎合测试。

验收：

~~~bash
pytest -q tests/unit/test_tutor_recognition.py
make test-unit
~~~

要求全绿，不允许留下“710 passed, 1 failed”。

### 1.2 增加真实组合闭环测试

现有 `test_stub_three_question_quiz_uses_one_kc_batch_through_profile` 手工插入 KC，只证明了 API
证据链。新增一条组合 integration test，必须使用真实 Worker 路径：

~~~text
创建用户/KB/三章节文档
→ ingest_document(gateway_mode=stub)
→ bootstrap_document_kcs
→ 从数据库读取真实章节 KC
→ 生成 3 题，每题映射一个真实 KC
→ submit
→ LearningEvent / MasteryRecord / MasteryLog
→ GET /api/profile 至少出现一个 KC
→ 重跑 bootstrap，KC ID 和数量不变
~~~

禁止在测试中手工创建 `KnowledgeComponent`。模型部分使用 fake/replay，测试不能依赖外部限流。

三题诊断的后端纯函数/构造器必须确定性选择最多三个有效 KC，并固定：

~~~json
{"num_questions": 3, "difficulty_mix": {"easy": 1, "mid": 2, "hard": 0}}
~~~

此步骤只建立可供 Wave 2 调用的服务能力，不提前重写页面。不要让普通高级 Quiz 被无条件改成
三题；用明确的内部 cold-start intent 或服务函数区分。

验收：KC bootstrap、Quiz KC、VLSE 定向测试和 integration 全绿。完成后更新计划，进入 Step 2。

---

## Step 2：Wave 2 下一步决策与零配置练习

### 2.1 单一决策服务

新增 `apps/api/src/eduagents_api/practice_recommendation.py`，所有入口共享同一优先级：

1. 有 FSRS 到期错题 → review；
2. 有今日未完成 plan task → 使用 task 的 KB/KC；
3. 有有效薄弱 KC → 自适应 5 题；
4. 有 ready KB、当前用户对该 KB 没有掌握记录 → cold-start 3 题；
5. 多 KB 且无可信上下文 → 只询问一次 KB scope；
6. 无资料 → 上传资料或明确标识的 sample diagnostic。

不得由 Web 复制一套优先级。决策必须返回稳定枚举、标题、理由、题数、KB、scope options 和允许
的 adjustments。理由只能使用真实计划、到期队列、MasteryRecord、近期错误和 ready KB 事实。

### 2.2 API

新增并注册：

- `GET /api/practice/next`；
- `POST /api/quiz/recommended`，复用现有 Quiz SSE/outcome v2。

推荐请求只接受安全上下文：

~~~json
{
  "scope_kb_id": null,
  "plan_id": null,
  "task_key": null,
  "previous_quiz_id": null,
  "adjustment": null
}
~~~

第一版 adjustment 仅允许：`easier | harder | switch_kc | switch_kb`。

服务端必须重新校验 plan/task/KB/KC 归属。决策变化时返回稳定的 409 错误和最新 recommendation。
原 `/api/quiz` 保留为高级/兼容入口。

### 2.3 自动蓝图与教师回归

- 学生推荐练习使用 `confirmation_mode=auto`，仍生成蓝图和 trace，但不 interrupt。
- 教师组卷、导出和显式 required 路径继续 interrupt/resume。
- cold-start 由 Step 1 的后端构造器选择三个真实 KC，固定 `easy=1, mid=2, hard=0`。
- 每题 `kc_ids` 必须非空、属于目标 KB；不命中白名单必须有界修复或失败。

### 2.4 Web 主路径

修改 Quiz/Today 主路径：

- 首屏只显示推荐标题、一行真实理由、一个主 CTA；
- 无 weak points 时 CTA 仍有有效动作；
- 多 KB 仅在确实需要时显示一次 chips；
- 主题、KB、三档难度和蓝图确认移入“高级设置”，不再位于常见主路径；
- 出题后显示“简单一点 / 难一点 / 换个知识点 / 换个科目”；
- loading/preparing/needs_scope/needs_upload/error 均有清晰状态；
- 错误保留上下文重试，不把 KC preparing 显示成生成失败。

学生常见路径主动输入为 0；高级设置仍可访问；教师路径无回归。

### 2.5 Wave 2 门禁

- decision priority 每一分支和并列条件单测；
- API 多租户/机构 KB、plan attribution、decision changed 测试；
- cold-start 真实三题闭环；
- auto 无 interrupt、required 有 interrupt；
- Web 各 recommendation 状态、事后调整和键盘可访问性测试；
- 桌面与移动本地截图。

通过后再进入 Step 3。

---

## Step 3：Wave 3 OCR 逐字段置信度与校对页

### 3.1 additive migration 与数据契约

从实际 Alembic head 分配下一个空闲 revision，禁止修改 `0028`。为 ResourceQuestion 增加：

~~~text
field_confidence_json JSONB NOT NULL DEFAULT '{}'
~~~

固定 schema version 和字段白名单：

~~~text
question_no, page_no, type, stem, options,
detected_student_answer, detected_reference_answer,
answer, explanation, kc_names, difficulty, points
~~~

每个字段记录 `confidence | null`、`source`、有界 `warnings`。未知字段过滤；warnings 数量和长度
有上限；非法数值变成 unknown，不能自动变成 1.0。

来源至少区分：`parser`、`ocr_provider`、`rule`、`model_reported`、`unknown`。未经标注校准的
`model_reported` 不得获得自动确认资格。

### 3.2 Worker 与 API

- Worker 结构化输出逐字段 confidence，并做严格清洗；
- 保留整题 `extraction_confidence` 一版兼容；
- 服务端统一计算 eligibility，前端不能自行宣布高置信；
- 关键字段阈值：`stem/detected_student_answer/answer >= 0.90`；
- 结构字段阈值：`type/options/question_no/page_no >= 0.85`；
- unknown 按低置信；
- 新增“确认全部服务端判定为高置信的题”的批量端点；
- pending/rejected 题仍不能产生 Assessment、ErrorItem、LearningEvent 或掌握度证据；
- 保留已有语义锁定规则。

### 3.3 校对 UI

- 高置信字段渲染为正文，不是常驻输入框；
- 只自动展开低置信/unknown 的关键字段；
- 学生原答案始终可修改；
- 题号、页码和 AI 建议字段放入“修正识别”折叠区；
- 主操作为“确认无误”；
- 20 题仅 1 个低置信字段时，只展开该题该字段；
- 显示“为什么需要确认”，但不伪造置信度准确性。

### 3.4 Wave 3 门禁

- migration upgrade/downgrade/upgrade、RLS 和历史数据兼容；
- confidence 清洗、来源、阈值边界、unknown 单测；
- 批量确认必须由服务端重算；
- pending/rejected 隔离集成测试；
- Web 高/低/unknown 和 20 题单低置信测试；
- 桌面/移动截图。

通过后进入 Step 4。

---

## Step 4：Wave 4 辅导与计划渐进披露

### 4.1 辅导页三入口

按真实上下文展示：

1. 继续今天的任务；
2. 挑一道我的错题；
3. 粘贴或上传新题目。

有上下文时不先展示空 textarea。三入口最终复用现有 Tutor draft/thread、权限校验和题目核验。
不得把题目全文、答案、token 放入 URL；URL 只允许安全 ID。

### 4.2 计划页一句话目标

第一屏只问：“你想在什么时候，达到什么结果？”

- 从自然语言中解析目标与日期；
- 从 KB 和画像推导当前基础、知识范围和建议节奏；
- 仅缺失时追问截止日期/周期和每天可投入时间；
- 日期和可投入时间必须由用户确认；
- 多 KB 范围不清时只用 chips 追问一次；
- 草案生成后允许修改推导值。

### 4.3 Wave 4 门禁

- Tutor 三入口上下文、越权、空状态和 URL 安全测试；
- 计划文本含/不含日期、含/不含每日时间、多 KB 测试；
- 现有 Tutor/Plan graph 回归；
- Web 键盘、移动端和截图验收。

通过后进入 Step 5。

---

## Step 5：Wave 5A 机械最低字号

先重新统计 `apps/web` 当前实际数量，记录基线，不沿用旧数字。

1. 在 Tailwind 4 `@theme`/全局 token 中增加中文 caption token，最低 12px，并设置合理 line-height。
2. 仅机械处理 DOM Tailwind 的 `text-[9px]`、`text-[10px]`、`text-[11px]`。
3. SVG 刻度、图表 canvas、动态 `fontSize` 和打印 CSS 单独审查，不混入机械替换。
4. 新增检查脚本或 lint，禁止新的 DOM 9/10/11px 任意值。
5. 检查按钮高度、表格、长中文换行、KaTeX 和移动端横向溢出。

要求：DOM 9/10/11px 清零；不能通过忽略整个目录获得绿灯。

通过 Web 测试、TypeScript、ESLint、Next build 和截图后进入 Step 6。

---

## Step 6：Wave 5B 12px 语义审查

逐处审查 `text-xs` 和显式 `text-[12px]`：

- 时间戳、状态、图例、次要说明允许保留 12px；
- 按钮、输入内容、正文、题目主元数据至少 13/14px；
- 用 `caption/body-sm/body` token 代替继续散落任意值；
- 不以 12px 出现次数归零为目标；
- 不进行全仓盲目 search/replace；
- 每一类保留/升级都要能解释语义角色。

至少审查 Quiz、Today、Tutor、Plans、Resource Review、Profile、Errors、Reviews、KB、Reports 的桌面
和移动状态。完成后运行 Web 全门禁和视觉截图。

---

## Step 7：最终本地验收与文档

### 7.1 自动化门禁

依次执行并修复本次引入的问题：

~~~bash
git diff --check
python -m alembic heads
make test-unit
make test-integration
make test-web
make lint
make docs-check
~~~

另执行：

- migration 从全新数据库升级到 head；
- 所有新增 migration 的 downgrade/upgrade；
- TypeScript、ESLint、Next build；
- Quiz/KC/SSE、OCR/pending、Tutor/Plan 的定向测试。

Alembic 必须只有一个 head。不得使用 `stamp` 掩盖漏执行的 migration。

### 7.2 本地浏览器旅程

使用专用本地测试账号与新建测试数据，完成桌面 `1440×1024` 和移动 `390×844`：

1. 上传三章节资料；
2. stub ingestion 建立章节 KC；
3. 首屏零配置显示“用 3 道题建立第一版画像”及真实理由；
4. 自动生成 3 题且每题 KC 非空；
5. 作答后画像出现 KC；
6. 再次推荐基于刚才证据，并允许事后调整；
7. OCR 高/低/unknown 校对和一键确认；
8. 辅导三入口；
9. 计划一句话目标与按需追问；
10. 检查字号、换行、焦点、横向溢出和错误状态。

浏览器验收可通过进程级环境覆盖使用 stub/fake/replay，不永久改写当前 OpenCode `.env`。外部 429
不得阻止确定性本地验收，也不得通过循环请求或恢复旧 provider 解决。

证据写入新的：

~~~text
docs/evidence/ai-first-complete-local-2026-08-09/
~~~

不得覆盖既有证据；截图和 JSON 不得包含邮箱、密码、token、API key 或敏感文档正文。

### 7.3 文档同步

只把真实落地内容同步到 Active 文档：

- `docs/api-contract.md`；
- `docs/data-model.md`；
- `docs/development.md`；
- `docs/evidence/README.md`；
- 必要的 `docs/README.md`。

历史提示词和规格继续标识为 reference/implementation handoff，不把目标描述成既成事实。

---

## 必须暂停并报告

只有以下情况暂停：

- migration 出现无法无损合并的多 head；
- 必须删除或重写非空用户数据才能继续；
- 只能通过伪造 KC、置信度、题目、模型结果或降低断言才能通过；
- 需要恢复已禁用 provider；
- 现有用户修改与目标文件发生无法绕开的语义冲突；
- 关键产品选择无法从规格或代码事实确定，且不同选择会产生不兼容数据契约。

普通测试失败、实现复杂、上下文压缩或外部 429 都不是停止理由。继续使用确定性本地测试完成可验证
部分，并如实记录外部限制。

## 最终交付格式

全部完成后一次性报告：

1. 每个 Wave 的实际完成内容；
2. 修改文件和 migration revision；
3. 学生主路径还剩多少主动输入，以及每个保留输入为什么必须由用户决定；
4. KC、LearningEvent/BKT、OCR eligibility 的不变式；
5. 自动化测试的真实命令和结果；
6. 本地桌面/移动证据目录；
7. 未解决风险和明确未完成项。

如果任一 Wave 未达到门禁，禁止写“全部完成”。未经用户授权，不提交 Git。
