# STATUS — 验收条款真值表（唯一事实源）

> 状态: ⬜未开始 / 🟡进行中 / ✅通过 / ⛔BLOCKED。每条附验证命令。
> 更新纪律: 完成一条勾一条并记录时间; 禁止无验证勾选。
> 2026-07-27 对照代码/git/docs/evidence 全表逐项回填 (HEAD=fddc81d)。本轮只回填状态与证据指针, 未改任何实现; [HUMAN]/[LIVE] 无记录的项一律不虚标。

## 里程碑 DoD

| 条款 | 内容 | 状态 | 验证命令 | 最近通过 |
|---|---|---|---|---|
| M0-D1 [CI] | 迁移从零建库成功 (21 表 + RLS + edu_app 角色) | ✅ | `make demo-m0` | 2026-07-26 |
| M0-D2 [HUMAN] | 契约冻结 (data-model.md / api-contract.md 经用户确认) | 🟡 | 用户 review | — |
| M1-D1 [LIVE] | docker-compose up 全绿 | ✅ | 7/7 容器 healthy + 经 Caddy 注册/登录/SSE 冒烟; 顺手修镜像缺 langgraph(改 lock 安装); 证据 docs/evidence/m1/compose_validation.md | 2026-07-27 |
| M1-D2 [LIVE] | SSE 逐 token 输出 | ✅ | `make test-integration` (test_tenant_isolation SSE 段) | 2026-07-26 |
| M1-D3 [LIVE] | 拔主模型 key 走降级链, 日志可见熔断迁移 | 🟡 | 熔断状态机 4 单测 (test_circuit) + 故障注入 8 单测 (test_faults) + admin 故障注入 API/降级链集成 (test_evals_teacher.py::test_fault_injection_admin_api_and_degradation); live 档"拔真 key→日志可见熔断迁移"演示仍未录制 | — |
| M1-D4 [CI] | usage_log 三维归因 (user/feature/model) | ✅ | `make test-integration` | 2026-07-26 |
| M1-D5 [CI] | 越权测试 (含 RLS 连接池复用路径) + auth 限流用例 | ✅ | 越权 ✅ (test_tenant_isolation.py, 含 asyncpg 池复用交替切换与无 GUC 默认拒绝); auth 限流专项 ✅ (test_stream_auth_fix.py::test_login_account_bucket_blocks_same_email_across_ips: 同邮箱第 6 次 429 且不牵连他人); 残留缺口如实记: Retry-After 头 / register/refresh 桶无专项断言 | 2026-07-27 |
| M2-D1 [LIVE] | 20页 PDF 60秒内 ready(basic) | ✅ | 21页 PDF: stub 口径每次测试覆写 (最新 2.24s, docs/evidence/m2/pdf_ingest_timing.txt 2026-07-27) / **live 口径 5.46s** (pdf_ingest_timing_live.md, 独立文件防覆写, 唯一可稳定引用的数字) | 2026-07-27 |
| M2-D2 [LIVE] | 回答带引用定位原文块+公式渲染 | ✅ | API 层 citation 事件 e2e ✅ (test_rag_e2e); 前端已上线: 引用卡片与正文 [N] 双向联动高亮 (apps/web/components/chat/citations-panel.tsx) + 引用回原文抽屉 (context-drawer.tsx, 配 test_chunk_context_endpoint) + KaTeX 全链路 (apps/web/lib/katex.tsx, 10 处复用); UI 实拍 docs/evidence/m4/03-chat-answer.png | 2026-07-27 |
| M2-D3 [LIVE] | 黄金集 recall@5 ≥ 0.8 | ✅ | v1 live: hit_rate@5=1.0 / MRR 0.92 (docs/evidence/m2/retrieval_eval_live_full.md; 饱和解读 ablation_summary.md); v2 黄金集 60 问 (54 可答+6 拒答, 3 语料跨文档干扰): hit@5=1.0 / MRR 0.946 / 拒答代理 5/6 — q59 reranker 0.77 误报已如实记录 (docs/evidence/m5/retrieval_eval_v2.md) | 2026-07-26 |
| M2-D4 [CI] | 损坏/恶意文件正确处理 | ✅ | `make test-integration` (test_upload_security_rejections) | 2026-07-26 |
| M2-D5 [LIVE] | 消融开关生效+真实差异报告 | ✅ | 三配置 live 对比 + 如实解读 (rerank 在饱和小集上反降 MRR, 已解释并保留开关): docs/evidence/m2/ablation_summary.md | 2026-07-26 |
| M2-D6 [CI] | KC-A1 归一化测试通过 | ✅ | 单测: 泰勒公式/Taylor公式/泰勒展开 收敛同一 kc_id (test_kc.py); 集成: enrich 后 chunk.kc_ids 100% 外键有效、成环边被拒 (test_enrichment.py) | 2026-07-26 |
| M3-D1 [LIVE] | Langfuse 完整 trace 树 | ✅ | 自托管六容器全绿 + 网关/qa 图 SDK 接线(PII 指纹化) + 真实流量 span 树截图; 证据 docs/evidence/m3/langfuse_tracing.md | 2026-07-27 |
| M3-D2 [LIVE] | interrupt→resume 全流程 + 续传 | ✅ | 集成测试: 出题 interrupt 挂起→跨 HTTP 请求 PG Checkpointer 恢复→生成落库; 越权 resume 404 (`test_quiz_flow.py`) | 2026-07-26 |
| M3-D3 [LIVE] | 出题 100% 过 schema 校验 | ✅ | pydantic 全量校验+坏题带错误清单重试环(≤3轮), 校验不过不落库; 单测13+集成1 | 2026-07-26 |
| M3-D4 [LIVE] | 辅导前3轮不给答案 | 🟡 | 结构性防线 CI 全绿: hint_level 状态机/每轮 prompt 禁答令压轴/索答不跳级 6 单测 (test_tutor_graph.py) + 画像注入不破防线 (test_plan_personalization.py::test_tutor_hint0_to_3_receive_profile_and_ban_unaffected); live 效果断言脚本(真模型回复不含最终答案)仍缺 | — |
| M3-D5 [LIVE] | 拒答用例正确拒答 | ✅ | qa 图: 检索空+web 空→refused (单测); API 层无依据拒答 (rag e2e) | 2026-07-26 |
| M3-D6 [LIVE] | 延迟三档口径达标 | 🟡 | stub 档 30 并发初测: TTFE p95=87ms / TTFT p95=160ms / 0 错误 (docs/evidence/m5/loadtest_stub_initial.md, 首段如实声明缩水规模); live 档 (首答案 token P95<6s, 真模型口径) 未测 | — |
| M4-D1 [LIVE] | 黄金路径 6.5 分钟 GUI 一镜到底 | 🟡 | HTTP 层黄金路径 record 口径 8 PASS (scripts/smoke_golden_path.py, 真实模型+三进程栈); Playwright UI 巡检 17 屏 (apps/web/ui_review3.mjs, 截图+console 收集型); GUI 一镜到底断言式串场脚本仍缺 | 2026-07-26 |
| M4-D2 [LIVE] | 答错→time-travel→针对性出题 e2e | ✅ | 答错→错题入队→time-travel+3天→到期重现 (test_reviews_flow.py::test_reviews_fsrs_time_travel_flow); 薄弱点自动选 KC 出题已接 (routers/quiz.py; test_vlse_loop.py::test_quiz_from_weak_points + 无数据拒绝用例; commit 5d5947d) | 2026-07-27 |
| M4-D3 [LIVE] | 重启后状态完整+新会话引用历史 | ✅ | 重启状态完整 = U-A2 (compose restart 后命名卷数据存活); 新会话引用历史 = F8-A2 开场卡+可解释推荐 (components/chat/opening-card.tsx + /api/next-actions; test_next_actions_flow.py::test_next_actions_reference_history_stuck_point 断言点名 KC+学生原话; commit 7926f27) | 2026-07-27 |
| M4-D4 [CI] | 画像更新日志与作答记录核对一致 | ✅ | mastery_log 逐条 evidence_id 关联 attempt (test_reviews_flow/test_profile_reports) | 2026-07-26 |
| M5-D1 [LIVE] | 压测两档口径 (stub 100 并发零 5xx + 真实 API 10-20 并发) | 🟡 | stub 档 30 并发/90s 缩水初测: 零 5xx、TTFE p95 87ms、TTFT p95 160ms (docs/evidence/m5/loadtest_stub_initial.md + 原始 CSV/HTML, 首段如实声明非 100 并发正式规模, 缩水原因 7.6G 内存 OOM 风险); 缺: 100 并发阶梯正式档、真实 API 10-20 并发端到端档 (loadtest/accounts_live.json 未生成) | — |
| M5-D2 [LIVE] | 现场关模型/关 Qdrant 按预设链路降级 | 🟡 | 故障注入开关+admin API+前端操作台+降级链测试 ✅ (gateway/faults.py 8 单测; test_evals_teacher.py::test_fault_injection_admin_api_and_degradation); Qdrant 掉线检测 ✅ (test_ready_returns_503_when_qdrant_unreachable); 缺: Qdrant 侧故障开关、现场"关模型→前端友好提示"演示录制 | — |
| M5-D3 [LIVE] | 闭环实验自适应 vs 随机曲线图 | ✅ | 三臂×10 seeds 配对: adaptive 26.0 vs ladder 41.7 vs random 80.9 题毕业, Wilcoxon p=0.0020 (n=10 精确下界, 报告自注), 敏感性 10 变体方向 10/10 不变; 实验 A 如实报告 BKT misspecification (校准 R² 为负); docs/evidence/m5/report.md + fig1-4 + results.json; 复现: eval/simulation/ + `make demo-m5` | 2026-07-26 |
| M5-D4 [HUMAN] | 全新机器按 README 15 分钟复现 | 🟡 | 核对单已写 (deploy/README-deploy.md §7, 六步带分段耗时, make demo-m2..m5 均真实存在); 全新机器实测未做; 注意: 核对单末尾"M1-D1 ⛔"阻塞声明已过期待回填 | — |
| M5-D5 [CI] | STATUS 全部条款非 BLOCKED 项 100% 通过 | ⬜ | 待本表非 BLOCKED 项收敛后统一核验 | — |

