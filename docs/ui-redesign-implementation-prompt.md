# 苏问 AI 课堂 · UI 重构实施提示词（供实现模型一次性执行全部阶段）

> 本文档是**定稿方案的实施指令**。IA 设计已经过两轮评审定稿，**不要重新设计、不要提出替代方案**，按本文档执行。
> 背景现状文档见 `docs/ui-redesign-prompt.md` 第一至六节（页面详单、URL 参数表、localStorage 键表）。**不要通读整个代码库**——按每阶段开头的"先读文件"清单读，需要更多再按需查。

---

## 0. 执行方式（一次性跑完 6 个阶段）

本次执行**连续完成阶段 1→6**，中途无人工验收。为此：

1. **每阶段仍独立提交**：一个阶段一个或多个 commit，commit message 以 `[phase-N]` 开头，**禁止跨阶段混改**。
2. **阶段间自检**：每完成一个阶段，跑一遍第 10 节检查清单（构建 + 单测 + 关键回归），通过才进入下一阶段；不通过就先修复本阶段。
3. **实施报告**：在 `docs/ui-redesign-implementation-report.md` 边做边记，每阶段一节：做了什么、改了哪些文件、检查清单结果、**遇到的问题与自行裁决**。这是次日人工审查的依据。
4. **合法的求助出口**：遇到本文档没裁决、且两种做法差异重大的情况，在报告中记录"待裁决"并选择**改动更小、更可逆**的一种继续；遇到确实实现不了的条目（如第 8 节阶段 6 的停止条件），**跳过该条目并在报告标注 SKIPPED + 原因**，绝不允许用 mock、硬编码、注释测试来"完成"它。

## 0.1 全局约束（每一步都必须遵守）

1. **后端零改动**。所有 API 契约（next-actions、SSE 事件、URL 归因参数 `?plan_id=&task_id=`、`?kc=&n=`、`?weak=1` 等）保持不变，只改前端的入口与布局。
2. **旧路由不许断**。被合并/迁移的路由必须保留重定向并转发原 query。**例外**：需要查数据才能确定目标的重定向（如 /plans?id=X）不做 middleware/服务端重定向，改为页内解析后 client-side `router.replace`。全站 24 个跨页跳转在任何阶段完成时刻都不能 404。
3. **禁止虚假实现**：不许留 TODO 占位、不许 mock 数据糊界面、不许注释掉或跳过失败测试。做不了走 0.4 的报告通道。
4. **设计系统不变**：沿用现有色彩/字号/圆角/动效 tokens（现状文档第三节），中文最低 12px，动效全部保持 `prefers-reduced-motion` 降级。新组件放 `apps/web/components/` 对应子目录，复用 PageHeader/Card/EmptyState/ConfirmDialog 等公共组件。
5. **单测同步**：`tests/unit/test_refactor_invariants.py` 对侧栏结构有字符串级断言（第 40 行附近解析 `PRIMARY_NAV_ITEMS` 与 `TODAY_TOOL_PATHS`）。改侧栏必须同步更新该测试为锁定**新**结构，不许删除断言。
6. **localStorage 键**：新增键统一 `suwen:` 前缀；被迁移功能的旧键（现状文档 5.3）保留读取兼容。
7. **安全**：任何 `?from=` 返回参数只接受站内路径——以 `/` 开头且不以 `//` 开头，否则回落默认页。写一个公共工具函数统一校验。
8. **性能**：AppShell 层新增的数据拉取（如最近会话）不得给每个页面增加阻塞请求；多 tab 页面非激活 tab 不挂载组件、不预取数据。

---

## 1. 目标信息架构（定稿，不可变更）

学生侧一级导航 5 项 + 会话区：

```
侧栏
├─ 今日      /today     学习调度台（阶段 4 加厚）
├─ 对话      /chat      chat 主路由；辅导(tutor)是其中一种会话类型（阶段 6）
├─ 练习      /practice  quiz + reviews + 错题本合并（阶段 2），tab：练一练/到期复习/错题本
├─ 课程      /courses   知识库=课程，列表→课程工作区（阶段 5），入口下默认展开最近 2-3 个活跃课程
└─ 成长      /growth    保持现状
─────────────
最近会话（3-5 条，对话/辅导混排，辅导带徽章；"查看全部"进 /chat）——阶段 1 实现
─────────────
底部：机构 /orgs（仅当用户有机构成员关系时显示）· 苏问 AI 额度 /billing · 账户菜单
```

三个交互原语（跳转消除的判断标准）：

