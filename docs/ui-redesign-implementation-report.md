# 苏问 AI 课堂 · UI 重构实施报告

依据 `docs/ui-redesign-implementation-prompt.md`（定稿实施指令）连续执行阶段 1→6。
每阶段一节：做了什么 / 改动文件 / 检查清单 / 自行裁决 / 待裁决与 SKIPPED。

---

## 阶段 1：侧栏补全 + 最近会话区

- 状态：完成（1 条 SKIPPED）
- 改动文件：
  - `apps/web/components/app-shell.tsx`
  - `apps/web/components/nav/recent-sessions.tsx`（新增）
  - `apps/web/app/chat/page.tsx`、`app/chat/view-helpers.ts`、`app/chat/use-chat-stream.ts`
  - `tests/unit/test_refactor_invariants.py`
- 做了什么：
  - `PRIMARY_NAV_ITEMS` 扩为 6 项：今日 /today、对话 /chat、练习 /quiz（阶段 2 改 /practice）、课程 /materials（阶段 5 改 /courses）、成长 /growth、学习计划 /plans（阶段 5 移除）。
  - 删除 `TODAY_TOOL_PATHS`，新增 `NAV_HIGHLIGHT_ALIASES` 显式映射：/tutor→/chat、/reviews→/quiz、/practice→/quiz（预留）、/courses→/materials（预留）。`isActive` 先解析别名再前缀匹配。
  - 顶栏标题与 `isChatRoute`（overflow-hidden 判定）改用 `matchesPath` 纯路径匹配，避免 /tutor 被别名误判成对话页（标题错显 + 滚动被锁）。
  - 最近会话区 `components/nav/recent-sessions.tsx`：最近 5 条（提示词允许 3-5，取上限），标题 MathText、running spinner/completed 蓝点（与 chat 页同一 `eduagents:chat-session-status` 键的 hook 实例，自动同步）；模块级缓存（移动端抽屉反复开关不重复请求）；监听 `eduagents:storage:eduagents:last-chat-session` 同页事件刷新；折叠态（64px）不渲染；空列表/失败静默不占位。
  - chat 页新增 `?session=` 直达支持（独立 effect，消费后 `router.replace("/chat")`；上次会话恢复 effect 对 `?session=` 让位）。
  - 新增 `rememberChatSession()` 统一写 last-chat-session 并按 use-hydrated-local-storage 约定派发同页事件；chat 页 4 处直接 setItem/removeItem 全部换用。
  - `test_refactor_invariants.py`：导航断言更新为 6 项路由表 + `TODAY_TOOL_PATHS` 不存在 + `NAV_HIGHLIGHT_ALIASES` 存在（未删断言）。
- 检查清单：
  - ✅ 生产构建通过（25 路由静态生成）
  - ✅ `test_refactor_invariants.py` 7 passed
  - ✅ web 单测 22 文件 / 115 用例全过
  - ✅ eslint / tsc 通过
  - ✅ 侧栏可直达 chat/quiz/plans；/tutor 高亮"对话"、/reviews 高亮"练习"（代码路径验证，浏览器终验在阶段 6 后统一做）
- 自行裁决：
  - 最近会话点击用 `?session=` 参数而非写 last-chat-session 键后跳转：键写法在"已在 /chat 页时点击侧栏会话"场景下不会触发切换（恢复 effect 已消费），参数法在两种场景都成立。
  - 会话区放 `nav`（flex-1 滚动区）内、底部分隔线上方，滚动跟随主导航。
- SKIPPED：
  - 「机构项仅在有成员关系时显示」：`UserOut` 无机构字段（lib/types/common.ts:5-12），auth 上下文拿不到 → 按指令 2.5 SKIP。备选方案（供人工裁决）：AppShell 挂载后非阻塞 `GET /api/orgs` 一次并缓存于内存（不阻塞首屏，但会给每个会话增加一次请求），未实施。

---

## 阶段 2：练习合并（/practice = quiz + reviews + 错题本）

