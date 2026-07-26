# 竞品对比矩阵

> **取证纪律（先说清楚，这决定这张表能不能被信）**
> 1. **我方（EduAgents）每一格**都标注了仓库内的出处文件或页面路径，**未做的一律写「未做」，半成品一律写「部分」并说明缺口**。唯一事实源是 `STATUS.md`。
> 2. **竞品格**的依据分三档，逐格标注：
>    - `[调研]` = 来自 `docs/research/brief-4-M5实验与答辩.md`（实为竞品交互调研简报，含来源链接）
>    - `[公开]` = 厂商公开文档 / 产品形态的常识性判断
>    - `[未核实]` = **公开信息有限，我们未做独立核实**——这类格子答辩时必须说"我不确定"，**不得当作打击对手的论据**
> 3. 本表**不做诋毁**。竞品在其主战场强于我们的地方，照实写强。

---

## 一、主矩阵

| 维度 | ChatGPT / 豆包 | Google NotebookLM | Coze / Dify | 学而思 / 作业帮 AI | **EduAgents（我方）** |
|---|---|---|---|---|---|
| **知识库溯源** | **弱–中** `[调研]`<br>联网回答带信源角标（公众号名 + 日期）可点击跳转；私域文档溯源不是主形态 | **强（行业标杆）** `[调研]`<br>三段式：灰色数字角标内嵌句中 → 悬停浮层显示被引原文 → 点击左侧来源查看器**滚动到被引段落并高亮** | **中** `[调研]`<br>「引用和归因」是应用级可配置开关；开启后回答下方折叠展示命中分段 + 所属文档，可展开原文 | **弱** `[未核实]`<br>以解题讲解为主，未见系统性的文档级引用溯源 | **中** ✅+🟡<br>已有：引用面板含文档名 + `heading_path` + 页码 + 命中原文片段；生成阶段即锚定 `chunk_id`；**无依据直接拒答**<br>**未做**：点击跳回原文定位高亮（**依赖 Wave1**）；引用卡片内的公式仍是 LaTeX 源码未渲染 |
| **学情建模** | **无** `[公开]`<br>有自然语言记忆，但不可计算、不可审计、不驱动调度 | **无** `[公开]`<br>定位是文档理解工具，非学习者建模 | **无** `[公开]`<br>定位是 Agent 开发平台，学情建模需自建 | **有，但不可审计** `[未核实]`<br>大厂题库 + 学情报告是其强项，但模型与数据闭源，外部无法核对某个掌握度数字的来源 | **强** ✅<br>KC 受控词表（两遍式归一化，别名 + 向量兜底合并）+ **先修 DAG**（无环检测）+ BKT 逐 KC 掌握度与置信度；**每次变化写 `mastery_log`（`old_p→new_p, evidence_type, evidence_id, weight`）**；主观题证据 **×0.5 降权系数直接暴露在 UI 上** |
| **复习调度** | **无** `[公开]` | **无** `[公开]`<br>Studio 可一键生成 Flashcards / Quiz，但**不排期、不追踪遗忘** | **无** `[公开]` | **有** `[未核实]`<br>有错题本与复习功能；调度算法未公开，行业常见为固定间隔 | **强** ✅<br>FSRS（py-fsrs）为每道错题与每个 KC 各挂一张 Card；错题状态机（新错→复习中→已攻克→归档）+ 错因五分类；**全局虚拟 Clock 抽象**使遗忘曲线可现场演示、可自动化断言（`make time-travel DAYS=N`，禁止改系统时钟） |
| **多智能体编排** | **隐式** `[公开]`<br>用户不可见、不可控、不可讲解 | **无** `[公开]` | **强（其主战场，强于我方）** `[调研]`<br>可视化工作流编排 + 分段预览可编辑 + 知识库分级权限 + 召回测试页留存检索事件可逐条回放 | **未公开** `[未核实]` | **中** ✅+⚠<br>LangGraph **四个子图**（qa / quiz / plan / tutor）+ PG Checkpointer；`interrupt()` 人在环确认（出题蓝图 / 计划草案）→ 跨 HTTP 请求 `resume` 恢复，越权 resume 返回 404<br>**⚠ 口径纠正：仓库中无 analyst 子图、无顶层 Supervisor 节点**，学情诊断实现在服务层（`routers/reports.py`）。**答辩不得说"五子图 + Supervisor"** |
| **可复现评测** | **无** `[公开]` | **无** `[公开]` | **部分** `[调研]`<br>Dify 有召回测试页（命中分段 + 相似度分数 + 可临时改检索模式/top-k，仅对当前测试会话生效），并留存检索事件记录；**但没有随产品交付的固定黄金集与消融基线** | **未公开** `[未核实]` | **强** ✅+🟡<br>已有：黄金集 v1（15 问，与语料版本绑定冻结）+ 三配置消融对照 + 评测 CLI（`make eval-retrieval`）+ **record/replay 网关模式固化 LLM 响应使跑分可复现** + 知识库页内置「检索调试面板」<br>**并且如实公开了不利结论**：关掉 rerank 反而 MRR 更高，附原因解释与处置<br>**待产出**：黄金集 v2（60 题 / 10 类难例 / 干扰文档 / chunk 级判定 / 拒答正确率）、闭环有效性实验、评估看板页面 |
| **私域部署** | **否** `[公开]`<br>SaaS，数据出网 | **否** `[公开]`<br>Google 云端 | **是（Dify/Coze Studio 开源可自托管）** `[调研]` | **否** `[公开]`<br>面向 C 端 App | **是（设计上）** ⛔<br>`deploy/docker-compose.yml` 全套已配（frontend / api / worker / postgres / redis / qdrant / minio / caddy + profiles）+ healthcheck；模型端点、模型 ID、全部开关走 env，密钥零硬编码<br>**⛔ 但 `make up` 当前跑不起来**：本机 Docker daemon DNS 污染且无免密 sudo（`docs/decisions.md` D5，`STATUS.md` M1-D1 BLOCKED）；开发与集成测试退到本地 conda 版 PG/Redis |

