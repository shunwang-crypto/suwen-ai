# 竞赛冲刺待办说明（2026-07-27）

> [!WARNING]
> **历史待办。** 本文仅保留竞赛冲刺阶段的上下文，条目不代表当前缺口、优先级或开发规范。
> 当前状态见 [`STATUS.md`](../STATUS.md)，后续开发从 [`docs/README.md`](README.md) 进入。

> 本文只解释「是什么 / 在哪 / 为什么要做」，执行计划见 docs/HANDOFF.md §6。

## 1. 磁带（cassettes）

**位置**：`cassettes/`（355 盘 JSON，2.3 MB，按哈希两级目录）

LLM 请求/响应的录音。每盘 `{text, model, prompt_tokens, completion_tokens}`，
文件名 = SHA256(prompt 全文 + 模型档位 + 温度)。`.env` 的 `GATEWAY_MODE` 三态：
`record` 真调并存盘 / `replay` 只从盘取不联网 / `live` 纯真调。
用途：答辩断网仍可跑完整演示；CI 零 API 成本。

**注意**：磁带存的是模型「回来」的内容；人格/教学法写在 `prompts/` 与各 graph 的
注入段里（发「过去」的内容），两者无关。

### 待办 A：重录磁带

1. prompt 改动使旧盘哈希失配 —— 个性化两波改过 qa/quiz/plan/tutor 四子图，
   本轮又改过学情报告 narrative prompt，这些盘已命不中。
2. **355 盘中 83 盘是桩响应**（`"model": "stub-cheap"`，text 含 `"stub": true`）：
   录制时上游超时→网关降级返回开发占位文本→被当成有效响应存盘。
   replay 演示会把「非真实模型输出」当答案播出去。
3. `scripts/record_demo_tape.py` 已写好（含 no_stub 断言防上述污染），**从未跑过**。

命令：
- 录制：API/worker 以 `GATEWAY_MODE=record` 启动 → `python scripts/record_demo_tape.py --reset`
- 验证：`GATEWAY_MODE=replay` + `GATEWAY_REPLAY_FALLBACK_STUB=false` →
  `python scripts/record_demo_tape.py --reset --expect-mode replay`

## 2. 答辩材料（PPT）

**位置**：`docs/defense/`，四个 Markdown，无 .pptx（需人工照大纲做）

| 文件 | 内容 |
|---|---|
| `PPT_OUTLINE.md`（42 KB） | 十页幻灯片逐页大纲 |
| `DEMO_SCRIPT.md`（24 KB） | 3 分钟演示剧本 + 口播稿 + 超时预案（§6 为个性化段） |
| `QA_TOP10.md`（29 KB） | 评委十问背诵答案 |
| `COMPETITOR_MATRIX.md`（11 KB） | 竞品对比矩阵 |

### 待办 B：材料换代（性价比最高，约 1 天）

`PPT_OUTLINE.md` 与 `QA_TOP10.md` 滞后一代，仍写着：
- 「188 单测」→ 实际 441 单测 + 98 集成
- 「第 6 页待 M5 产出，无任何实验数据文件」→ `docs/evidence/m5/` 已有 p=0.0020
  完整报告 + 四张图
- 「loadtest/ 目录为空」→ 已有压测报告
- 未收录 60 题黄金集 v2 评估、个性化两波

另需处理三处风险：
- 主动讲实验 A 的诚实失败（BKT misspecification 分析）——不讲会被当漏洞，
  主动讲是科学素养加分
- 撤掉屏显 Cohen's d=14.26（配对共同随机数致方差极小，统计背景评委会嘲讽），
  只报题量差与 p 值
- `QA_TOP10.md` 多处背诵「STATUS.md 可当场打开」——STATUS 已于本轮回填，可保留

## 3. 商用闭环文件（已提交于 `ddcad6c`，非游离残留）

约 1000 行，补「商用四缺」中的支付与账号安全两缺：

| 文件 | 行数 | 作用 |
|---|---|---|
| `apps/api/src/eduagents_api/wallet.py` | 379 | 余额/额度账本 |
| `alembic/versions/0006_billing.py` | 181 | 计费建表迁移 |
| `apps/api/src/eduagents_api/routers/billing.py` | 150 | 计费 HTTP 接口 |
| `apps/api/src/eduagents_api/emailer.py` | 111 | 发信（密码找回） |
| `apps/api/src/eduagents_api/payments.py` | 97 | 支付渠道对接 |
| `apps/api/src/eduagents_api/captcha.py` | 96 | 图形验证码 |
| `apps/web/app/{billing,forgot,reset}/` | — | 前端三页 |

### 待办 C：审查或摘除

由子智能体在 API 中断前生成，**未逐行审查过**。涉及钱的逻辑不该无人复核即留主干。
二选一：
- 完整审一遍（重点：余额扣减原子性、支付回调幂等与验签、并发扣减）
- 答辩前从主干摘掉，避免被问「这个支付真能用吗」答不上

## 4. 战略补强（三件事，共用已有基建，约一周）

背景：工程完成度是入场券，不是记忆点。当前**全部证据均为自产**（自写语料、
自出黄金集、自造合成学生），缺第三方锚点。这三件补的是「真实感」与「可量化自证」。

### ① 真实数据重放（2-3 天）

- **Junyi**：台湾均一教育平台（Khan 中文版）公开集，2590 万作答，
  **含专家标注知识点先修关系**，中文数学，最贴合我们
- **EdNet**（Riiid）：78.4 万学生 / 1.31 亿交互
- 做法：真实作答序列喂进现有 BKT/FSRS，真实掌握度曲线与合成学生曲线并排
- 效果：不招募真人即补上「无真实用户」短板 ——
  从「用自己造的学生验证」变成「在 78 万真实学生数据上跑过」

### ② 合成学生效度检验（+2 天）

现有闭环实验（自适应 26.0 题 vs 随机 80.9 题，p=0.0020）用的是合成学生，
易被一句「学生是你自己编的」削掉一半价值。
做法：用①的真实能力分布校验合成学生分布吻合度（KS 检验等）。
**全场独一份** —— 需先有合成学生闭环基建，他人短期抄不出。
效果：合成学生从「玩具」升格为「经过效度检验的科学仪器」。

### ③ MathTutorBench 公开基准（2 天）

EMNLP 2025 数学辅导质量基准，7 任务（苏格拉底提问 / 错误定位 / 脚手架生成等），
开源可本地跑，judge 仅 1.5B（单卡十分钟，成本数美元）。
跑「注入学习者模型的辅导链路 vs 裸大模型」消融。
效果：全场唯一第三方标尺 —— 别人的效果自说自话，我们有可复现的分数。

**串起来的定位**：
> 我们不是又一个教育智能体，而是一台经过 78 万真实学生数据校准、
> 能在公开基准上量化自证的教育实验机器。

## 5. 优先级建议

1. **待办 B** 答辩材料换代（1 天，性价比最高；不做则以弱一代的形象出场）
2. **待办 A** 磁带重录（半天；不做则断网兜底不可靠，且 83 盘桩响应会当场露馅）
3. **待办 C** 商用闭环审查或摘除（半天）
4. **战略补强 ①→②→③**（约一周，按序做，后两件依赖第一件）