- **规则 A 同实体页内化**：跳转目标与当前属同一课程/同一题目 → 改抽屉/peek/页内 tab，不换路由。
- **规则 B 动作会话化**：跳去 tutor/quiz/reviews 做题的 → 统一改为"发起专注会话"（FocusSession 全屏壳，阶段 3），URL 归因参数原样传递，结算后返回来源页。
- **规则 C 跨域降文字链**：真正的跨域导航（去成长/去课程）保留，从按钮降为文字链接。
- **保留原则**：诊断→行动链路的入口不删（成长页"针对此点练习"、KC 下钻做 3 题、today 主推卡）。

明确不做的事：不取消今日页；不把 tutor 做成 Composer 模式开关；蓝图编辑/计划草案树编辑**不进**专注模式；settings/orgs/teacher/admin/evals 保持现状收在账户菜单（阶段 5 会往账户菜单**新增**"学习计划"一项，属本文档明确授权的唯一改动）。

---

## 2. 阶段 1：侧栏补全 + 最近会话区

**先读文件**：`apps/web/components/app-shell.tsx`、`tests/unit/test_refactor_invariants.py`、`apps/web/components/chat/` 下会话列表相关组件（找会话列表数据源与 `eduagents:chat-session-status` 的用法）。

1. `PRIMARY_NAV_ITEMS` 改为 6 项（过渡态）：今日 /today、对话 /chat、练习 /quiz（阶段 2 改 /practice）、课程 /materials（阶段 5 改 /courses）、成长 /growth、学习计划 /plans（阶段 5 移除）。
2. **删除 `TODAY_TOOL_PATHS`**（app-shell.tsx:71 与 :104），替代为一张**显式高亮映射表** `NAV_HIGHLIGHT_ALIASES`：

   | 访问路径前缀 | 高亮项 |
   |---|---|
   | /tutor | 对话 |
   | /reviews | 练习（阶段 1 指 /quiz 项） |
   | /practice | 练习（阶段 2 起） |
   | /courses | 课程（阶段 5 起） |

   其余路径按现有前缀匹配。**不许重新发明 TODAY_TOOL_PATHS 式 hack**。
3. 同步更新 `test_refactor_invariants.py`：断言新 6 项导航齐全、`TODAY_TOOL_PATHS` 字符串不存在、`NAV_HIGHLIGHT_ALIASES` 存在。后续阶段导航再变时继续同步该测试。
4. **最近会话区**：一级导航下方分隔线后，展示最近 3-5 条会话（标题 MathText 渲染、running/completed 指示沿用 `eduagents:chat-session-status` 逻辑）+「查看全部」→ /chat。约束：数据仅在侧栏可见且展开时拉取一次（复用 chat 会话列表接口，可缓存）；不得阻塞或拖慢各页面首屏；折叠态（64px）不显示该区；移动端抽屉内显示。点击某条 → /chat 并打开该会话（复用 `eduagents:last-chat-session` 机制或带会话参数）。
5. 侧栏底部：机构项加条件显示（无机构成员关系时隐藏；若 auth 上下文拿不到该信息，本条 SKIP 并记录报告）；苏问 AI 额度保留。
6. TITLE_MAP 本阶段不变。折叠态与移动端抽屉验证全部导航项可点。

**验收**：从侧栏可直达 chat/quiz/plans；访问 /tutor 高亮"对话"、/reviews 高亮"练习"；最近会话可点入；单测通过。

---

## 3. 阶段 2：练习合并（/practice = quiz + reviews + 错题本）

**先读文件**：`apps/web/app/quiz/page.tsx`（1179 行，只读顶层结构：视图状态机、URL 参数解析、草稿恢复，不必逐行）、`apps/web/app/reviews/page.tsx`、`apps/web/components/errors/` 目录清单。组件目录 `components/quiz/`、`components/reviews/` **原样复用不重写**。