## 用户体系 (§3.0)

| 条款 | 内容 | 状态 | 验证 |
|---|---|---|---|
| U-A1 [CI] | 双账号全面越权测试 (API+向量+存储+SSE, 含 asyncpg 池复用) | 🟡 | API/RLS/池复用/SSE ✅ (test_tenant_isolation_via_api_and_rls); 向量层强制 tenant filter ✅ (test_vectorstore.py::test_tenant_isolation_and_delete; 机构库 decoy 不出现断言 test_org_kb.py); 画像/偏好/洞察/答案缓存逐域隔离 ✅ (test_profile_tenant_isolation 等); MinIO 预签名路径在本实现中不存在 (下载经 API 流式转发, routers/account.py)——该子面以"无攻击面"口径说明; 缺: 集成层双账号向量检索越权与下载端点越权专项 |
| U-A2 [LIVE] | 重启容器后数据完整 | ✅ | 本地实例 + 容器版均验证: compose restart postgres/api 后注册用户登录成功(命名卷持久化), 见 docs/evidence/m3/langfuse_tracing.md §4 |
| U-A3 [CI] | auth 独立限流 + refresh 重放拒绝 | ✅ | 重放拒绝+撤销全家 ✅ (test_full_auth_lifecycle); 登录限流专项 ✅ (test_stream_auth_fix.py::test_login_account_bucket_blocks_same_email_across_ips, IP 桶+账号桶并联 429); 残留缺口如实记: Retry-After 头 / register/refresh 桶无专项断言, ratelimit.py 无独立单测 |
| U-A4 [LIVE] | 教师组卷导出 / 学生 403 | ✅ | test_evals_teacher.py::test_teacher_export_json_print_and_student_forbidden (学生 403、导出题面不含答案、他人卷 404、admin 放行); 页面 apps/web/app/teacher/page.tsx; 截图 docs/evidence/features/13-teacher.png |