---

## 二、我方每一格的证据索引

| 维度 | 证据文件 / 页面 / 命令 |
|---|---|
| 知识库溯源 | `docs/evidence/m4/03-chat-answer.png`（5 条引用 + 12 条过程事件）· `apps/web/components/chat/citations-panel.tsx` · `docs/api-contract.md` citation 事件 payload · `STATUS.md` M2-D2 🟡（API 层 ✅，前端点击定位待 Wave1）· 拒答：`STATUS.md` M3-D5 ✅ |
| 学情建模 | `docs/evidence/m4/08-profile.png`（`0.30→0.24→0.23`，`×0.5` 权重可见）· `docs/data-model.md` §「学习者状态引擎 VLSE」· `tests/unit/test_bkt.py`（11 项）· `tests/unit/test_kc.py`（17 项，含三表述收敛与 DAG 无环）· `tests/integration/test_reviews_flow.py`（`mastery_log` 逐条 `evidence_id` 关联 attempt）· `STATUS.md` M4-D4 ✅ / KC-A1 ✅ / KC-A3 ✅ |
| 复习调度 | `docs/evidence/m4/07-reviews.png`（KaTeX 渲染真题 + 错因标签 + 到期时间 + 变式重练）· `tests/unit/test_scheduler.py`（6 项）· `tests/unit/test_clock.py`（3 项）· `scripts/time_travel.py` · `make time-travel DAYS=3` · `STATUS.md` M4-D2 🟡（答错→入队→+3 天→重现 ✅；"针对薄弱点自动出题"的画像联动待接） |
| 多智能体编排 | `packages/core/src/eduagents_core/graphs/`（`qa_graph.py` / `quiz_graph.py` / `plan_graph.py` / `tutor_graph.py` / `state.py`）· `tests/integration/test_quiz_flow.py`（interrupt→跨请求 resume→落库；越权 resume 404）· `STATUS.md` M3-D2 ✅ / M3-D3 ✅ |
| 可复现评测 | `docs/evidence/m2/ablation_summary.md` · `docs/evidence/m2/retrieval_eval_live_full.md`（15 问逐题名次）· `docs/evidence/m2/retrieval_eval_live_no-sparse.md` · `docs/evidence/m2/retrieval_eval_live_no-rerank.md` · `eval/run_retrieval_eval.py` · `make eval-retrieval` · `cassettes/`（109 个）· `docs/evidence/m4/04-kb.png`（检索调试面板）· `STATUS.md` M2-D3 ✅ / M2-D5 ✅ |
| 私域部署 | `deploy/docker-compose.yml` · `.env.example` · `README.md`「Docker 修复」· `docs/decisions.md` D5 · `STATUS.md` M1-D1 ⛔ |

---

