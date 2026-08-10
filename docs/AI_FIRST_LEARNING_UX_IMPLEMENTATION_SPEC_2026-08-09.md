# AI 优先学习体验实施规格（2026-08-09）

> 状态：待实施
> 分类：Reference
> 目标读者：产品、前端、API、Worker、测试，以及负责实现的 Luna / AI 编程代理
> 事实优先级：运行时代码、Alembic migration、自动化测试高于本文
> 重要说明：本文是实施规格，不代表其中功能已经实现

## 1. 结论

核心方向成立：学生主路径应从“先配置、再确认、再执行”改成“系统根据已有证据直接给出下一步，学生只在不满意时调整”。但它不是单纯删表单，必须先补齐三个底层前提：

1. 正式出题必须可靠，并且不能把生成失败误报成用户取消。
2. 每道可计入画像的题必须绑定真实存在的 KC；stub 模式要有独立的章节 KC 冷启动方案。
3. 校对页要先有逐字段置信度契约，才能只让用户处理低置信字段。

统一判断标准：

> 每留下一个主动输入，必须回答两件事：系统为什么不知道，以及为什么决定权属于用户。任一答不上来，就由系统先执行，并把修改权放到事后。

因此，考试日期、每天可投入时间仍属于用户决策；练习主题、知识库、难度配比、AI 推断的题型/KC/难度通常不应成为学生主路径的前置表单。

## 2. 范围与非目标

本轮规格覆盖：

- 学生下一步练习决策与零配置启动；
- Quiz 生成可靠性、SSE 终态和错误展示；
- live/replay/record 与 stub 模式下的 KC 冷启动；
- 学生练习的自动蓝图与事后调整；
- OCR 校对的逐字段置信度与一键确认；
- 辅导页三入口；
- 计划页的一句话目标和按需追问；
- 9/10/11px 机械治理与 12px 语义治理；
- 对应自动化测试、feature flag、回滚和生产浏览器验收。

本轮不做：

- 不删除教师组卷、导出或教师的前置确认；
- 不允许 pending/rejected 题进入学习状态；
- 不用 stub 伪造真实模型质量、真实 OCR 置信度或真实 enrichment；
- 不让 `_resolve_kc_ids` 在判分/落库时静默创建自由文本 KC；
- 不重写 BKT、FSRS 或现有掌握度算法；
- 不把所有计划输入压成零输入；
- 不一次性机械替换所有 `text-xs` 或显式 12px；
- 不覆盖现有生产验收证据。

## 3. 已证实的当前事实

| 事实 | 代码/证据 | 影响 |
|---|---|---|
| stub 摄取不入队 enrichment | `apps/worker/src/eduagents_worker/ingestion.py:392` | stub 下不会自动生成 KC 词表 |
| enrichment 是当前唯一 KC 创建链路 | `apps/worker/src/eduagents_worker/enrichment.py:260` | 不补分支时 KC 表会一直为空 |
| `_resolve_kc_ids` 未命中即丢弃 | `apps/api/src/eduagents_api/services.py:183` | Question 可落成 `kc_ids=[]`，判分无法写 BKT |
| 后端已有按掌握度算难度的能力 | `apps/api/src/eduagents_api/routers/quiz.py:26`、`:107` | 学生不应手填难度配比 |
| 无弱点时后端明确返回 `NO_WEAK_POINTS` | `apps/api/src/eduagents_api/routers/quiz.py:113` | 只改前端 CTA 不能解冷启动 |
| 前端在无弱点时禁用主 CTA | `apps/web/app/quiz/page.tsx:905` | 与后端共同形成冷启动死锁 |
| Quiz 空 `done` 被统一解释为蓝图未确认 | `apps/web/app/quiz/page.tsx:348` | 生成失败被误报为用户取消 |
| 生产正式出题连续三轮生成 0 题 | `docs/evidence/production-2026-08-09/README.md` | 当前 release blocker |
| OCR 只有整题 `extraction_confidence` | `models.py:694`、`routers/resources.py:64`、`types.ts:1484` | 不能仅靠 UI 实现逐字段高亮 |
| 教师组卷与导出真实存在 | `apps/api/src/eduagents_api/routers/teacher.py`、`apps/web/app/teacher/page.tsx` | 教师路径继续保留前置确认 |
| 今日任务已携带 topic/kb/count/difficulty/plan/task 上下文 | `apps/web/app/today/today-plan.ts` | 不应让学生再次填写相同信息 |

