# M5 四件套执行方案

先说结论:文献核实通过。用"模拟作答模型与掌握度估计模型异构"来做闭环实验是学界标准做法——arXiv 2605.21613 (2026) 正是用 AFM(IRT 族)模拟学生作答、用 BKT 做掌握度追踪的双模型架构;Baker 等人的 BKT 参数恢复研究也用"真值参数生成数据→拟合→比误差"范式。理论上 BKT 稳态分布可推出 IRT logistic 曲线(slip/guess 对应 4PL 上下渐近线),两者同源但参数化独立,循环论证质疑可正面回应。答辩时可直接引用这三条。

---

## 一、合成学生闭环实验(差异化证据,优先级最高)

### 1.0 先修正任务书里的两个内在矛盾

**矛盾 A(量纲)**:θ~U(0.1,0.9) 直接代入 sigmoid(1.7*(θ-b)) 会导致区分度失效——最差学生(θ=0.1)做 easy 题(b=-1)的正确率仍高达 sigmoid(1.87)≈0.87,曲线拉不开。修正:把 θ 解释为 0-1 掌握度标度,作答时先取 z=logit(θ)(值域约 ±2.2,恰好是标准 IRT 能力尺度),作答模型为:

```
P(答对) = c + (1-c) * sigmoid(1.7 * (z - b)),  z = ln(θ/(1-θ))
```

这样 MAE(P_L vs θ) 两边都在 [0,1] 上直接可比,评委(清北研究生)一定会查量纲,这个修正必须做。

**矛盾 B(静态 θ 与"达标题量"冲突)**:若 θ 静态,"全体达到 P(L)>0.8 所需题量"只反映估计器收敛速度而非教学效果;若 θ 动态,MAE 收敛曲线又混入学习效应。修正:拆成两个子实验,各回答一个问题——

### 1.1 实验 A:估计收敛(θ 静态)——"我们的掌握度数字准不准"

- 设置:N=30 学生 × K=8 个 KC(用现有先修 DAG 的一条子图),θ_(s,k)~U(0.1,0.9) 固定不变。
- 题库:每 KC 30 题,难度 b∈{-1,0,+1} 各 10 题;猜对率 c 按题型:单选(4选1) 0.25、判断 0.5、多选 0.06、填空 0.05。
- 轮数:每学生 T=40 题(每 KC 平均 5 题即可见收敛拐点),每轮记录全体 MAE(P_L, θ)。
- 收敛判定:滑窗 5 轮的 MAE 变化量 < 0.01,且与第 40 轮终值差 < 0.02;报告"达到收敛所需轮数"。
- 预期结论:自适应策略(优先 0.3<P(L)<0.8 的 KC + 难度匹配当前估计)比随机选题更快压低 MAE(信息量更大的题 → 更快收敛),终局 MAE 两者接近但自适应用更少题达到。

### 1.2 实验 B:学习效率(θ 动态)——"自适应教学省多少题"

- θ 增长模型(对两策略完全一致,隐藏于环境):每次练习 KC k 后,θ ← θ + λ*(θ_max - θ),λ=0.15,θ_max=0.95;答对时全额增长,答错时半额(练习即有学习,符合 doer effect)。先修约束:前置 KC 的 θ<0.5 时,后继 KC 增长率减半(体现 DAG 价值)。
- 终止:学生所有 KC 的 P(L)>0.8 即毕业;单学生上限 200 题(防随机策略跑不完)。
- 主指标(预注册,唯一主指标):毕业所需题量;次要指标:200 题内毕业率、终局真实 θ 均值(防"只刷估计不刷学习")。

### 1.3 统计设计(评委必查)

- **Seeds:10 个随机种子**,每 seed 重新抽 θ 和作答随机数;两策略在同一 seed 下共享同一批学生和题库(**配对设计**,方差大幅减小)。
- 检验:对 10 个 seed 级均值做 **Wilcoxon 符号秩检验**(n=10 配对,不假设正态),报告 p 值 + **Cohen's d 效应量** + 跨 seed 95% CI。样本层面等效 300 虚拟学生/策略,回应"N=30 太小"。
- 敏感性分析(各重跑一遍,写进附录,答辩弹药):
  - BKT 估计器参数 P(T)/P(G)/P(S) 各扰动 ±50% → 结论方向不变;
  - 掌握阈值 0.8 / 0.9 / 0.95 三档(文献标准是 Corbett & Anderson 的 0.95);
  - 学习率 λ ∈ {0.08, 0.15, 0.25}。
- BKT 估计器参数与作答模型参数**互不共享**:估计器用产品线上默认值 P(L0)=0.3, P(T)=0.15, P(G)=0.2, P(S)=0.1,不告知题型真实 c。

