# EduAgents 交接文档（新 session 从这里开始）

> 写于 2026-07-27，HEAD = `85241de`（tag `ui-polish`）。本文档是唯一的跨 session 交接入口：
> 先读完本文，再按需查阅文末的文件索引。**注意 §7 的"文档滞后"警告，不要直接相信 STATUS.md 的基线段落。**

## 0. 一句话定位

面向腾讯最高规格竞赛（清北研究生参赛，目标**第一名**）的教育智能体平台，同时要求达到
**真实可运营的生产级**（千万~亿级用户口径），零虚假实现。权威规格 = `docs/IMPLEMENTATION_PROMPT.md`
（F1-F10/KC/M/S/D 验收项 + M0-M5 里程碑 DoD，§13 禁止阈值博弈，§14 反造假条款）。

## 1. 用户工作偏好（必须遵守）

- **中文交流**；回复简洁直接。
- **速度优先**：大任务立即拆给子智能体并行（"太慢了"是最常见的催促）。子智能体因 API 断连挂掉是常态，
  恢复模式：SendMessage 告知"先 git status 看磁盘状态，从断点继续，验收标准不变"。
- **零虚假实现**：不许 mock 冒充真功能。开发期不调真实 LLM（省时间）→ 网关 `GATEWAY_MODE=stub`（桩响应带
  "非真实模型输出"横幅），正式演示用 record→replay 磁带。嵌入/重排是 live（SiliconFlow，便宜）。
- Python 一律 conda 环境 `eduagents`（Python 3.12）+ TUNA 源；npm 用 npmmirror。缺什么直接下载。
- **证据纪律**：每个验收项要有可复现证据（docs/evidence/），诚实口径（例如落地页数据脚注注明
  "合成学生仿真实验，非真实用户统计"）。曾因"五子图"夸大表述被专门修正（D15）。
- 提交习惯：一波工作一个大 commit + tag（现有 11 个 tag），信息用中文多行格式。

## 2. 环境与坑（血泪清单，新 session 必读）

| 坑 | 处置 |
|---|---|
| **机器只有代理出口** | shell 已有 `http(s)_proxy=127.0.0.1:7890`，直连是死的。**dockerd 不读代理 → docker pull 全挂**，修法是 systemd `proxy.conf`（六步指南已给用户，用户尚未执行）→ 这阻塞 M1-D1（compose 验证）和 M3-D1（Langfuse） |
| IPv6 已被用户禁用 | 一律用 `127.0.0.1`，不要用 `localhost`（会解析到 ::1 黑洞） |
| conda libstdc++ ABI 冲突（D10） | 跑任何 Python 前 `export LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib`；pytest 由 conftest ctypes 预加载兜底 |
| `pkill -f` 会匹配到自己的复合命令（exit 144） | 杀进程用 `ss -tlnp` 找 pid 或 `lsof -ti:端口`；**注意 lsof 有时漏报 next-server 残留进程，`ss -tlnp \| grep 3000` 才可靠**（刚踩过：旧 next-server 占着 3000，新服务 EADDRINUSE，三轮截图都打在旧 build 上） |
| Qdrant | Ubuntu 22.04 glibc 太老，用 **musl 静态版 1.18.3** 二进制，dev_infra.sh 负责拉起（服务端模式，D11 的本地文件锁问题已消失） |
| Playwright | 1.62 + 已下载 headless shell；巡检脚本见 §4。fullPage 截图不触发 IntersectionObserver → 脚本里先 scrollThrough 再截（ui_review3.mjs 已内置） |
| .env 有**真实密钥**（gitignored） | DeepSeek + SiliconFlow + Tavily。上线前用户须去控制台轮换（六步指南在 deploy/README-deploy.md）。不要把密钥写进任何提交 |
| SQLAlchemy 无 relationship，UoW 不懂 FK 顺序 | 父表先 `await session.flush()` 再插子表（KC 边、Quiz→Questions 都踩过） |
| RLS 三 GUC | `edu_app`（NOSUPERUSER NOBYPASSRLS）跑应用、`edu` 跑迁移；策略用 `NULLIF(current_setting(...),'')::uuid` 兜空串 |
| 磁带口径 | 语料/enrich 变更会改检索结果 → replay 磁带 key 失配。**终版语料定稿后须重录黄金路径磁带**（见 §6 竞赛收尾） |

## 3. 运行手册