KC 冷启动的实际断点是：

~~~text
gateway_mode=stub
  → ingestion 不入队 enrich_document
  → knowledge_components 为空
  → _resolve_kc_ids 返回 []
  → Question / LearningEvent 没有有效 KC
  → BKT 不更新
  → 画像长期显示 0 个知识点
~~~

## 4. 目标用户旅程

### 4.1 有学习记录的学生

1. 今日页或练习页展示一个主动作，例如“开始今天的 5 道题”。
2. 按“到期复习 → 今日计划 → 薄弱 KC”的优先级自动决定内容。
3. 主动作下展示一行基于真实证据的理由，例如“极限的定义最近仍不稳定，今天先补 5 题”。
4. 点击后直接生成题，不经过学生蓝图审批。
5. 出题后才出现“简单一点 / 难一点 / 换个知识点 / 换个科目”。

### 4.2 刚上传一份资料的新学生

1. 文档 ready 后，系统建立可用 KC：真实模式等待 enrichment，stub 模式创建章节 KC。
2. 主 CTA 为“用 3 道题建立第一版画像”，不要求主题、难度或配比。
3. 三题必须带有效 `kc_ids`。
4. 作答后产生第一批 MasteryRecord / MasteryLog，画像不再是 0。

### 4.3 多知识库但没有足够上下文

系统只有在无法可靠判断科目范围时提问一次，使用知识库 chip，不展示完整配置表。选择后记住本次范围；出题后仍允许换科目。

### 4.4 没有任何资料

不要显示不可点击的“个性化练习”。展示：

- 主动作：“上传一份正在学的资料”；
- 次动作：“先做 3 题样例诊断”，并明确样例诊断不是基于个人资料。

## 5. 下一步练习决策器

新增单一服务 `practice_recommendation.py`，API 和页面都使用它，禁止前后端各写一套优先级。

### 5.1 决策优先级

| 优先级 | 条件 | 动作 | 问题数 | 理由来源 |
|---:|---|---|---:|---|
| 1 | 有 FSRS 到期错题 | 跳转 `/reviews` | 到期队列 | 到期数量与调度原因 |
| 2 | 有今日未完成 plan task | 按 task 的 KB/KC 出题 | task.count 或 5 | 计划目标、阶段、任务 KC |
| 3 | 有有效薄弱 KC | 自适应练习 | 5 | 定性掌握区间、近期错误 |
| 4 | 有 ready KB、该 KB 尚无掌握记录 | 冷启动诊断 | 3 | “先建立第一版画像” |
| 5 | 多个 KB 且无可信范围 | 只问一次 KB chip | 0 | “需要确认这次练哪一科” |
| 6 | 无可用资料 | 上传或样例诊断 | 3 | 明确当前无个人资料 |

约束：

- `review` 优先时不再额外生成 Quiz。
- 计划 task 的 `plan_id/task_key/kb_id` 必须由服务端重新校验归属。
- 前端传来的 KC 名、难度配比只视为高级调整意图，不能覆盖服务端的安全与归属判断。
- 多 KB 时，活跃计划的 KB 是高置信上下文；没有计划时才询问一次范围。
- 理由使用定性描述，默认不暴露 `p_mastery` 小数。

### 5.2 建议 API

新增 `GET /api/practice/next`：

~~~json
{
  "status": "ready",
  "action": "quiz",
  "source": "cold_start",
  "title": "用 3 道题建立第一版画像",
  "reason": "这份资料刚准备好，先从三个章节各取一道题了解你的起点。",
  "question_count": 3,
  "kb": {"id": "...", "name": "高等数学"},
  "scope_options": [],
  "adjustments": ["switch_kb"]
}
~~~

字段枚举：