### 1.4 图表形态(4 张,进 PPT 第 6 页)

1. **收敛曲线**:x=轮数,y=MAE,两策略两条线 + 跨 seed 95% CI 阴影带(实验 A 主图);
2. **毕业生存曲线**(比箱线图更有冲击力):x=已做题量,y=尚未全 KC 达标的学生比例,Kaplan-Meier 风格,两策略两条,横向差距就是"省的题量";
3. **校准散点**:终局 P(L) vs 真值 θ,对角线参考,按策略着色(展示"数字可信");
4. **第三臂对照**(可选加分):加"固定难度阶梯"非自适应启发式策略,证明赢的不只是"随机太弱"。

### 1.5 被攻击点与预答(背熟)

| 攻击 | 回应 |
|---|---|
| 模拟学生循环论证 | 作答模型是 IRT(连续能力+难度+猜测,含题型异质 c),估计模型是 BKT(二元潜变量 HMM),参数互不共享、结构异构;与 arXiv 2605.21613 的 AFM 模拟+BKT 追踪同范式 |
| 模拟学生不等于真人 | 定位是算法层验证(estimator 正确性 + 策略相对优劣),不宣称替代真人实验;同时给出小规模真人试用计划 |
| 随机基线太弱 | 有第三臂难度阶梯策略 |
| 增长模型是你编的 | 增长机制对两策略一致且隐藏,只影响绝对值不影响相对结论;λ 做了三档敏感性 |
| 阈值 0.8 哪来的 | 文献标准 0.95,我们报 0.8/0.9/0.95 三档,结论稳定 |
| 为何不用 DKT | 逐 KC 可解释可审计、冷启动无需大数据,与产品"可验证"主线一致 |

### 1.6 工程落地

纯数值仿真,模型网关走 stub,零 LLM 成本,单机分钟级跑完。产出:`experiments/synthetic_students/`(runner + config.yaml + 输出 CSV)+ 前端评估看板一页(读 CSV 画上述 4 图)。合成学生代码答辩后还能当**持续回归资产**卖点(改 BKT 实现前后跑一遍即回归测试)。

---

## 二、黄金集 v2

### 2.1 干扰语料

再生成 2 篇相邻学科文档(各 3000-5000 字,与主文档同一租户入库),硬性要求:与主文档共享至少 30% 的表面术语但语义错位(例:主文档"操作系统"的"调度/锁/页" vs 干扰文档"数据库"的事务调度、行锁、页结构)。这使 doc-level 检索不再天然满分,BM25 会被表面重叠误导,恰好凸显 rerank 价值。

### 2.2 难例类型清单(10 类)

1. 公式/图/编号引用("式(3.2) 的适用条件是什么")
2. 跨节比较("第 2 节的 X 和第 4 节的 Y 有何区别"——多 gold chunk)
3. 否定式提问("以下哪种情况**不**适用……")
4. 术语别名/缩写(用 KC 词表里的别名提问,验证归一化正向收益)
5. 多跳(答案需两个不相邻 chunk 拼接)
6. **无答案题**(答案不在库中,期望明确拒答——直接测幻觉,答辩杀手锏)
7. 干扰针对题(问主文档,但干扰文档存在表面高相似段落)
8. 数字/单位精确性(答案是具体数值,LLM 易篡改)
9. 表格内容提问
10. 长尾同义改写(与原文零词面重叠的问法,测 BGE-M3 语义召回)

### 2.3 规模与标注 schema

60 题 = v1 保留 15 + 新增 45(每类 4-5 题)。每题标注:`{qid, question, type_tag, gold_chunk_ids: [], gold_answer_points: [], answerable: bool}`。gold_chunk_ids 在摄取后人工对照 chunk 边界确定,支持多 chunk。

### 2.4 判定升级:doc-level hit@5 → chunk-level 全家桶

- 检索端:chunk hit@1/3/5、MRR、nDCG@5(graded:2=含直接答案,1=支持性上下文,0=无关);
- 生成端:**引用精确率**(答案引用的 chunk ∈ gold 集的比例)+ **拒答正确率**(6 类无答案题);
- 自动化:chunk id 精确匹配为主;chunk 切分参数变更导致 id 漂移时,用 gold answer 要点文本与 chunk 的字符重叠率 > 0.6 兜底匹配;答案要点覆盖率用 LLM-judge,走网关 record 模式固化,保证评测可复现;
- 报表:**按 10 类难例分桶的 hit/MRR 矩阵**——"我们精确知道系统边界在哪"本身就是答辩的完成度证据,哪怕某些桶分数低。