## 功能验收 (2026-07-27 逐项回填)

| 模块 | 条款 | 状态 |
|---|---|---|
| F1 知识问答 | F1-A1..A6 | A2 ✅ (web 兜底→拒答链 + "以下依据网络搜索结果"来源分区双断言 test_qa_graph.py + rag e2e 库外问题); A6 ✅ (会话增删改查端点全 + 越权 404, test_tenant_isolation; "改"重命名路径缺专项用例); 引用卡片/KaTeX/追问 chips(≤3 去重)/路由理由展示 前端全上线 ✅ (citations-panel.tsx / lib/katex.tsx / chat 页 chips / timeline.tsx); A3 🟡 stub 档已测 (见 M3-D6); A1/A5 [HUMAN] 抽检零记录; A4 实现有 (历史 20 条注入) 但 5 轮追问无任何用例或脚本 |
| F2 知识库 | F2-A1..A6 | A2 ✅; A3 ✅ (test_rag_e2e 删除后 debug/retrieve 为空 + test_doc_delete_bumps_kb_version_and_kills_cache 缓存/向量清理); A6 ✅ (test_chunking: 公式 $ 定界成对存活/表格原子不跨块); A1 🟡 (两阶段状态机+enrich 全通 test_enrichment, 21页 live 5.46s; 但 50页/2min 与 enriched≤10min 计时零证据——仓库最大语料仅 21 页); A4 🟡 (双租户+机构库三库切片 ✅ test_org_kb 含跨租户 decoy 不出现; 同租户 ≥3 库正交检索用例待补); A5 ⬜ (judge 脚本与 20 例人工复核记录均无); 附加已上线: URL 导入+SSRF 四闸 ✅ (test_import_guide), 学习指南 ✅, 分块预览器+检索调试面板前后端 ✅ (chunks-drawer.tsx / retrieve-debug.tsx + test_rag_e2e) |
| F3 规划 | F3-A1..A4 | A2 ✅ ("无任务先于未掌握前置"自动化断言 test_plan_personalization.py::test_no_task_before_unmastered_prerequisite + 集成 test_plan_create_uses_real_mastery_and_dag_order); A3 ✅ (<60% 自动补强+调整理由+审计历史 plan_adjust.py; test_plan_errors.py::test_plan_alive_and_error_book); A1 🟡 (日预算程序性钳制 [0.7,1.15]× 并缩放, plan_graph.py + 单测——下界 -30% 宽于验收 15%, 且无"总时长 vs 总预算"整体断言); A4 🟡 (图内资源采集+越界引用置空 ✅; 但资源未随 finalize 落库、前端无跳转链接); interrupt 确认: 图+单测 ✅ (含用户编辑版覆盖), 计划页前端仅批准/拒绝无编辑入口 (quiz 页有完整编辑确认) |
| F4 苏格拉底辅导 | F4-A1..A3 | A1 🟡 (=M3-D4: 结构性防线 CI 全绿, live 效果断言脚本缺); A2 🟡 (四类卡点分类+guidance 注入实现 tutor_graph.py; 测试仅覆盖 unknown 降级路径, [HUMAN] 10 例抽检无); A3 🟡 (小结写画像 ✅ test_tutor_profile/test_insights_flow + 薄弱点出题 ✅ test_quiz_from_weak_points; 两段未串成单条端到端用例) |
| F5 出题测评 | F5-A1..A3 | A2 ✅ (source_chunk_id 映射+越界置空 test_kb_materials_and_source_hint_mapping; kc_ids 走词表归一禁裸串 services.py::_resolve_kc_ids + 外键断言 test_verify_reassess_flow); A3 ✅ (test_quiz_flow.py::test_quiz_interrupt_resume_grade_bkt: 判分→BKT 直写→错题入本端到端); A1 🟡 (schema 严校验+重试环实现与测试 ✅; "10 题成功率 100%"规模的用例/运行记录缺——现有用例 2-5 题) |
| F6 错题本 | F6-A1..A4 | A1 ✅ (判错即入本 services.py; test_quiz_flow + test_plan_errors 低分卷归集); A4 ✅ (今日队列≤5 + FSRS 到期 + time-travel 重现/间隔拉长 test_reviews_fsrs_time_travel_flow + test_scheduler); A2 🟡 (错因五分类+stub 不冒充+手动修正均有集成断言 test_plan_alive_and_error_book; [HUMAN] 20 例抽检无); A3 🟡 (状态机 连对2次→已攻克/答错清零/变式 variant_of 均有集成断言 test_reviews_fsrs_time_travel_flow; [HUMAN] 变式 10 例抽检无) |
| F7 学情报告 | F7-A1..A3 | A1 ✅ (trend 30 天与作答记录核对 test_profile_reports.py::test_reports_learning); A3 ✅ (报告 ≥2 条带 href 可执行建议断言 + /api/next-actions 14 单测+3 集成); A2 🟡 (三段式『现状/归因/建议』+禁空话实现 reports.py, stub 下如实返 null; live 输出质量零自动化覆盖, [HUMAN] 待) |
| F8 画像 | F8-A1..A3 | A1 ✅ (三类事件各有集成用例: quiz_attempt test_verify_reassess_flow / tutor_summary test_tutor_profile / chat_reflection test_vlse_loop+幂等; 页面可见, UI 实拍 personalization/ui/08-profile.png); A2 ✅ (开场卡 opening-card.tsx + next-actions; test_next_actions_reference_history_stuck_point 断言点名 KC+学生原话); A3 ✅ (mastery_log 逐条 evidence 断言覆盖 quiz_attempt/tutor_summary/chat_reflection/review/user_override/appeal_regrade 六类) |
| F9 评估看板 | F9-A1..A2 | A1 ✅ (POST /api/evals/run→ARQ+job 轮询+页面一键触发, evals.py + evals/page.tsx; test_evals_import_idempotent_list_detail_compare 含触发与学生 403); A2 ✅ (eval_runs 入库+消融标签+/api/evals/compare+页面 A/B 对比; 6 条真实评测入库, delta 断言) |
| F10 运营面板 | F10-A1..A2 | A1 ✅ (usage_log 三维归因); A2 ✅ (80/100/150% 三级: soft→cheap 降档 / hard 429+Retry-After / 全站杀闸; test_budget_guard 6 单测 + test_budget_e2e 5 集成); 页面 ✅ (/admin 四标签: 运营/检索/故障注入/反馈, 三维聚合+P95 表); 如实记: 缓存命中率百分比未上 UI (后端 cache_savings 已算, 前端仅渲染 cached_tokens 大数卡) |
| KC 体系 | KC-A1..A3 | A1 ✅ (归一化三表述收敛+别名追加, test_kc.py 17 单测; 集成 enrich 外键有效/成环拒绝); A3 ✅ (DAG 无环+稳定拓扑序); A2 ⬜ ([HUMAN] 词表 20 例抽检零记录, judge 脚本无) |
| 数学专项 | M-A1..A2 | A1 🟡 (KaTeX 全链路实现 lib/katex.tsx 10 处复用 + 2 张截图定性描述 m4/03,07; [HUMAN] 10 问逐问抽检表无); A2 🟡 (表格原子切块+摘要参检 CI ✅ test_chunking; 端到端"引用定位到该表"证据无; 如实记反向数据: v2 评估"表格数据查询"为最差桶 hit@1=0.75 / hit@5=1.0, dense 对纯数值表格表征弱, 见 retrieval_eval_v2.md 解读) |
| 安全 | S-A1..A3 | A1 ✅ (5类注入载荷红队集成测试+对照小节零误杀, 13条双语过滤模式39单测); A2 ✅ (伪装/超限/空/非utf8 全拒, test_upload_security_rejections); A3 ✅ (输入侧中间件 SSE 首字节前 400 拦截+学术豁免防误杀 test_import_guide.py::test_moderation_middleware_blocks_and_passes; 输出侧流式分句审核+危机热线 12356: test_moderation_stream 5 单测 + test_moderation 15 单测) |
| 演示工程 | D-A1..A3 | A1 ✅ (Clock 单测 + time-travel API routers/admin.py + `make time-travel DAYS=N`; FSRS 到期/BKT 衰减断言 test_reviews_fsrs_time_travel_flow); A2 🟡 (record/replay 机制+磁带日常用于 CI ✅; 黄金路径 replay 全程运行证据无; 且个性化两波改了 qa/quiz/plan/tutor prompt, 旧磁带 key 失配, 正按 scripts/record_demo_tape.py 重录 [2026-07-27 进行中]); A3 🟡 (Playwright UI 巡检 17 屏 ✅ ui_review3.mjs, 截图+console 收集型; 黄金路径断言式冒烟未写, 与 M4-D1 同源) |