- `status`: `ready | needs_scope | preparing | needs_upload`；
- `action`: `review | quiz | choose_scope | upload | sample_diagnostic`；
- `source`: `due_review | plan_task | weak_kc | cold_start | sample`。

新增 `POST /api/quiz/recommended`，返回 Quiz SSE。请求只接受上下文和事后调整，不接受自由难度配比：

~~~json
{
  "scope_kb_id": null,
  "plan_id": null,
  "task_key": null,
  "previous_quiz_id": null,
  "adjustment": null
}
~~~

`adjustment` 第一版只允许：`easier | harder | switch_kc | switch_kb`。服务端重新运行决策器；如果动作已变成 review/scope/upload，返回 `409 PRACTICE_DECISION_CHANGED` 和最新 recommendation，不能继续用旧上下文静默出题。

原 `POST /api/quiz` 保留一版兼容，用于高级设置、教师路径和旧链接；学生主 CTA 不再直接组装 topic/KC/difficulty payload。

## 6. Quiz 可靠性与终态契约（P0）

### 6.1 先修生成，再移除表单

生产问题不能用“按钮可点击”掩盖。Luna 必须先复现三轮 0 题，并为每轮记录有界诊断：

- provider/model/feature（不得记录密钥）；
- 响应字符数、finish reason、JSON parse 是否成功；
- 顶层 key、原始问题数、通过 schema 的问题数；
- 是否截断、重试轮次、最终分类；
- request_id/thread_id，便于关联 usage 和网关日志；
- 不把完整题干或学生资料写入普通日志。

修复目标不是“永远生成成功”，而是：正常模型响应能稳定解析和落库；异常时给出真实、可重试的错误，绝不发空成功。

### 6.2 Quiz `done` 契约 v2

Quiz 的 `done` 必须带明确 outcome：

~~~json
{
  "schema_version": 2,
  "thread_id": "quiz:...",
  "outcome": "generation_failed",
  "result": null,
  "error": {
    "code": "QUIZ_GENERATION_EMPTY",
    "message": "这次没有生成出可用题目，请重试。",
    "stage": "generate",
    "retryable": true,
    "attempts": 3
  }
}
~~~

`outcome` 只能是：

- `succeeded`：题目已校验、已落库，`result.questions.length > 0`；
- `cancelled_by_user`：只在用户明确拒绝/取消时使用；
- `generation_failed`：所有生成轮次都没有得到非空题目数组，或响应无法形成题目集合；
- `validation_failed`：至少生成过一题，但 schema 校验在上限内仍未通过。

基础设施异常（网关不可达、数据库失败、图运行异常）继续使用 SSE `error`，但错误码必须具体；禁止用空 `done.result={}` 代替。

前端映射：

| outcome/event | UI | 操作 |
|---|---|---|
| `succeeded` | 展示题目 | 开始作答 |
| `cancelled_by_user` | “已取消本次出题” | 返回/重新开始 |
| `generation_failed` | “没有生成出可用题目” | 保留上下文重试 |
| `validation_failed` | “题目质量检查未通过” | 保留上下文重试 |
| SSE `error` | “服务暂时不可用”+错误编号 | 重试/稍后再试 |

不得再通过“有没有 questions”猜测蓝图是否确认。

### 6.3 图状态要求

在 `QuizState` 增加有界字段：

~~~text
confirmation_mode: auto | required
terminal_outcome: succeeded | cancelled_by_user | generation_failed | validation_failed
generation_status: ok | empty | invalid_json | invalid_shape
ever_generated_nonempty: bool
failure_code: str
~~

- 学生推荐练习使用 `confirmation_mode=auto`；图仍生成蓝图和 trace，但跳过 interrupt。
- 教师、批量组卷和显式高风险操作使用 `required`。
- validate 用 `ever_generated_nonempty` 区分 generation 与 validation failure。
- 成功落库前检查题目数和每题 KC；落库失败不能发 `succeeded`。

## 7. KC 冷启动设计（P0，与 Quiz 修复并行）

### 7.1 不变式

以下条件必须同时成立，练习才可写入画像：