---

## 三、Locust SSE 压测

### 3.1 两口径

| 口径 | 并发 | 目的 | 通过基线 |
|---|---|---|---|
| stub 档 | 100(阶梯 10→50→100,各 2 分钟,LoadTestShape) | 检索/图/框架真实,仅 LLM 打桩;测架构开销与拐点 | TTFE p95 < 500ms,错误率 < 1% |
| live 档 | 10(恒定,10 分钟) | 真实 DeepSeek;测 TTFT 与成本 | TTFT p95 < 3s,断流率 0 |

场景权重:问答流式 70% / 出题 15% / 复习队列 10% / 报告 5%。

### 3.2 SSE 正确写法(关键:Locust 默认计时只到响应头,必须手动计时 + 自定义事件)

```python
from locust import HttpUser, task, between, events
import time, json, random

class QAUser(HttpUser):
    wait_time = between(1, 3)

    @task(7)
    def ask_stream(self):
        start = time.perf_counter()
        ttfe = ttft = None; n_tokens = 0; err = None
        with self.client.post("/api/qa/stream",
                json={"question": random.choice(QUESTIONS)},
                headers={"Accept": "text/event-stream"},
                stream=True, catch_response=True, name="qa_stream",
                timeout=(5, 60)) as resp:
            try:
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    now = time.perf_counter()
                    if ttfe is None: ttfe = now - start
                    evt = json.loads(line[5:].strip())
                    if evt.get("type") == "token":
                        n_tokens += 1
                        if ttft is None: ttft = now - start
                total = time.perf_counter() - start
                if ttft is None:
                    resp.failure("stream ended with no token"); err = "no_token"
                else:
                    resp.success()
            except Exception as e:
                resp.failure(str(e)); err = type(e).__name__; total = time.perf_counter() - start
        # 自定义指标:每个流式请求上报 3 条,Locust 统计表里分行显示
        for name, val in (("qa:ttfe", ttfe), ("qa:ttft", ttft), ("qa:total", total)):
            if val is not None:
                events.request.fire(request_type="SSE", name=name,
                    response_time=val * 1000, response_length=n_tokens,
                    exception=None if err is None else Exception(err), context={})
```

要点:用 `HttpUser`(requests + gevent)而非 `FastHttpUser`——后者流式迭代支持不完整,100 并发 SSE 对 requests 毫无压力;`catch_response=True` + 固定 `name=` 聚合统计;`timeout=(连接, 读)` 双超时防挂死;主指标看自定义事件 `qa:ttft`,原生 `qa_stream` 行的 response_time 仅是到响应头的时间,报告里要注明。

### 3.3 报告字段

并发数、RPS、TTFE p50/p95、TTFT p50/p95、总时长 p50/p95、单流 tokens/s、错误率(超时/5xx)、**断流率**(已收 token 后中断)、服务端对照埋点(Qdrant 检索延迟、PG 连接池占用、CPU/内存);live 档另加:上游模型延迟占总时长比例、每请求成本(接现有成本面板)、语义缓存命中率(缓存开/关各跑一轮,命中率提升就是缓存功能的实测证据)。

---

## 四、答辩材料

### 4.1 PPT 10 页(主线:VLSE 可验证学习者状态引擎)

1. **封面 + 一句话**:"把'学生学会了吗'从黑盒直觉,变成可验证、可审计、可实验证伪的工程量"
2. **痛点**:现有 AI 教育产品 = 聊天皮套;无状态、无证据、答案不可溯源、效果不可验证
3. **总架构一图**:LangGraph 五子图 + 模型网关四模式 + Qdrant 混合检索 + RLS 多租户
4. **VLSE 核心**:三层状态(BKT 掌握度 × FSRS 记忆 × KC 先修 DAG)+ 证据链——每个掌握度数字可点开看是由哪几道题推出来的
5. **可信 RAG**:混合检索 + 引用点击回原文 + 黄金集 v2 十类难例分桶矩阵 + 无答案拒答率
6. **闭环实验**(差异化证据页):合成学生 A/B,收敛曲线 + 毕业生存曲线,p 值与效应量,异构模型设计说明
7. **工程深度**:四模式网关(record/replay = 评测可复现)、注入防御、预算强制、成本面板
8. **压测**:stub 100 并发 / live 10 并发关键数字表
9. **Demo 索引 + time-travel**:虚拟时钟快进遗忘曲线(演示环节最惊艳的一手,单独占页)
10. **路线图**:机构共享知识库 / 合成学生作为持续回归资产 / 真人小规模试用计划