## 个性化两波 (tag personalization-p1 / personalization-p2, 2026-07-27; 设计出处 docs/research/brief-5-个性化.md)

| 项 | 状态 | 验证 |
|---|---|---|
| ZPD 难度自适应产品化 (BKT 配比+逐 KC 定性理由+程序性强制) | ✅ | routers/quiz.py (后端自选 KC+自适应难度配比); 单测 tests/unit/test_quiz_personalization.py (难度分档边界+理由定性化) |
| 计划读真实 BKT+先修 DAG 拓扑序 (F3-A2 接线) | ✅ | tests/integration/test_plan_personalization_flow.py::test_plan_create_uses_real_mastery_and_dag_order |
| learner_insights 长期记忆表 (RLS FORCE, 可见可删, PIPL 覆盖) | ✅ | 迁移 alembic/versions/0005_learner_memory.py; test_insights_flow.py 4 用例 (含 test_export_includes_memory_tables); 页面「AI 记住的你」逐条删除 (app/profile/page.tsx) |
| user_preferences 偏好→四链路 prompt 注入 | ✅ | test_preferences_flow.py 5 用例 (进 QA system prompt / 缓存立即失效 / 按用户隔离) + 单测 test_preferences.py |
| onboarding 三步表单 | ✅ | app/onboarding/page.tsx; UI 实拍 docs/evidence/personalization/ui/17-onboarding.png |
| F8-A2 开场卡 + /api/next-actions 可解释推荐 (零 LLM) | ✅ | components/chat/opening-card.tsx; test_next_actions.py 14 单测 + test_next_actions_flow.py 3 集成 (含跨用户不泄漏) |
| 画像可争辩「验证一下」3 题重估 (真实写 BKT) | ✅ | test_verify_reassess_flow.py::test_verify_quiz_without_kb_reassesses_mastery (evidence_type=quiz_attempt; 无 kb 时 kc_ids 空转缺陷已修) |
| FSRS 遗忘快照 (N 天后预计保持率) | ✅ | 单测 tests/unit/test_retention.py (保持率单调衰减/稳定性越高衰减越慢); 页面 profile/reviews 展示 |
| P2-7 脱敏铁律: 掌握度小数不出境第三方 LLM (只给定性档) | ✅ | test_quiz_personalization.py::test_rationale_uses_qualitative_zone_words_without_decimals + test_plan_personalization.py::test_render_diagnosis_qualitative_no_decimals + test_next_actions.py::test_reasons_qualitative_no_mastery_decimals |
| 红线纪律: 画像非空禁答案缓存 (个性化答案不跨用户复用) | ✅ | test_vlse_loop.py::test_answer_cache_disabled_when_profile_present; 其余红线 (掌握度只许 BKT+规则写 / 伪科学学习风格词禁用) 为设计纪律, 见 docs/HANDOFF.md §5 |