1. 每个目标 KC 在 `knowledge_components` 中真实存在并属于当前 tenant/KB；
2. 推荐练习的每道 Question 至少解析出一个目标 `kc_id`；
3. `record_attempts`、LearningEvent 和 BKT 使用同一批 KC ID；
4. `_resolve_kc_ids` 继续“只解析、不自由创建”，防止错别字污染词表；
5. `kc_ids=[]` 的题不能被宣称为“完成画像冷启动”。

### 7.2 live / record / replay：enrichment barrier

- 摄取完成后按现有队列运行 `enrich_document`。
- `GET /api/practice/next` 只有在选中 KB 存在有效 KC 后才返回 `ready/quiz`。
- 文档 ready 但 enrichment 未完成时返回 `preparing`，页面显示“正在理解资料并整理知识点”，自动轮询，不显示死按钮。
- enrichment 失败要显示可重试状态并记录错误；不能降级成空 KC 出题。
- 直接访问 `/quiz` 也必须经过同一 barrier，不能绕过。

### 7.3 stub / 演示：章节 KC

新增 `apps/worker/src/eduagents_worker/kc_bootstrap.py`，在 stub 摄取的 child chunks 已落库后幂等执行：

1. 从 `heading_path` 取最深的非空章节名；同名冲突时用完整路径区分。
2. 空 heading 使用“文档名 · 未分章节”，stable key 包含 doc ID。
3. stable key 为 `chapter:<normalized-full-heading-path>`。
4. KC ID 使用 `uuid5(kb_id, stable_key)`，同一 KB 重跑不变。
5. 每个 child chunk 至少回写一个章节 KC ID。
6. 设置数量上限；超出时合并到父级/“其他章节”，禁止一段一个 KC 造成爆炸。
7. stub 只声称“从章节结构建立临时画像”，不得声称完成真实语义 enrichment。

为避免以后从 stub 切到 live 出现双词表，给 `knowledge_components` 增加：

~~~text
source_kind: enrichment | chapter_fallback
stable_key: nullable string
superseded_by_id: nullable self FK
~~

并增加 `(kb_id, stable_key)` 的条件唯一索引。迁移中的历史 KC 默认 `source_kind=enrichment`。

切换到真实 enrichment 时：

- 不能仅用“词表非空”判断已完成真实词表；只有 enrichment KC 才满足该条件。
- 对生成的语义 KC 与章节 KC 做规范化名称/别名/embedding 匹配。
- 命中时保留章节 KC 的原 ID，原位提升为 enrichment KC，避免 MasteryRecord 断链。
- 未命中但已有学习证据的章节 KC 不删除、不改 ID；只有高置信映射后才能设置 `superseded_by_id`。
- 所有 chunk、Question JSON KC 快照和 MasteryRecord 的迁移必须可审计、可幂等；禁止静默删除历史画像。

### 7.4 冷启动题选取

- 从选中 KB 的有效 KC 中稳定选最多 3 个，优先覆盖不同章节。
- 初始难度不是“自适应”伪装：固定诊断分布 `easy=1, mid=2, hard=0`。
- 文案明确“这是建立起点的诊断题”；答完后下一轮才使用 `adaptive_difficulty_mix`。
- graph 的 validate 要求 `kc_names` 至少命中目标 KC 白名单；否则打回生成修复。

## 8. 学生练习页

### 8.1 主路径

删除主路径中的：主题输入、绑定知识库、三个难度数字框、蓝图确认步骤。页面首屏只保留：

- 推荐标题；
- 一行“为什么是这个”；
- 一个主 CTA；
- 必要时一次 KB 范围 chips；
- “高级设置”折叠入口。

主 CTA 不能因为 `weak_points.length===0` 永久禁用。它按 recommendation 显示为开始练习、继续复习、准备资料或上传资料。

### 8.2 事后调整

题目生成后展示：

- “简单一点”；
- “难一点”；
- “换个知识点”；
- 多 KB 时“换个科目”。

点击后通过 `previous_quiz_id + adjustment` 生成新一套；旧卷保留审计但标记为被替换，未作答旧卷不产生学习证据。调整理由进入 trace。

### 8.3 高级设置

