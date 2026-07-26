# STATUS — 验收条款真值表（唯一事实源）

> 状态: ⬜未开始 / 🟡进行中 / ✅通过 / ⛔BLOCKED。每条附验证命令。
> 更新纪律: 完成一条勾一条并记录时间; 禁止无验证勾选。

## 里程碑 DoD

| 条款 | 内容 | 状态 | 验证命令 | 最近通过 |
|---|---|---|---|---|
| M0-D1 [CI] | 迁移从零建库成功 (21 表 + RLS + edu_app 角色) | ✅ | `make demo-m0` | 2026-07-26 |
| M0-D2 [HUMAN] | 契约冻结 (data-model.md / api-contract.md 经用户确认) | 🟡 | 用户 review | — |
| M1-D1 [LIVE] | docker-compose up 全绿 | ⛔ | `make up` | Docker daemon DNS 污染, 待用户执行 README「Docker 修复」; 本地开发用 `make dev-infra` 替代 |
| M1-D2 [LIVE] | SSE 逐 token 输出 | ✅ | `make test-integration` (test_tenant_isolation SSE 段) | 2026-07-26 |
| M1-D3 [LIVE] | 拔主模型 key 走降级链, 日志可见熔断迁移 | 🟡 | 单测已覆盖降级到 stub (`test_live_without_keys_*`); live 档真实熔断演示待录制 | — |
| M1-D4 [CI] | usage_log 三维归因 (user/feature/model) | ✅ | `make test-integration` | 2026-07-26 |
| M1-D5 [CI] | 越权测试 (含 RLS 连接池复用路径) + auth 限流用例 | 🟡 | 越权 ✅ (test_tenant_isolation); auth 限流实现完成但缺专项用例 | 2026-07-26 |
| M2-D1 [LIVE] | 20页 PDF 60秒内 ready(basic) | ✅ | 21页 PDF: stub口径 2.54s / live BGE-M3 口径 4.63s (docs/evidence/m2/pdf_ingest_timing.txt) | 2026-07-26 |
| M2-D2 [LIVE] | 回答带引用定位原文块+公式渲染 | 🟡 | API 层 citation 事件(文档/heading/页码/高亮) ✅ e2e; 前端渲染在 M4 | 2026-07-26 |
| M2-D3 [LIVE] | 黄金集 recall@5 ≥ 0.8 | ✅ | live 口径 hit_rate@5=1.0 / MRR 0.92 (docs/evidence/m2/retrieval_eval_live_full.md; 小语料近饱和的诚实解读见 ablation_summary.md) | 2026-07-26 |
| M2-D4 [CI] | 损坏/恶意文件正确处理 | ✅ | `make test-integration` (test_upload_security_rejections) | 2026-07-26 |
| M2-D5 [LIVE] | 消融开关生效+真实差异报告 | ✅ | 三配置 live 对比 + 如实解读 (rerank 在饱和小集上反降 MRR, 已解释并保留开关): docs/evidence/m2/ablation_summary.md | 2026-07-26 |
| M2-D6 [CI] | KC-A1 归一化测试通过 | ✅ | 单测: 泰勒公式/Taylor公式/泰勒展开 收敛同一 kc_id (test_kc.py); 集成: enrich 后 chunk.kc_ids 100% 外键有效、成环边被拒 (test_enrichment.py) | 2026-07-26 |
| M3-D1 [LIVE] | Langfuse 完整 trace 树 | ⛔ | Langfuse 自托管容器被 Docker 问题阻塞 (同 M1-D1); 图内 trace 事件流已全量落 state.trace 并经 SSE 透出 | — |
| M3-D2 [LIVE] | interrupt→resume 全流程 + 续传 | ✅ | 集成测试: 出题 interrupt 挂起→跨 HTTP 请求 PG Checkpointer 恢复→生成落库; 越权 resume 404 (`test_quiz_flow.py`) | 2026-07-26 |
| M3-D3 [LIVE] | 出题 100% 过 schema 校验 | ✅ | pydantic 全量校验+坏题带错误清单重试环(≤3轮), 校验不过不落库; 单测13+集成1 | 2026-07-26 |
| M3-D4 [LIVE] | 辅导前3轮不给答案 | 🟡 | 结构性防线 ✅ (hint_level 状态机+代码注入禁答令, 单测逐轮断言实收 prompt); LIVE 效果脚本待录制阶段 | — |
| M3-D5 [LIVE] | 拒答用例正确拒答 | ✅ | qa 图: 检索空+web 空→refused (单测); API 层无依据拒答 (rag e2e) | 2026-07-26 |
| M3-D6 [LIVE] | 延迟三档口径达标 | ⬜ | 测量脚本待 M5 压测阶段 (stub 口径下无意义) | — |
| M4-D1 [LIVE] | 黄金路径 6.5 分钟 GUI 一镜到底 | 🟡 | HTTP 层黄金路径 record 口径 8 PASS (scripts/smoke_golden_path.py, 真实模型+三进程栈); GUI 一镜到底待前端联调+Playwright | 2026-07-26 |
| M4-D2 [LIVE] | 答错→time-travel→针对性出题 e2e | 🟡 | 答错→错题入队→time-travel+3天→到期重现 ✅ (test_reviews_flow); "针对薄弱点出题"的画像联动待接 quiz kc_names 自动填充 | 2026-07-26 |
| M4-D3 [LIVE] | 重启后状态完整+新会话引用历史 | 🟡 | PG 持久化+U-A2 已验; "新会话主动引用历史卡点"待 Reflection/开场注入 (F8-A2) | — |
| M4-D4 [CI] | 画像更新日志与作答记录核对一致 | ✅ | mastery_log 逐条 evidence_id 关联 attempt (test_reviews_flow/test_profile_reports) | 2026-07-26 |
| M5-D1..D5 | 压测/故障注入/闭环实验/15分钟复现 | ⬜ | — | — |