1. 新路由 `/practice`，顶部三 tab：**练一练**（原 /quiz 全部内容）/ **到期复习**（原 /reviews 到期队列）/ **错题本**（原 ErrorLibrary）。tab 状态 `?tab=quiz|review|errors`，默认 quiz。**非激活 tab 不挂载**（懒加载），确保 quiz 草稿轮询与 reviews 队列请求互不干扰、首屏只发激活 tab 的请求。
2. 旧路由重定向（保 query）：`/quiz`+8 种参数 → `/practice?tab=quiz&…`；`/reviews` → `/practice?tab=review&…`；`/reviews?view=errors` → `/practice?tab=errors`。
3. quiz 结果页"去复习"改页内切 tab（规则 A）；"查看成长"降为文字链；其余结果页按钮本阶段不动。
4. 错题本新增"未归类"筛选，URL 参数**定名 `?filter=unclassified`**（阶段 5 依赖此名），逻辑沿用 `linked_kb_id === null` 过滤（参考 materials 页伪选项实现；materials 伪选项本阶段不动）。
5. TITLE_MAP 新增 `/practice → 练习`。
6. 草稿键 `suwen:pending-quiz-draft:{userId}` 验证可用；**重点回归**：30 分钟草稿 + interrupt 蓝图确认恢复链路（generating/await_confirm/resuming/failed 四态）在新路由下完整。

**验收**：三 tab 原有全部功能逐项可用（四种题型、信心滑条、explain-back、变式重练、申诉、Celebrate、退出确认、错题 CRUD、统计卡筛选、蓝图可编辑确认）；旧 URL 重定向等价；深链 `?kc=&n=`、`?weak=1`、`?plan_id=&task_id=` 正常。

---

## 4. 阶段 3：专注会话壳（FocusSession）

**先读文件**：`apps/web/app/onboarding/page.tsx` 与认证守卫中 BARE 路由的判定位置（onboarding 是唯一脱离 AppShell 的先例）、`apps/web/app/reviews/page.tsx` 的进度行/退出确认/完成卡部分（阶段 2 后已在 /practice 内）。

1. **壳**：新建 `apps/web/components/focus/focus-shell.tsx` + 路由 `/practice/session`（BARE 判定改为可配置列表并加入该路由；BARE 无顶栏故不需要 TITLE_MAP 项）。结构：顶部 48-56px 极简栏 = 进度条 +「第 N 题 / 共 M」+ 退出按钮（触发现有退出确认模态）；主体居中 max-w ~800px。
2. **进入协议**：`/practice/session?mode=review|quiz&from=<站内路径>&…业务参数`（topic/kb/count/difficulty/plan_id/task_id/kc/n/weak 原样透传）。`from` 必须过 0.1-7 的站内路径校验，非法则回落 `/practice`。
3. **退出协议**：完成 → 结算卡（复用现有完成卡/结果汇总卡 + Celebrate）→「返回」回 from；中途退出经确认模态后回 from。
4. **接入顺序**：先接到期复习队列（一次一卡+进度行+退出确认均已存在，只换壳），自测通过再接 quiz 作答与结果（作答+结果两互斥视图整体进壳；**发起页与蓝图确认留在 /practice 普通页面**——编辑任务不进专注模式）。
5. 入口改写（规则 B）：today 主推卡 review/weak_practice 类（前端做 next-actions href → 专注路由的映射表，**后端 href 不改**）、成长页"针对此点练习"与 KC 下钻 `?kc=&n=`、quiz 结果"重练本次错题"、**chat「考考我」chip**（→ /practice/session?mode=quiz&from=/chat&topic=&kb=，本阶段一并完成，不留到以后）。
6. 中途刷新可恢复（沿用现有草稿/队列恢复机制）。

**验收**：复习/练习全流程在全屏壳内完成并正确返回来源页；from 注入 `https://evil.com` 或 `//evil.com` 时回落 /practice；移动端与 reduced-motion 无异常。

---

## 5. 阶段 4：今日页加厚

**先读文件**：`apps/web/app/today/page.tsx`、`apps/web/app/plans/page.tsx` 中"今日任务"卡的组件与数据源、reviews 队列接口的计数字段。

保持 next-actions 为主推荐源，页面改为四块：

1. **主推卡**：现状保留。
2. **今日计划任务卡**：复用 plans 日历视图"今日任务"卡（checkbox 完成/跳过/锁徽章/预计分钟全部现有逻辑，若组件未抽离先抽为共享组件再复用，不许复制粘贴两份）；无活跃计划整卡隐藏。任务的练习/辅导按钮遵循规则 B（辅导在阶段 6 前保持跳 /tutor）。
3. **到期复习计数条**：「今日到期 N 题」+「开始复习」→ /practice/session?mode=review&from=/today；N=0 显示"复习清零"。
4. **本周活跃摘要**（一行小字）：复用 /api/reports/learning 已有聚合；拿不到就整行不渲染并记录报告，不许 mock。
5. 次要推荐 `<details>` 与页脚"真实学习证据"说明保留。

**验收**：有/无计划 × 有/无到期四种组合布局成立；空态仍引导建库。