高级设置是逃生口，不是主流程。可保留题数、范围和教师需要的蓝图；不得重新把难度配比作为学生必填。学生选择“更难/更简单”是相对意图，最终分布仍由后端约束。

## 9. OCR 校对：先扩契约，再改 UI

### 9.1 数据契约

保留整题 `extraction_confidence` 一版兼容，新增 `field_confidence_json JSONB NOT NULL DEFAULT '{}'`，格式固定为：

~~~json
{
  "schema_version": 1,
  "fields": {
    "stem": {"confidence": 0.98, "source": "ocr_provider", "warnings": []},
    "detected_student_answer": {"confidence": 0.61, "source": "ocr_provider", "warnings": ["handwriting"]},
    "page_no": {"confidence": 1.0, "source": "parser", "warnings": []}
  }
}
~~~

允许字段：`question_no/page_no/type/stem/options/detected_student_answer/detected_reference_answer/answer/explanation/kc_names/difficulty/points`。API 必须过滤未知字段、限制 warnings 数量和长度。

Worker 的结构化输出同时返回 `field_confidences`；缺失、非法或 stub 产生的置信度一律返回 `null/unknown`，不能默认成 1.0。置信度来源必须可区分：parser 的确定性定位、OCR provider 原生分数、规则告警和模型自报分数不能混成同一种证据。未经标注集校准的 LLM 自报 confidence 只记录为 `model_reported`，不得直接获得“高置信自动确认”资格。

### 9.2 阈值与行为

第一版阈值作为服务端常量并带 schema version；只有 `parser`、已校准的 `ocr_provider` 或通过离线标注验证的组合分数可进入阈值判断：

- 学习关键字段 `stem/detected_student_answer/answer`：`>=0.90` 为高置信；
- 结构字段 `type/options/question_no/page_no`：`>=0.85` 为高置信；
- AI 建议字段 `kc_names/difficulty/points/explanation`：默认只读展示，可主动修改，不作为阻止确认的唯一原因；
- `unknown` 按低置信处理。

阈值上线前至少用一批覆盖印刷体、手写答案、公式和低清扫描件的人工标注数据计算字段级 precision/recall；自动确认优先保证 precision。阈值以后只能根据同口径标注数据调整，不能用视觉感觉改。

### 9.3 页面行为

- 高置信字段以正文展示，不渲染成常驻输入框。
- 只自动展开低置信/unknown 的关键字段，并给出“这里识别得不确定”。
- 学生原答案始终提供“修改”入口，因为只有本人能确认当时答案。
- 题号、页码和 AI 建议字段放在“修正识别”折叠区。
- 主操作为“确认无误”；有 20 题、只有 1 题低置信时，只让用户处理那 1 题。
- 增加“确认全部高置信题”批量 API；服务端重新计算 eligibility，前端不能自行宣布 verified。
- pending/rejected 题仍不得进入 Assessment、ErrorItem、MasteryRecord、LearningEvent。
- 已产生 FSRS/BKT 证据后的语义锁定规则继续保留。

## 10. 辅导页与计划页

### 10.1 辅导页三入口

按上下文优先展示：

1. “继续今天的任务”：带 plan/task/resource 归属进入辅导；
2. “挑一道我的错题”：从本人 ErrorItem 中选择；
3. “粘贴或上传新题目”：作为第三入口。

有可继续上下文时，不要先展示空 textarea。所有入口最终复用现有 Tutor draft/thread 和权限校验，不能把题目全文放进 URL。

### 10.2 计划页

计划页不能追求零输入。第一屏只问一句：“你想在什么时候，达到什么结果？”允许用户自然写入考试日期和目标。系统从资料和画像推断当前基础、知识范围和建议节奏。

只有在句子中缺失时再追问：

- 截止日期/周期；
- 每天可投入时间。

它们必须由用户决定，因为系统不知道现实日程，也不应替用户承诺。`current_level/target_outcome/success_criteria/kb_id` 默认由上下文推导并在计划草案后可改；多 KB 范围不明确时用 chips 追问一次。

## 11. 字号治理拆成两轮

截至 2026-08-09，`apps/web` 的基线统计为：