```bash
cd /home/shunw/eduagents
export LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib

make dev-infra        # conda 版 PG(5432)+Redis(6379)+Qdrant(6333, musl 二进制)，幂等
make seed             # 演示账号+高数知识库，幂等

# API（8000）：
/home/shunw/miniforge3/envs/eduagents/bin/python -m uvicorn eduagents_api.main:app \
  --port 8000 --app-dir apps/api/src
# Worker（要跑摄取/反思等异步任务时）：make worker

# Web（3000）：
cd apps/web && npm run build && npm run start    # 或 npm run dev
# next.config.ts 已做同源代理 /api/* → 127.0.0.1:8000（解决跨域 refresh cookie 掉登录）

# 健康：curl http://127.0.0.1:3000/api/health/ready
# 测试：make test-unit / make test-integration（需先 dev-infra）；make lint
# 演示：make demo-m0 .. demo-m5 / make preheat / make backup / make time-travel DAYS=3
```

**账号**：demo@eduagents.cn / demo-pass-123（admin）；smoke-762fa2a2f9@eduagents.cn / smoke-pass-1（冒烟 admin）。

**UI 巡检**：`node apps/web/ui_review3.mjs <email> <password> <输出目录>`（17 屏：落地页双端全页
+ 认证页 + 登录后全功能页 + 移动端抽查，自带横向溢出检测与 console/pageerror 收集）。旧版
scripts/ui_review.mjs、apps/web/ui_review2.mjs 仍可用。

## 4. 架构速览

- **Monorepo**：apps/api（FastAPI 全异步，~18 路由器）、apps/web（Next 16 / React 19 / Tailwind 4，
  16 页路由）、apps/worker（ARQ：14 任务 + 4 定时）、packages/core（网关/解析/切块/嵌入/检索/KC/掌握度/
  缓存/审核/图编排）、alembic 0001-0004、eval/、loadtest/、deploy/。
- **模型网关**（自建）：live/record/replay/stub 四模式；三档降级链 cheap(SiliconFlow Qwen)→
  standard(deepseek-chat)→strong(deepseek-reasoner)；滑窗熔断；BudgetGuard（Lua 原子预扣，
  80/100/150% 三级殺闸）；故障注入；磁带录制回放（130+ 盘）。
- **RAG**：pymupdf4llm → 父子结构切块（公式保护）→ BGE-M3 dense + jieba/BM25 sparse → RRF →
  bge-reranker → Agentic RAG 自省环（路由/查询规划/置信门/评分/改写≤2/web 兜底/拒答/引用支持校验）。
  两级答案缓存（Redis 精确 + Qdrant 语义，数字/否定/中文数字守卫，kb_version 失效，按 owner 隔离）。
- **VLSE**（核心亮点）：KC 词表归一化（exact/alias/cosine≥0.92）+ 先修 DAG + BKT（纯函数）+ FSRS 排程
  + time-travel 虚拟时钟（生产禁用）+ 画像注入 QA + 薄弱点驱动出题 + 对话 Reflection。
- **编排**：LangGraph 四子图 qa/quiz/plan/tutor + PG Checkpointer，interrupt→Command resume 跨 HTTP。
- **多租户安全**：PG RLS FORCE + 三 GUC（user/org/admin，D14 机构库 owner_type 判别）+ refresh 轮换
  与重放吊销 + 输出侧流式分句审核（危机热线 12356）+ 未成年人严格模式 + PIPL 删除/导出/留存 cron。
- **实验**（M5 亮点）：合成学生 IRT 作答 ×3 臂 ×10 seeds，自适应 26.0 题 vs 阶梯 41.7 vs 随机 80.9，
  配对 Wilcoxon p=0.0020，敏感性 10/10（evidence + 落地页引用同口径）。

## 5. 当前状态（以代码和 git 为准）

- **HEAD `85241de` tag `ui-polish`**；全部 17 个提交、11 个 tag（m0→ui-polish）见 `git log`。
- **测试基线（production-hardening 波次实测）：313 单测 + 60 集成全绿；web build 零错（19 页）+
  eslint 全仓零警**。真模型黄金路径（record 口径）8 PASS 含真实拒答。
- 已完成大事记：M0-M4 主体 + M5 实验/压测/答辩材料 + 功能补全波（VLSE 断点/错题本/评估看板/教师端/
  学习指南/URL 导入/审核）+ 生产化审查修复波（**P0×6 全清 + P1×15 全清 + P2×7**，明细
  `git show 1b01dd4`）+ 前端商业级美化三路（设计系统 tokens/BrandMark/营销落地页/全功能页微交互，
  无外部图片字体 CDN，reduced-motion 全降级，17 屏 Playwright 实测）。