### 4.2 三分钟 demo 剧本(黄金路径,全程 replay 模式兜底预录)

- 0:00-0:30 上传文档 → 两阶段摄取 → KC 词表 + 先修 DAG 可视化("系统读懂了这本书的知识结构")
- 0:30-1:15 提问 → 真 token 流式 → **点击引用跳回原文高亮** → 点追问 chip("每句话都有出处")
- 1:15-2:00 出题 → interrupt 蓝图确认(人在环)→ 作答 → 掌握度条实时变化("每道题都在更新你的状态")
- 2:00-2:40 time-travel 快进 7 天 → FSRS 复习队列浮现 → 学情报告("系统知道你会忘")
- 2:40-3:00 成本面板 + 合成学生实验看板收尾("这一切,我们用 300 个虚拟学生验证过")

风险预案:live 网络故障 → 网关一键切 replay,演示照常;这本身可当场秀一下,是加分项。

### 4.3 评委必问 Top10 与答案要点

1. **合成学生是不是循环论证?** 作答 IRT / 估计 BKT,结构异构、参数不共享;文献同范式(AFM 模拟 + BKT 追踪);且做了估计器参数 ±50% 敏感性。
2. **与直接套 ChatGPT 的区别?** 差在状态引擎:掌握度、遗忘、先修 DAG、证据链、可复现评测——prompt 包装做不出"可验证"。
3. **BKT 过时了,为何不用 DKT/LLM 打分?** 逐 KC 可解释可审计、冷启动零数据可用、与"可验证"主线自洽;架构留了 estimator 接口可替换。
4. **RAG 幻觉如何治理?** 引用强制 + 无答案题拒答测试(黄金集第 6 类)+ 分桶指标暴露边界。
5. **多租户数据安全?** PG RLS 行级隔离(tenant_id=user_id)+ 注入防御(可现场演示攻击被拦截)。
6. **成本怎么控?** 预算强制 + 语义缓存(压测报命中率实数)+ 成本面板;网关多供应商可切换。
7. **掌握阈值/参数依据?** 文献标准 0.95(Corbett & Anderson),报三档敏感性,结论稳定。
8. **真人验证过吗?** 诚实回答:当前是算法层合成验证,真人小规模试用在路线图,不夸大——评委反感过度宣称。
9. **为何 FSRS 不用 SM-2/艾宾浩斯?** FSRS 是现代开源基准中预测精度最优的记忆模型,参数可从作答记录拟合,与 BKT 互补(会不会 vs 忘没忘)。
10. **并发扩展性?** 全 async + ARQ 队列,stub 档 100 并发数字在手;瓶颈在上游 LLM 而非本系统,网关层可横向多供应商分流。

---

## 执行顺序与工作量

1. 合成学生实验(2 天:runner 1 天、跑图+敏感性 1 天)——证据链最长,先做;
2. 黄金集 v2(1.5 天:干扰语料 0.5、出题标注 0.5、chunk 级判定改造 0.5);
3. 压测(1 天:脚本 0.5、两口径跑+报告 0.5);
4. PPT + demo 排练(1.5 天,素材全部来自前三项产出)。

关键文件建议路径:`experiments/synthetic_students/`、`eval/golden_v2/questions.jsonl`、`loadtest/locustfile.py`、`docs/defense/`。

Sources:
- [Simulating Learners' Task-Selection Strategies and System Constraints in Mastery Learning (arXiv 2605.21613)](https://arxiv.org/pdf/2605.21613)
- [Simulated Students: Models and Applications](https://www.emergentmind.com/topics/simulated-students)
- [Bayesian Knowledge Tracing (BKT-IRT 理论联系)](https://www.emergentmind.com/topics/bayesian-knowledge-tracing)
- [Degree of Error in BKT Estimates (Baker et al., 参数恢复范式)](https://learninganalytics.upenn.edu/ryanbaker/behaviormetrika_vfinal.pdf)
- [Optimizing Mastery Learning by Fast-Forwarding Over-Practice Steps (arXiv 2506.17577)](https://arxiv.org/pdf/2506.17577)
- [Personalized Stopping Rules in Bayesian Adaptive Mastery Assessment (arXiv 2103.03766)](https://arxiv.org/pdf/2103.03766)
- [Load Testing LLM APIs with Streaming (TTFT 自定义事件写法)](https://loadforge.com/guides/ai-llm/load-testing-llm-apis-streaming-non-streaming)
- [Load Testing SSE with Locust 实践](https://medium.com/@hiteshpara762/how-i-load-tested-a-conversational-ai-chatbot-agent-with-a-streaming-endpoint-c8e26e7a091b)