| 类别 | 数量 | 处理 |
|---|---:|---|
| `text-[9px]` | 9 | 第一轮机械上调 |
| `text-[10px]` | 247 | 第一轮机械上调 |
| `text-[11px]` | 195 | 第一轮机械上调 |
| `text-[12px]` | 95 | 第二轮语义审查 |
| `text-xs` | 约 494 | 第二轮语义审查 |

所以“≤12px 共 546 处”不能作为一个机械替换批次。真正可机械处理的是 9/10/11px 共 451 处。

### 第一轮：机械最低字号

- 在 Tailwind 4 `@theme` 中增加明确的中文 caption token，最低 12px，并带合适 line-height。
- 将 `text-[9px|10px|11px]` 机械替换到最低 token。
- SVG 图表刻度、动态 `fontSize`、打印 CSS 单独列出，不混入 DOM Tailwind 替换。
- 增加 lint/检查脚本，阻止新的 DOM `text-[9px|10px|11px]`。
- 完成桌面 1440px 和移动 390px 截图，检查换行、按钮高度和表格溢出。

### 第二轮：12px 语义审查

逐处判断 `text-xs` 和显式 `text-[12px]`：

- 时间戳、状态、图例、次要说明可保留 12px；
- 按钮、输入内容、正文、题目元数据主信息至少升为 13/14px；
- 不以“出现次数归零”为目标，以语义角色和可读性为目标；
- 用 `caption/body-sm/body` token 代替继续散落任意值。

## 12. 分阶段实施顺序

### Wave 1A：Quiz release blocker

1. 复现并诊断正式出题 0 题。
2. 加入 Quiz outcome v2 和前端三类真实错误。
3. 正常响应稳定生成、校验、落库；异常不再空成功。

### Wave 1B：KC 冷启动（可与 1A 并行开发，合并前联合验收）

1. live enrichment barrier。
2. stub 章节 KC、稳定 ID、chunk 回写和 migration。
3. 三题作答后画像出现 KC 的闭环测试。

### Wave 2：下一步决策与零配置练习

1. 单一 decision service 和 `/api/practice/next`。
2. 学生自动蓝图、主 CTA 永不死锁。
3. “为什么是这个”和事后调整。
4. 教师确认路径回归。

### Wave 3：OCR 契约和校对页

1. additive migration、Worker 输出、API 类型。
2. 低置信字段 UI、一键确认和批量服务端校验。
3. pending 数据隔离回归。

### Wave 4：辅导/计划渐进披露

1. 辅导三入口。
2. 计划一句话目标和按需追问。

### Wave 5A / 5B：字号

1. 先机械治理 9/10/11px。
2. 独立 PR 审查 12px/`text-xs` 的语义。

任何 Wave 未通过自己的 Definition of Done，不进入下一 Wave。Wave 1A 和 1B 可开发并行，但发布必须同时通过，避免“能出题却不写画像”或“有 KC 却出不了题”。

## 13. 文件级修改清单

