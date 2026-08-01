# EduAgents 后续行动方案

> [!WARNING]
> **历史研究材料。** 本文记录当时的调研结论和行动建议，不代表当前实现、产品优先级或工程规范。
> 当前文档入口与真值层级请以 [`docs/README.md`](../README.md) 为准。

## 1. 调研核心结论

1. NotebookLM 引用三段式(角标内嵌+悬停浮层+点击跳原文高亮)是行业标杆交互, 演示时评委必点。(竞品简报)
2. 追问 chips 与"考考我"固定 chip 可在问答子图末节点同次调用生成, 零额外成本高感知价值。(竞品简报)
3. Dify 式召回测试页(展示 BM25/向量/RRF/rerank 各阶段分数)是向清北评委展示混合检索技术深度的最佳窗口。(竞品简报)
4. Khanmigo 提示阶梯(引导问→小提示→大提示→相似例题, 永不直出答案)+ Lily 式会话复盘卡, 构成辅导页差异化叙事。(竞品简报)
5. 机构库主流范式 = 库有唯一所有者 + 成员只读自动挂载 + 写权限收敛 admin; RAGFlow 至今未实现跨租户联合检索, 我们做到即是亮点。(企业KB简报)
6. LangGraph 图内流式唯一正解是节点内 `get_stream_writer()` + `astream(stream_mode=["custom","updates"], subgraphs=True)`, 不用 astream_events/messages 模式。(技术简报)
7. 引用定位本期用文本抽屉(chunk 上下文接口+heading_path+页码+mark 高亮), 不引入 pdfjs, 规避演示风险。(技术简报)
8. 语义缓存两级: Redis 精确 + Qdrant 独立 answer_cache collection, 零新依赖; 预算强制在网关 sink 前置检查(唯一 choke point)。(技术简报)
9. 合成学生闭环实验用 IRT 作答/BKT 估计的异构双模型, 是学界标准范式(arXiv 2605.21613), 可正面回应循环论证质疑。(M5简报)
10. Locust SSE 压测必须手动计时+自定义事件上报 TTFT, 原生统计只到响应头。(M5简报)

## 2. 技术决策表

| 项 | 唯一决定 |
|---|---|
| 图内流式 | 节点内 `get_stream_writer()` 逐 token 写出, token 事件带 attempt 序号; API 层 astream 多模式+subgraphs 消费, SSE 侧 20-40ms 微批 |
| 引用定位 | `GET /chunks/{id}/context?window=1` + shadcn Sheet 抽屉; quote indexOf 精确高亮, 失败降级整块高亮; 引用生成阶段即锚定 chunk_id |
| 追问 chips | 问答末节点同次调用结构化输出 3 追问 + 学情驱动"考考我"chip 直连出题子图 |
| 语义缓存 | L1 Redis SHA256 精确(TTL 24h) + L2 Qdrant answer_cache(阈值 0.95+数字/否定词正则复核); kb_version 进 filter 天然失效; 命中走同协议伪流式 SSE 并写 usage_log(cost=0) |
| 预算强制 | 网关入口预检, Redis 天粒度微元计数器(Asia/Shanghai, TTL 48h); <80% 正常 / 80-100% cheap 档 / ≥100% 429+Retry-After; replay/stub 零成本不计数 |
| 机构库模型 | `owner_type` 判别列 + tenant_id 泛化为 owner_id, 不加 org_id 列; 三 GUC RLS; 入会即挂载无 mount 表 |
| 联合检索 | chat 入参 `kb_ids: list[UUID]`(不做 scope=all); 单次查询 Qdrant MatchAny 双 filter; PG 侧先校验可见性, 不可见 403 |
| 实验统计 | 配对设计 10 seeds + Wilcoxon 符号秩 + Cohen's d + 95% CI; 主指标唯一=毕业题量 |
| 压测 | HttpUser(非 FastHttpUser), stub 100 并发阶梯 + live 10 并发恒定 |

## 3. 执行计划

### Wave 1 — P0 (三包并行, 三子智能体)

**包 A: 流式+引用+chips(前端主线)**
- 任务: ① generate 节点接 writer, replay 模式按录制分片回放 token; ② API SSE 端点兼容二/三元组解析; ③ 前端 token 渲染+attempt 清空重排; ④ chunk context 接口+迁移补 char offset/页码; ⑤ 引用角标+悬停浮层+点击抽屉高亮; ⑥ 末节点结构化追问+chips 渲染+"考考我"chip。
- 验收: 真流式 TTFT<3s 且自省环二次生成不出现拼接答案; 点击任意引用抽屉打开显示 heading_path+页码+高亮命中段; chips 点击即发且"考考我"进入出题流程; replay 模式流式路径测试通过。