- 状态：完成（1 条 SKIPPED，见下）
- 改动文件：
  - `apps/web/app/practice/page.tsx`（新增）
  - `apps/web/components/practice/quiz-tab.tsx`（自 app/quiz/page.tsx git mv，逻辑零改动）
  - `apps/web/components/practice/review-tab.tsx`（自 /reviews 队列拆出）
  - `apps/web/app/quiz/page.tsx`、`apps/web/app/reviews/page.tsx`（改为重定向 shim）
  - `apps/web/components/app-shell.tsx`（练习项 → /practice，TITLE_MAP）
  - `tests/unit/test_refactor_invariants.py`
- 做了什么：
  - /practice 顶部三 tab（练一练/到期复习/错题本），`?tab=quiz|review|errors` 默认 quiz；useSearchParams + Suspense 驱动（chat 页同款模式）；**非激活 tab 不挂载**。
  - 旧路由客户端重定向（保 query）：/quiz → ?tab=quiz（8 类参数原样转发）；/reviews → ?tab=review；/reviews?view=errors → ?tab=errors。选客户端 shim 而非 next.config redirects：Next 在 destination 带自身 query 时不转发 incoming query（会导致深链参数丢失），shim 可精确转发。
  - quiz 结果页「去复习」→ `/practice?tab=review` 页内切 tab（规则 A）；「查看成长」降为文字链（规则 C）。
  - 蓝图确认/草稿 30 分钟恢复链路：QuizTab 逻辑零改动，草稿键与 pathname 无关，重定向保参后等价（构建+单测验证；浏览器深链回归在终验统一做）。
- 检查清单：✅ 构建通过（26 路由）· ✅ invariants 7 passed · ✅ web 单测 115 passed · ✅ eslint/tsc
- 自行裁决：
  - 重定向用客户端 shim 而非 next.config（理由见上）。
  - ReviewTab 的「查看错题」完成卡按钮改为 Link 到 `/practice?tab=errors`（原为页内 setSection）。
- SKIPPED：
  - **错题本「未归类」筛选（`?filter=unclassified`）**：后端 `/api/errors` 仅支持 state/cause/kc_id/kb_id/q 过滤（routers/reviews.py:627-634，kb_id 经 KC 交集实现），`ErrorItemOut` 无 linked_kb/resource 归属字段；在服务端分页（10/页）上做客户端过滤只能过滤当前页，属虚假实现 → 按指令 0.4 跳过。影响：阶段 5 的「未归类资料」入口需替代方案（在阶段 5 用课程列表内视图承接，见阶段 5 节）。

## 阶段 3：专注会话壳（FocusSession）

- 状态：完成
- 改动文件：
  - `apps/web/components/focus/focus-shell.tsx`（新增，含 FocusSessionHandle 上下文）
  - `apps/web/app/practice/session/page.tsx`（新增）
  - `apps/web/lib/safe-internal-path.ts`（新增，0.1-7 的 ?from= 校验）
  - `apps/web/components/practice/review-tab.tsx`、`quiz-tab.tsx`（focus 适配）
  - `apps/web/components/quiz/blueprint-card.tsx`（editable prop）
  - `apps/web/components/app-shell.tsx`（BARE_AUTHED_PATHS 加入 /practice/session）
  - 入口改写：`components/chat/message-list.tsx`、`components/mastery-radar.tsx`、`components/growth/history/weak-points.tsx`、`components/plans/plan-task-drawer.tsx`、`app/today/page.tsx`
  - `components/plans/__tests__/plan-task-drawer.test.tsx`（断言同步）