| Wave | 主要文件 | 修改责任 |
|---|---|---|
| 1A | `packages/core/src/eduagents_core/graphs/quiz_graph.py` | 生成诊断、自动确认路由、明确终态 |
| 1A | `packages/core/src/eduagents_core/graphs/state.py` | QuizState 新字段 |
| 1A | `apps/api/src/eduagents_api/routers/threads.py` | Quiz done v2、禁止空成功 |
| 1A | `apps/api/src/eduagents_api/routers/quiz.py` | request mode、错误码、落库前不变式 |
| 1A | `apps/web/app/quiz/page.tsx`、`apps/web/lib/types.ts` | outcome 映射、重试保留上下文 |
| 1B | `apps/worker/src/eduagents_worker/ingestion.py` | stub bootstrap 调用、live enqueue/barrier 状态 |
| 1B | 新增 `apps/worker/src/eduagents_worker/kc_bootstrap.py` | 章节 KC 稳定生成和 chunk 绑定 |
| 1B | `apps/worker/src/eduagents_worker/enrichment.py` | 区分 fallback/enrichment、升级归并 |
| 1B | `apps/api/src/eduagents_api/models.py`、新增 Alembic revision | KC 来源、stable key、superseded 关系 |
| 1B | `apps/api/src/eduagents_api/services.py` | 解析审计；不得静默创建 KC |
| 2 | 新增 `apps/api/src/eduagents_api/practice_recommendation.py` | 单一下一步决策算法 |
| 2 | 新增 `apps/api/src/eduagents_api/routers/practice.py` | recommendation API |
| 2 | `apps/api/src/eduagents_api/routers/quiz.py` | recommended start 与 adjustment |
| 2 | `apps/web/app/quiz/page.tsx`、`apps/web/app/today/*` | 零配置 CTA、理由、范围 chips、事后调整 |
| 2 | `apps/api/src/eduagents_api/routers/teacher.py`、教师页面 | 只做兼容回归，保留确认 |
| 3 | `models.py`、新增 Alembic revision | `field_confidence_json` |
| 3 | `apps/worker/src/eduagents_worker/resources.py` | 逐字段 confidence 生成与清洗 |
| 3 | `routers/resources.py`、`apps/web/lib/types.ts` | API schema 和批量核验 |
| 3 | `apps/web/components/resources/resource-review.tsx` | 只读、高亮、按需编辑、一键确认 |
| 4 | `apps/web/app/tutor/*`、Tutor API/组件 | 三入口和安全上下文传递 |
| 4 | `apps/web/app/plans/page.tsx`、`routers/plans.py` | 一句话目标、按需追问 |
| 5 | `apps/web/app/globals.css`、Web 组件 | 字号 token 和两轮迁移 |

实现时以 `rg` 重新确认实际路径和调用方；表中路径是责任边界，不是允许忽略其他受影响调用点。

## 14. 测试与验收矩阵

### 14.1 单元测试

- decision priority 的每个分支及并列条件；
- `adaptive_difficulty_mix` 保持现有行为；
- Quiz 四种 outcome；
- 0 题三轮为 `generation_failed`，非空非法题为 `validation_failed`；
- chapter stable key/UUID5 幂等、空 heading、同名路径、数量上限；
- fallback → enrichment ID 保留与 supersede 规则；
- 置信度 JSON 的非法值、unknown、阈值边界；
- 计划自然语言已含/未含日期和每日时长。

### 14.2 集成测试

- 非 stub：ready → enriching → enriched → practice ready；
- stub：ready → 章节 KC → 冷启动 3 题 → submit → MasteryRecord/Log/learning event；
- 三题每题 `kc_ids` 非空且属于选中 KB；
- 重复摄取/bootstrap 不产生重复 KC；
- 多租户/机构 KB 不越权；
- Quiz auto 模式无 interrupt，required 模式仍 interrupt/resume；
- SSE `done` 不再出现无 outcome 的空 Quiz result；
- 高置信批量核验只核验服务端判定合格的题；
- pending/rejected 题不产生任何学习证据；
- migration upgrade/downgrade、RLS、导出、注销路径通过。

### 14.3 Web 测试

- 无 weak points 时主 CTA 仍有有效动作；
- recommendation loading/preparing/needs_scope/needs_upload 状态；
- generation/validation/cancel 三种文案不串；
- 事后调整保留 scope/plan 归属；
- 20 题仅 1 个低置信字段时，只展开该题该字段；
- 键盘操作、focus、aria-label 和移动端范围 chips；
- 教师蓝图确认仍可用。

### 14.4 生产浏览器验收

使用真实部署、桌面 `1440 × 1024` 和移动 `390 × 844`，至少完成：

1. 新账号上传一份含三个章节的资料；
2. 分别验证当前部署模式对应的 enrichment 或章节 KC；
3. 零配置生成 3 题；
4. 作答并确认画像出现至少一个 KC；
5. 再次练习显示基于真实记录的理由和自适应难度；
6. 模拟生成空响应和 schema 非法响应，核对两种错误；
7. OCR 高/低/unknown 置信题校对；
8. 辅导三入口、计划按需追问；
9. 全流程桌面/移动截图。

新证据写入 `docs/evidence/ai-first-acceptance-YYYY-MM-DD/`，不得覆盖 `docs/evidence/production-2026-08-09/`。截图不得包含密码、token、邮箱等个人信息。