## 三、我方明确「未做」清单（答辩时主动交代，不等评委挖）

| 项 | 状态 | 出处 |
|---|---|---|
| 引用点击跳回原文并高亮定位（NotebookLM 三段式第三段） | **未做**，Wave1 | `STATUS.md` M2-D2 🟡 |
| 引用卡片内的数学公式渲染（当前显示 LaTeX 源码） | **未做** | `apps/web/components/chat/citations-panel.tsx` 未接 `MathText` |
| 追问 chips（认知递进追问） | **未做**，Wave1 | 前端无相关组件 |
| 苏格拉底辅导前端页（提示阶梯 / 复盘卡） | **未做**（后端 tutor 子图 + hint_level 状态机已有，7 单测） | `STATUS.md` M3-D4 🟡 |
| 学情报告独立前端页（雷达 / 趋势 / 三段式诊断下钻） | **未做**（后端 `GET /api/reports/learning` 已有，且 stub 模式诚实返回 null 不冒充） | `apps/web/app/` 无 reports 路由 |
| 评估看板页面（F9-A1/A2：页面一键触发评估并渲染） | **未做** | `STATUS.md` F9 ⬜ |
| 教师组卷导出页（U-A4） | **未做**（`require_teacher` 中间件已就位） | `STATUS.md` U-A4 ⬜ |
| 语义缓存 / 预算强制降档 | **未做**，Wave1 | `docs/research/ACTION_PLAN.md` 包 B |
| Langfuse 完整 trace 树 | **⛔ BLOCKED**（Docker 阻塞；图内 trace 事件流已全量落 `state.trace` 并经 SSE 透出） | `STATUS.md` M3-D1 |
| `docker compose up` 全绿 | **⛔ BLOCKED** | `STATUS.md` M1-D1 · `docs/decisions.md` D5 |
| 压测报告（两档口径） | **待 M5 产出**（`loadtest/` 目录当前为空） | `STATUS.md` M5-D1 ⬜ |
| 闭环有效性实验（合成学生） | **待 M5 产出**（无 `eval/simulation/`） | `STATUS.md` M5-D3 ⬜ |
| 校验器信度评估（三个 LLM 判断器 × 20 条人工对照集，一致率 ≥85%） | **未做** | `IMPLEMENTATION_PROMPT.md` §12.4 |
| 真实成本数字 / 单位经济学 | **无数据**（运营页当前为 **stub 口径伪定价**，禁止引用） | `docs/evidence/m4/09-admin.png` |
| 延迟三档口径达标验证 | **未做**（stub 口径下测量无意义） | `STATUS.md` M3-D6 ⬜ |
| 机构共享知识库 | **未做**，Wave3（数据模型已定案） | `docs/research/ACTION_PLAN.md` §3 Wave 3 |
| 真人用户验证 | **未做**，路线图 | 见 `QA_TOP10.md` Q5 |

---

## 四、答辩用的三句话总结

1. **不争的地方**：溯源交互的行业标杆是 NotebookLM，可视化编排的行业标杆是 Coze/Dify，题库与真人数据规模的优势在学而思/作业帮。**这三点我们都承认。**
2. **真正的分界线**：前四家产品优化的都是**一次交互**——一次问答、一次文档理解、一次工作流、一次讲题。EduAgents 优化的是**一个学生在时间轴上的状态**。这需要一张带外键的学习者状态表和它的审计日志，**这是提示词包装做不出来的**。
3. **最难被复刻的一格**：**可复现评测**。我们不仅有黄金集和消融，还把"关掉 rerank 反而指标更高"这个**对自己不利的结论**印在了报告和 PPT 上并给了解释。护城河不在技术难度，在工程纪律。

---

## 五、调研来源

竞品交互细节的一手来源见 `docs/research/brief-4-M5实验与答辩.md` 末尾的 Sources 列表（NotebookLM 引用机制、腾讯 ima 知识库、豆包电脑版、Dify 召回测试页与引用归因开关、Coze 分段编辑、Khanmigo 分层提示、Duolingo Explain My Answer 等，共 18 条链接）。

> **⚠ 文件名提示**：`docs/research/` 下四份简报的**文件名与内容不对应**（`brief-4-M5实验与答辩.md` 装的是竞品体验调研，`brief-3-技术专项.md` 装的是 M5 四件套执行方案，`brief-1-竞品体验.md` 装的是四项技术决策）。引用时请以内容为准，或先重命名。