## 用户体系 (§3.0)

| 条款 | 内容 | 状态 | 验证 |
|---|---|---|---|
| U-A1 [CI] | 双账号全面越权测试 (API+向量+存储+SSE, 含 asyncpg 池复用) | 🟡 | API/RLS/池复用 ✅; 向量/MinIO 越权待 M2 | 
| U-A2 [LIVE] | 重启容器后数据完整 | 🟡 | PG 持久化已验证 (本地实例); 容器版待 M1-D1 解锁 |
| U-A3 [CI] | auth 独立限流 + refresh 重放拒绝 | 🟡 | 重放拒绝+撤销全家 ✅; 限流专项用例待补 |
| U-A4 [LIVE] | 教师组卷导出 / 学生 403 | ⬜ | 角色中间件已就位 (require_teacher), 页面待 M4 |

## 功能验收 (待对应里程碑)

| 模块 | 条款 | 状态 |
|---|---|---|
| F1 知识问答 | F1-A1..A6 | F1-A6 ✅; F1-A2 拒答路径 ✅ (e2e, 无依据不调模型); 引用链路 API 层 ✅; A1/A3/A5 待 live+前端 |
| F2 知识库 | F2-A1..A6 | A2 ✅ A3 ✅ A6 ✅; A1 两阶段状态机全通 ✅ (Stage2 enrich: 词表→KC挂载→前缀→重嵌入, FakeGateway 集成测试; live 运行待录制阶段); A4 双租户隔离 ✅ (≥3库用例待补); A5 待人工抽检 |
| F3 规划 | F3-A1..A4 | ⬜ (M3) |
| F4 苏格拉底辅导 | F4-A1..A3 | ⬜ (M3) |
| F5 出题测评 | F5-A1..A3 | ⬜ (M3) |
| F6 错题本 | F6-A1..A4 | ⬜ (M4) |
| F7 学情报告 | F7-A1..A3 | ⬜ (M4) |
| F8 画像 | F8-A1..A3 | ⬜ (M4) |
| F9 评估看板 | F9-A1..A2 | ⬜ (M2 CLI → M3 页面) |
| F10 运营面板 | F10-A1..A2 | F10-A1 数据面 ✅ (usage_log 三维); 页面 ⬜ (M4) |
| KC 体系 | KC-A1..A3 | A1 ✅ (归一化三表述收敛+别名追加); A3 ✅ (DAG 无环+稳定拓扑序, 17 单测); A2 待 live 词表人工抽检 |
| 数学专项 | M-A1..A2 | ⬜ (M2) |
| 安全 | S-A1..A3 | A1 ✅ (5类注入载荷红队集成测试+对照小节零误杀, 13条双语过滤模式39单测); A2 ✅ (伪装/超限/空/非utf8 全拒, test_upload_security_rejections); A3 ⬜ (审核中间件 M4) |
| 演示工程 | D-A1..A3 | Clock 抽象 ✅ (单测); time-travel API ⬜ (M4) |

## 基线工程质量 (持续)

| 项 | 状态 | 验证 |
|---|---|---|
| 单元测试 | ✅ 22 passed | `make test-unit` |
| 集成测试 | ✅ 3 passed (auth 全链路/租户隔离/健康) | `make dev-infra && make test-integration` |
| Lint (ruff) | ✅ 全过 | `make lint` |
| 结构化日志 (request_id 贯穿) | ✅ | 集成测试输出可见 JSON 日志 |
| 统一错误信封 | ✅ | test_auth_flow 断言 code/request_id |
| 网关四模式 (live/record/replay/stub) | ✅ 单测覆盖 | `make test-unit` |
| 熔断器状态机 | ✅ 4 单测 | `make test-unit` |
| requirements.lock | ✅ | `make lock` |