## 15. Feature flag 与回滚

建议增加：

~~~text
AI_FIRST_PRACTICE_ENABLED
CHAPTER_KC_BOOTSTRAP_ENABLED
RESOURCE_FIELD_CONFIDENCE_ENABLED
~~

- 先在测试/演示租户开启，再逐步放量。
- 原 `/api/quiz` 和原 ResourceQuestion 字段至少保留一个发布周期。
- migration 只做 additive；已产生证据的 KC 不允许回滚删除。
- 关闭 AI-first flag 时可回到旧 UI，但 Quiz outcome v2 和真实错误分类不应回滚。
- 关闭 field confidence UI 时仍保留新 JSON，不做破坏性降级。
- 章节 KC flag 关闭前先确认没有正在依赖它的 stub 冷启动任务；关闭不等于删除已建 KC。

## 16. Definition of Done

### Wave 1 完成条件

- 生产等价配置下正常出题连续 10 次无空成功；
- 所有失败都有正确 outcome/error code；
- stub 和真实模式都能保证题目 KC 非空；
- 新用户完成 3 题后画像出现有效 KC；
- 教师 required confirmation 无回归。

### 全规格完成条件

- 学生常见练习路径前置主动输入为 0；多 KB 低置信范围最多问 1 次；
- 每个推荐都有基于实际证据的理由；
- 校对只要求处理低置信关键字段，高置信题可一键确认；
- 计划保留目标期限和可投入时间的用户决定权；
- 9/10/11px DOM 类清零，12px 经过语义审查而非机械清零；
- 单元、集成、Web、migration、RLS 和生产浏览器验收全部通过；
- `STATUS.md`、Active API/data/development 文档只在代码真实落地后同步更新。

## 17. Luna 执行规则

1. 开始前读取根文档、目标目录 `AGENTS.md`、当前 git diff；不得覆盖用户已有修改。
2. 每次只实现一个 Wave；Wave 1A/1B 可分支开发，但必须联合验收。
3. migration 使用当前 head 的下一个空闲 revision；不得修改已提交 migration。
4. 先写失败测试，再实现；不得删除测试来获得绿灯。
5. 不用 stub 或硬编码题目制造“成功生成”的假象。
6. 不让 `kc_ids=[]` 的作答通过画像冷启动验收。
7. 不把计划文档描述成已实现；代码落地后再更新 Active 文档。
8. 每个 Wave 交付：变更摘要、文件清单、测试命令/结果、残余风险、桌面/移动截图。
9. 遇到本规格与代码事实冲突，先给出代码证据并暂停该冲突项，不能自行扩大范围。

可直接交给 Luna 的启动提示词：

~~~text
请在 /home/shunw/eduagents 按
docs/AI_FIRST_LEARNING_UX_IMPLEMENTATION_SPEC_2026-08-09.md 实施。

先只做 Wave 1A（Quiz release blocker），不要顺带改 UI 字号、OCR 或计划页。
开始前读取 git status、docs/README.md、apps/web/AGENTS.md 和所有目标文件；保留现有用户修改。
先用测试复现“蓝图已确认、generate 连续返回 0 题、最终空 done 被误报取消”，再实现：
1) 有界生成诊断；
2) Quiz done outcome v2；
3) generation_failed / validation_failed / cancelled_by_user 的前端正确映射；
4) 正常题目校验并落库后才允许 succeeded。

完成后运行定向单测、Quiz 集成测试、Web 单测/类型检查，并报告实际结果与残余风险。
不要声称生产已通过；生产浏览器验收要单独执行并写入新的 dated evidence 目录。
~~~

Wave 1A 通过后，再把启动提示词中的 Wave 替换为 1B，并要求严格执行第 7 节的模式分支和 KC 不变式。

## 18. 交付检查命令

按影响范围运行，最终合并前至少执行：

~~~bash
make test-unit
make test-integration
make test-web
make lint
make docs-check
~~~

生产浏览器脚本只能从环境变量读取账户信息，不得把凭据写入脚本、JSON、截图或本文。