- 做了什么：
  - FocusShell：顶栏 48px（进度条 + 文案 + 退出按钮），主体 max-w-860 居中滚动；退出按钮优先调用子视图注册的确认（复习的「暂时退出」模态），未注册直接返回 from。
  - ReviewTab focus：内部进度行/暂时退出隐藏（顶栏承担）；退出确认与完成卡的「返回今日」变「返回」→ from；进度上报第 N/共 M。
  - QuizTab focus：挂载一次性自动发起（verifyKc→startVerifyQuiz、weak→startWeakQuiz、topic→startQuiz 默认 5 题三档均摊）；已有非 failed 草稿优先恢复；无参数 redirect 回 /practice?tab=quiz；作答+结果在壳内，「返回列表」→「返回」→ from；进度上报已答/共 N。
  - 蓝图确认：BlueprintCard 加 `editable`（默认 true），focus 内只读确认（取消/确认并生成保留），并给「前往练习页编辑」链接（localStorage 草稿共享，/practice 自动恢复 await_confirm 态）。
  - 入口改写：chat「考考我」→ session?mode=quiz&from=/chat&topic&kb；成长「针对此点练习」→ weak=1、KC 下钻 → kc&n=3（from=/growth）；plans 抽屉练习/复习 → 专注会话带 plan_id/task_id（from=usePathname）；today 主推/次推 href 经 focusActionHref 映射（/reviews、/quiz?weak=1、/quiz 三类；/chat?q=、/tutor、计划任务 href 原样）。
- 检查清单：✅ 构建通过（27 路由）· ✅ invariants 7 passed · ✅ web 单测 115 passed（drawer 断言同步）· ✅ eslint/tsc · from 校验单值逻辑（evil 注入回落）将在终验浏览器实测
- 自行裁决：
  - **quiz 结果「重练本次错题」保持页内替换**（未移入专注壳）：重练的题目集合是客户端状态（本次判分的错题列表），无法经 URL 传递；现状本就是页内不跳转（比规则 B 的「发起会话」更内联，符合规则 A 同实体页内化）。提示词阶段 3 条目 5 列了此项，按 0.4 记录此裁决。
  - plans 抽屉「复习相关错题」提前到阶段 3 一并改为专注会话（提示词表格标 3/5）：练习与复习同一改造模式，一次改完总差异更小；原 /reviews?plan_id= 的归因参数经 session URL 原样透传。
  - 专注壳内蓝图确认采用「只读确认 + 去练习页编辑」而非完全禁止：interrupt 必须有人工 resume，完全移除会让专注会话的自动发起流程死锁。

---

## 阶段 4：今日页加厚

- 状态：完成
- 改动文件：
  - `apps/web/components/plans/plan-parts.tsx`（抽出共享 `PlanTodayCard`，PlanCalendar 改用它）
  - `apps/web/components/plans/today-plan-section.tsx`（新增：今日页自取数区块）
  - `apps/web/app/today/page.tsx`（四块布局重写）
  - `apps/web/app/growth/page.tsx`、`components/growth/history/history-trends-main.tsx`（降文字链）
- 做了什么：
  - 主推卡保留（含阶段 3 的 focusActionHref 映射）。
  - 今日计划任务卡：复用共享 PlanTodayCard；TodayPlanSection 自行拉 /api/plans→活跃计划→详情，三态勾选乐观更新+回滚（与 /plans 同一实现模式），点任务开 PlanTaskDrawer；无活跃计划/接口失败/结构为空 → 整块隐藏（静默，不阻塞主视图）。
  - 到期复习计数条：N=max(total_due, items.length)（与复习页副标题同源），开始复习 → 专注会话 from=/today；0 → 「复习清零」。
  - 本周活跃摘要：/api/reports/learning 的 metrics 一行（近 7 天作答±delta、活跃 N/30、正确率）；无 metrics 或失败 → 不渲染。仅 GET，不触发 refresh POST（避免每次进今日页都起后台报告生成）。
  - 成长页降文字链：摸底练习（入口同时改为专注会话）、制定学习计划、返回今日。