- 前端合流备注：`fx-` 前缀动效与 tokens 共存；共享组件 API 定稿于 components/ui/
  （BrandMark/Skeleton/EmptyState/ProgressRing/Celebrate）；components/common.tsx 里还有一套第三路的
  局部 Skeleton/EmptyState 实现，**可择机平移到 components/ui/ 统一**（不冲突，只是重复）。
  代码内遗留 TODO 仅 2 处：hero.tsx aurora token 合流、plan_graph.py 先修 DAG 拓扑序(M4)。

## 6. 待办（按优先级）

**A. 竞赛收尾（无需用户指令，可直接做）**
1. 终版磁带重录：语料定稿后 record 口径重跑黄金路径（scripts/smoke_golden_path.py），替换旧磁带，
   保证断网 replay 演示可靠。
2. STATUS.md / PRODUCTION_GAP_REVIEW.md 刷新（见 §7）。
3. M4-D1 GUI"一镜到底"Playwright 串场脚本（demo 剧本 docs/defense/DEMO_SCRIPT.md 已有）。
4. M4-D2 尾巴：quiz kc_names 从画像自动填充；F8-A2 新会话引用历史。
5. CI 在真实 GitHub runner 上未验证过（.github/workflows/ci.yml 只在本地等价跑过）。

**B. 被用户侧动作阻塞**
- Docker proxy（用户执行 systemd proxy.conf 六步）→ 解锁 M1-D1 compose 全绿验证 + M3-D1 Langfuse 自托管。
- 上线前密钥轮换（用户去 DeepSeek/SiliconFlow/Tavily 控制台）。

**C. 商用四缺（明确等用户点头再做，上次已如实告知"当前不可收费"）**
支付/订阅接入；密码找回 + 邮箱/手机验证 + 图形验证码；监控告警（Langfuse 被 B 阻塞）+ 多供应商 LLM
冗余；法务件（备案/隐私政策，用户侧）。

**D. P2 遗留（18 项中已修 7，确认未修的至少有）**
P2-2 公告/维护模式/feature flag；P2-12/13 embedding/rerank/Tavily 计价计量 + 单价快照；
P2-14 套餐/免费额度分层（UI 层）。其余各项以代码为准逐条核对（别信 review 文档的"均未完成"，见 §7）。

## 7. ⚠️ 文档滞后警告（本次扫描实measured）

1. **STATUS.md 基线段落停留在 232 单测/22 集成、"M5 未开始/F3-F9 未开始"**——那是 m4-stage2 时代的
   快照，之后 wave123 / features-complete / production-hardening / ui-polish 四波都没有回填。
   真实状态以 §5 和 git log 为准。**新 session 第一件杂务可以是刷新 STATUS.md**（逐项对照代码）。
2. **PRODUCTION_GAP_REVIEW.md 是外部审查的"发现清单"，从未标注修复状态**——P0×6/P1×15/P2×7 实际已在
   `1b01dd4` 修复（含 JWT 启动断言 assert_production_secrets、logout 吊销、429+Retry-After、
   幂等 reflect、死信、数据导出、users.status 封禁等）。核对时以 `git show 1b01dd4` + 代码为准。

## 8. 文件索引

| 文件 | 用途 |
|---|---|
| docs/IMPLEMENTATION_PROMPT.md | 权威规格（验收项编号体系） |
| STATUS.md | 验收真值表（**部分滞后**，见 §7） |
| docs/decisions.md | D1-D16 架构决策（无 D13） |
| docs/PRODUCTION_GAP_REVIEW.md | 外部审查发现（修复状态见 §7） |
| docs/research/ACTION_PLAN.md + brief-1..4 | 调研结论与波次规划（已大部分执行） |
| docs/defense/ | PPT 大纲/3 分钟 demo 剧本/QA Top10/竞品矩阵 |
| docs/evidence/ | m0/m2/m4/m5/features 全部证据（截图/评估/压测/实验四图） |
| deploy/ | docker-compose + Caddy + 部署与密钥轮换指南 |
| apps/web/ui_review3.mjs | 最新 17 屏 UI 巡检脚本 |
| ~/.claude/.../memory/ | 跨 session 记忆（项目卡片 + 用户偏好） |