**包 B: 缓存+预算(网关主线)**
- 任务: ① answer_cache collection+lookup/写入(仅自省环通过的最终答案, 排除个性化上下文); ② 伪流式回放复用同 SSE 事件类型; ③ ARQ 定时清 7 天前缓存点; ④ 网关 precheck/settle+cheap 降档链(含 max_tokens 硬上限); ⑤ 命中与节省写入成本面板。
- 验收: 同问二问命中且前端渲染无差异; "牛顿第一/第三定律"不互相命中; 超预算 SSE 收到 event:error 且 429 带 Retry-After; stub/replay 跑全量测试计数器为 0; 成本面板显示"缓存节省 X 元"。

**包 C: 辅导+报告前端页**
- 任务: ① 辅导子图提示层级状态机(引导→小提示→大提示→相似例题); ② 前端"要个提示"按钮+BKT 上涨动画; ③ 会话复盘卡(覆盖 KC/错因/掌握度曲线/FSRS 下次复习时间); ④ 学情报告页接现有 API; ⑤ 检索命中测试页(四阶段分数与排名变化)。
- 验收: 连点提示四级不出答案; 答对掌握度动画触发; 复盘卡四要素齐全; 管理端输入 query 可见 rerank 改变排序的实例。

### Wave 2 — M5 四件套(参数定案)

1. **合成学生**(2 天): 实验 A 静态 θ~U(0.1,0.9), N=30×K=8, T=40, `P=c+(1-c)·sigmoid(1.7(logit(θ)-b))`, c 按题型{0.25,0.5,0.06,0.05}; 实验 B 动态 λ=0.15, θ_max=0.95, 阈值 0.8 毕业, 上限 200 题, 三臂(自适应/随机/难度阶梯); 10 seeds 配对+Wilcoxon+敏感性(BKT 参数±50%, 阈值 0.8/0.9/0.95, λ 三档); 估计器固定 P(L0)=0.3,P(T)=0.15,P(G)=0.2,P(S)=0.1 不共享。验收: stub 模式分钟级跑完, 产出 4 图(收敛曲线/毕业生存曲线/校准散点/三臂对照)+看板页, 主指标 p<0.05。
2. **黄金集 v2**(1.5 天): 60 题=保留 15+新增 45 覆盖 10 类难例; 2 篇 30% 术语重叠干扰文档; chunk 级 hit@1/3/5+MRR+nDCG@5+引用精确率+拒答正确率; LLM-judge 走 record 固化。验收: 分桶矩阵报表生成, 无答案题 6 道拒答判定自动化。
3. **压测**(1 天): 权重 70/15/10/5; stub 档 TTFE p95<500ms 错误率<1%; live 档 TTFT p95<3s 断流率 0; 缓存开/关各一轮报命中率。
4. **答辩材料**(1.5 天): PPT 10 页按 VLSE 主线; 3 分钟 demo 剧本全程 replay 兜底预录; Top10 问答背熟。验收: 断网条件下 demo 完整跑通。

### Wave 3 — P1 机构库+学习指南+URL 导入

- 数据模型定案: orgs(invite_code 8 位码, 512MB 配额计数列)+org_members(admin/member)+kb.owner_type+documents.uploaded_by; 迁移零回填; Qdrant payload 零改写。
- 按 KB 简报 P0-1→P0-6 六阶段执行(3‖4 可并行), 各阶段验收照单: 关键为 member 写 org 库被 service+RLS 双拦 403、Qdrant 点位 tenant_id=org_id、非成员传 org kb_id 403、replay 结果确定。
- 学习指南: 摄取完成回调触发 ARQ 生成摘要+KC chips, Studio 式"学习指南/测验"按钮复用出题子图。验收: 摄取完成后指南自动出现, 点 KC chip 即提问。
- URL 导入: 粘贴 URL→readability 抽正文→现有两阶段摄取, 带分段预览可禁用单分段。验收: 公众号文章 URL 入库后可问答带溯源。不做浏览器插件。

## 4. 风险与预答 Top5

1. **合成学生循环论证**: 作答 IRT(连续能力+难度+题型异质猜测)/估计 BKT(二元 HMM), 结构异构参数不共享, 同 arXiv 2605.21613 范式, 另有估计器参数 ±50% 敏感性。
2. **与套壳 ChatGPT 区别**: 差在可验证状态引擎——BKT×FSRS×先修 DAG+证据链+record/replay 可复现评测, prompt 包装做不出。
3. **BKT 过时**: 逐 KC 可解释可审计、冷启动零数据, 与"可验证"主线自洽; estimator 接口可替换 DKT。
4. **幻觉治理**: 引用生成期锚定 chunk_id+点击回原文+黄金集无答案题拒答率+十类分桶暴露边界——"精确知道边界"即完成度证据。
5. **真人验证/并发**: 诚实定位算法层验证, 真人试用在路线图不夸大; 全 async+ARQ, stub 100 并发数字在手, 瓶颈在上游 LLM, 网关可多供应商分流。