---

## 6. 阶段 5：课程工作区（/courses）

**先读文件**：`apps/web/app/materials/page.tsx`（重点：库切换 select、选中库视图结构、伪选项 :347 附近）、`apps/web/app/plans/page.tsx`（重点：kb_id 关联与 ?id= 解析）、`components/kb/`、`components/plans/` 目录清单。

1. `/courses` = 课程列表（每库一卡：名称/学科/文档数/机构徽章）+「新建课程」；`/courses/[kbId]` = 工作区三 tab（懒加载）：
   - **资料**：原 materials 选中库视图迁入，**但剔除学习指南卡**（上传、文档表、分块预览、检索调试折叠、题目资料库、校对页、作答记录面板）。
   - **指南**：学习指南卡**独占本 tab**（大纲/术语/推荐问题/重新生成/轮询）。推荐问题 chip → /chat?new=1&kb=&q=。
   - **计划**：该 kb_id 关联计划的详情迁入（分段进度条、日历、任务抽屉、KC 依赖图、调整历史）。
   同一内容只渲染一处，指南不得在资料 tab 重复出现。
2. **/plans 保留聚合壳**：全部计划列表（含 kb_id 为空的全局计划）+ 发起表单；有 kb 的计划点击 → 对应课程计划 tab；全局计划仍在 /plans 查看详情。侧栏移除"学习计划"项；**账户菜单新增"学习计划"入口**（0 节已授权）。
3. **消除 materials 三跳转按钮**（对话/辅导/练习组），替换为工作区顶部「问这门课」（→ /chat?new=1&kb=）。
4. **移除"未归类题目资料"伪选项**：入口改为 /practice?tab=errors&filter=unclassified（阶段 2 已定名）+ /courses 列表底部"未归类资料"文字链同向。
5. **重定向**：`/materials` → `/courses`；`/materials?kb=X` → `/courses/X`；`/materials?resource_id=&question_id=` → 对应课程资料 tab 并保留深链行为（resource 所属库需查数据的，走页内 client-side replace）。**`/plans?id=X` 不做服务端重定向**：/plans 页内解析该计划的 kb_id，有 kb 则 `router.replace` 到课程计划 tab，无 kb 留在本页展示。
6. 侧栏"课程"项：navItem 指向 /courses，默认展开最近 2-3 个活跃课程（`suwen:last-knowledge-base:{userId}` + 新键 `suwen:recent-courses:{userId}` 记录最近访问）；折叠态只显图标。同步单测。
7. **TITLE_MAP**：`/courses → 课程`；`/courses/[kbId]` 顶栏显示**课程名**（TITLE_MAP 是静态映射则扩展 AppShell 支持动态标题，取不到时 fallback「课程」）。
8. 任务抽屉五按钮改造：阅读资料=课程内定位（外链保留外链）；开始练习/复习错题=专注会话带 plan_id/task_id（复习错题若涉及 /reviews?plan_id= 旧参数，经重定向等价）；进入辅导=本阶段保持跳 /tutor 带归因（阶段 6 改）；标记完成保留。

**验收**：上传→摄取→指南→提问→计划全链路在工作区闭环；机构库只读表现不变；全部深链等价可达；plans SSE 生成流在迁移后完整（FlowTimeline、草稿 30 分钟、interrupt resume）。

---

## 7. 阶段 6：tutor 会话类型化（最后做，风险最高）

**先读文件**：`apps/web/app/tutor/page.tsx`（663 行：入口三卡、draft 状态机、非流式轮次、thread 恢复）、`apps/web/app/chat/page.tsx` 的会话列表与恢复逻辑（:310-320 附近）、`components/tutor/`、`components/chat/` 目录清单。

**定稿裁决（不可改）**：辅导是对话页里的**独立会话类型（thread type）**，不是 Composer 模式开关。提示阶梯"第 3 层才给答案"是教学协议，做成开关即失效。