## 基线工程质量 (持续; 2026-07-27 核对)

| 项 | 状态 | 验证 |
|---|---|---|
| 单元测试 | ✅ 412 用例 (pytest --co -q 实收 2026-07-27; 最近全绿 412+79 见 commit fddc81d 同日; 数字随开发增长, 以 `make test-unit` 实测为准) | `make test-unit` |
| 集成测试 | ✅ 79 用例 (同上) | `make dev-infra && make test-integration` |
| Lint (ruff) | ✅ 已提交代码全过; 工作区未提交录带脚本 scripts/record_demo_tape.py 现有 3 处可自动修复告警 (2026-07-27 实测) | `make lint` |
| Web 构建 | ✅ 18 个 page.tsx 路由 (2026-07-27 计数, 含 /onboarding /settings); build+eslint 零错零警 (personalization-p2 波实测, commit 60444b1) | `cd apps/web && npm run build` |
| 结构化日志 (request_id 贯穿) | ✅ | 集成测试输出可见 JSON 日志 |
| 统一错误信封 | ✅ | test_auth_flow 断言 code/request_id |
| 网关四模式 (live/record/replay/stub) | ✅ 单测覆盖 | `make test-unit` |
| 熔断器状态机 | ✅ 4 单测 | `make test-unit` |
| requirements.lock | ✅ | `make lock` |