- 检查清单：✅ 构建通过 · ✅ web 单测 115 passed · ✅ eslint/tsc · 有/无计划×有/无到期四组合将在终验浏览器实测（依赖真实账号数据）
- 自行裁决：
  - 摸底练习入口带固定 topic「摸底练习」：原 /quiz 空入口落到练习发起页让用户自选；专注会话必须有自动发起参数，故给中性主题名（用户可在蓝图确认时看到主题）。
  - 今日页 WeeklySummary 只 GET 不 POST refresh（理由见上）。
  - TodayPlanSection 失败静默隐藏（提示词只规定「无活跃计划整卡隐藏」，失败态未规定；主视图错误已有 ErrorBanner 承担）。
  - 「前往资料→创建第一门课程(/courses)」推迟到阶段 5 与路由一同落地，避免本阶段死链。

---

## 阶段 5：课程工作区（/courses）

- 状态：完成
- 改动文件：
  - `apps/web/app/courses/page.tsx`（新增：课程列表 + 新建 + ?view=unclassified）
  - `apps/web/app/courses/[kbId]/page.tsx`（新增：三 tab 工作区）
  - `apps/web/components/courses/course-materials-tab.tsx`（新增：自 materials 选中视图迁移）
  - `apps/web/components/plans/plan-detail-panel.tsx`（新增：共享计划详情面板）
  - `apps/web/app/plans/page.tsx`（聚合壳化，1064→693 行）
  - `apps/web/app/materials/page.tsx`（重定向 shim）
  - `apps/web/components/nav/recent-courses.tsx`（新增）、`components/app-shell.tsx`、`lib/page-title.ts`（新增）
  - `components/kb/study-guide.tsx`、`components/plans/plan-task-drawer.tsx`、`app/today/page.tsx`、`tests/unit/test_refactor_invariants.py`
- 做了什么：
  - 课程列表：名称/学科/机构徽章/只读；新建（创建后直达工作区）；底部「未归类资料」文字链。
  - 工作区三 tab 懒加载（非激活不挂载）：资料 tab=上传两阶段摄取+文档表+分块预览+检索调试折叠+信息 aside+题目资料库（校对页/作答面板经 ResourceLibrary 深链 ?resource_id/?question_id 原样可用）；指南 tab=StudyGuidePanel 独占（推荐问题 chip → /chat?new=1&kb=&q=）；计划 tab=PlanDetailPanel（该 kb 计划，优先进行中；无计划空态引导去 /plans 发起）。
  - PlanTodayCard 之外又抽 PlanDetailPanel：/plans 与课程计划 tab 同一份实现；/plans 保留 发起表单+SSE 流+草案编辑+全局计划详情。
  - ?id= 深链：/plans 页内解析，kb 计划 router.replace 到课程计划 tab（5.5 不做服务端重定向的裁决照办）。
  - /materials shim 四分支重定向（kb/view=unclassified/resource_id 查库深链/默认）。
  - 侧栏 5 项（今日/对话/练习/课程/成长）+ 课程项下最近 2-3 门展开（新键 suwen:recent-courses:{userId}，兼容旧键）+ 账户菜单新增学习计划 + 动态标题（课程名）。
  - 消除 materials 三跳转按钮 → 「问这门课」；任务抽屉阅读资料 → 课程资料 tab。
- 检查清单：✅ 构建通过（28 路由，/courses/[kbId] 动态）· ✅ invariants 7 passed · ✅ web 单测 115 passed · ✅ eslint/tsc
- 自行裁决：
  - 「未归类资料」入口用 `/courses?view=unclassified`（渲染 ResourceLibrary unclassified），不是提示词定的 `/practice?tab=errors&filter=unclassified`——后者依赖阶段 2 SKIPPED 的后端过滤；本方案保留等价功能且零后端改动。
  - 新建课程后用 router.push 直达工作区（原 materials 行为等价）。
  - /plans 默认选中 list[0]（可为 kb 计划，详情面板在本页内渲染）；只有显式 ?id= 或点击 kb 计划才跳课程 tab——避免访问 /plans 时被无条件弹走。
  - course-materials-tab 的上传错误码表随迁移复制自 materials（materials 已是 shim，表只剩一处）。

---