1. chat 会话列表混排两类会话：普通 + 辅导（带"辅导"徽章）；置顶/重命名/删除/搜索一致；running/completed 指示复用。
2. 打开辅导会话：中栏渲染 tutor 消息流（HintBadge、卡点徽章、非流式 spinner、Ctrl/⌘+Enter——现有交互不变）；右栏切换为**提示阶梯**（4 级 sticky 步骤条）；会话带 ?kb= 时右栏"阶梯为主、引用折叠在下"。
3. 新建入口：「开启新对话」旁加「辅导解题」（或新对话空态双选一）；进入先呈现入口三卡与题目输入+校对流程，确认后转辅导消息流。
4. 全站"带题去辅导"跳转（错题详情、复习结果、任务抽屉、resources、today 主推卡 tutor 类 href）改为新建带 error_id/plan_id/draft 上下文的辅导会话直达 /chat。
5. 独立协议保持：非流式 POST /api/tutor、thread 轮询恢复、`suwen:active-tutor-session:{userId}` 兼容读取、solved 复盘卡照旧。
6. `/tutor` 旧路由（全参数）重定向为新建对应上下文辅导会话。
7. **停止条件（合法出口）**：若实施中发现辅导与 chat 的会话数据模型差异超出预期（如会话列表接口无法容纳 tutor thread、SSE/轮询契约冲突需要改后端），**停止本阶段，保留 /tutor 独立路由与阶段 5 状态**，在报告中详述冲突点与建议方案。禁止为融合而 mock 数据或改后端。

**验收**：辅导全流程（入口三卡→识别校对→逐层提示→复盘写画像）在对话页内等价；普通对话无回归（SSE token 流、引用面板、附件、四档回答方式、**会话自动恢复与后台 running 轮询**）；两类会话列表中可区分。

---

## 8. 跳转按钮改造总表（对照现状文档 5.2，逐个执行）

| 现有跳转 | 新去向 | 阶段 |
|---|---|---|
| materials 三按钮（对话/辅导/练习） | 删除，换"问这门课"单按钮 | 5 |
| study-guide 推荐问题 chip | 保留 → /chat?new=1&kb=&q= | 5 |
| chat「考考我」chip | → /practice/session?mode=quiz&from=/chat&topic=&kb= | 3 |
| tutor「改为基于资料提问」 | 删除（同页新建普通会话即可） | 6 |
| quiz 结果"去复习" | 页内切 tab | 2 |
| quiz 结果"进入分级辅导" | 新建辅导会话（此前保持跳 /tutor） | 6 |
| quiz 结果"查看成长" | 降文字链 | 2 |
| quiz "前往学习计划" | → 课程计划 tab（有 kb）/ /plans | 5 |
| reviews"返回今日"×2 | 专注壳退出自动回 from | 3 |
| reviews"带题去辅导"×3 | 新建辅导会话（此前保持跳 /tutor?error_id=） | 6 |
| reviews"导入整份题目资料/查看原题" | 课程工作区资料 tab 深链（抽屉内打开） | 5 |
| plans 任务抽屉：阅读资料 | 课程内定位 | 5 |
| plans 任务抽屉：练习/复习 | 专注会话带 plan_id/task_id | 3/5 |
| plans 任务抽屉：辅导 | 辅导会话带归因 | 6 |
| growth"针对此点练习"/KC 下钻 | 专注练习（保留！不删） | 3 |
| growth"摸底/制定计划/返回今日" | 降文字链 | 4 |
| today 各按钮 | 主推卡保留（href 前端映射）；"前往资料"→"创建第一门课程" /courses | 3/4/5 |
| settings/billing/orgs 内部跳转 | 全部保持现状 | — |

---

## 9. 实施报告模板（`docs/ui-redesign-implementation-report.md`）

每阶段一节，格式：

```markdown
## 阶段 N：<名称>
- 状态：完成 / 部分完成（列 SKIPPED 条目）
- 改动文件：<列表>
- 检查清单：<逐项 ✅/❌，❌ 需说明>
- 自行裁决：<本文档未覆盖、我做了选择的地方，及理由>
- 待裁决/SKIPPED：<需要人工决定的问题，含上下文>
```

---

## 10. 每阶段完成后的统一检查清单

- [ ] 构建通过（项目现有构建命令），无 TS 错误
- [ ] `tests/unit/test_refactor_invariants.py` 及相关单测通过（结构变更同步断言而非删除）
- [ ] 现状文档 5.1 URL 参数表逐条验证：每个参数在当前阶段有等价入口
- [ ] 三处 SSE 生成流回归：chat token 流 / quiz 蓝图流（interrupt+草稿恢复）/ plans 生成流
- [ ] chat 会话恢复（`eduagents:last-chat-session` 自动进入上次会话）与后台 running 轮询不回归
- [ ] 移动端（汉堡抽屉）与侧栏折叠态验证
- [ ] `prefers-reduced-motion` 降级不回归
- [ ] 机构库只读、teacher/admin 角色入口不回归
- [ ] 旧路由重定向带参可达，无 404
