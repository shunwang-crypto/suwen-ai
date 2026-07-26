# 选型对比论证 (§12.6) — 六组取舍成文

> 使用方式: 每组"结论一句话 + 三条理由 + 何时会翻案"。素材来源全部标注
> (提示词 §4.2 选型表 / decisions.md / 调研简报 / 消融真实数字), 不编造对比数据。
> 通用前提: `ModelGateway` / `VectorStore` / `DocumentParser` / `Clock` 四个 Protocol
> 先定义后实现 (§4.4 架构铁律), 因此下列每一组"翻案"都是实现替换而非架构手术。

## 1. Qdrant vs Milvus / pgvector

**结论**: 本量级选 Qdrant——原生混合检索 + 官方多租户方案 + 最小运维足迹。

理由:
1. **原生稀疏向量 + 服务端融合**: named vectors {dense, sparse} + Query API
   prefetch/RRF 一次调用完成混合检索 (§4.2; 实现
   `packages/core/src/eduagents_core/vectorstore/qdrant_store.py`)。pgvector 无原生
   sparse, 混合检索要自己在 SQL 层拼 RRF; Milvus 支持但代价见第 3 条。
2. **payload 分区是官方多租户方案**: 单 collection + `tenant_id` keyword 索引
   (`is_tenant=true` 物理聚簇), 与我们"全链路 tenant_id"的隔离设计同构 (§7.2);
   机构库演进只改 payload 语义, 向量零迁移 (decisions.md D14 实测零回填)。
3. **足迹匹配 8G 单机预算**: Qdrant 空载 <200MB (§4.2), 且提供纯本地嵌入模式供
   无容器开发 (decisions.md D11); Milvus 分布式形态自带 etcd/消息队列等组件簇,
   对单机演示与"15 分钟复现"是纯负担。pgvector 虽零新组件, 但把向量负载与
   OLTP 绑在同一 PG 上, 扩展性论证 (docs/scaling.md) 里两者的伸缩路径会互相锁死。

何时翻案: 向量规模到十亿级再评估 Milvus (§7 写死的演进路径); 或语料极小且运维
极简优先时可降级 pgvector——两者都因 `VectorStore` Protocol 收敛为实现替换
(`packages/core/src/eduagents_core/vectorstore/base.py`)。

## 2. BKT vs DKT (深度知识追踪)

**结论**: 选自实现 BKT (约 30 行纯函数) + py-fsrs, DKT 写进 roadmap。

理由:
1. **可解释可审计是 VLSE 主线的前提**: 画像页要能回答"这 23% 是哪几道题推出来的",
   每次更新一行 `mastery_log(old_p→new_p, evidence_id, weight)` (docs/vlse.md 审计
   流水支柱); DKT 的隐状态向量点不开给用户看, 与"可验证"定位冲突。
2. **冷启动零数据可用**: 我们没有百万级作答日志, DKT 无从训练; BKT 用文献先验
   四参数即可上线, 合成学生实验对参数 ±50% 做了敏感性分析, 10/10 变体方向不变
   (docs/evidence/m5/report.md 敏感性表)。
3. **纯函数可测试可复现**: `packages/core/src/eduagents_core/mastery/bkt.py` +
   `tests/unit/test_bkt.py`; 掌握度若不可复现, 闭环实验就没有因变量
   (docs/defense/QA_TOP10.md Q3 追问预案同口径)。

何时翻案: 真实作答日志积累到可训练量级、离线评估显示 DKT 类模型预测增益显著,
且能配套可解释性方案 (如注意力可视化) 时——estimator 是可替换接口, BKT 的
evidence 流水本身就是未来的训练集。

## 3. 自研模型网关 vs LiteLLM

**结论**: 自研几百行薄层 (openai SDK + tenacity + 自研熔断), 比 LiteLLM 少一个黑盒。

理由:
1. **record/replay/stub 四模式是刚需不是锦上添花**: 没有 record/replay, 黄金集
   评测数字每跑一次都变, "可复现评测"卖点不成立; replay 让现场断网可演示; stub
   让 CI 零 API 成本 (decisions.md D2, stub 强制带"非真实模型输出"横幅防冒充)。
   LiteLLM 无此机制 (docs/defense/QA_TOP10.md Q7)。
2. **单一 choke point 承载全部治理**: 三档路由/重试退避/熔断状态机/降级链/token
   计量三维归因/预算预检全部长在网关层 (`packages/core/src/eduagents_core/gateway/`),
   LLM 调用发生在图节点与 ARQ 任务里, HTTP 中间件拦不全 (brief-3 §4 论证)。
3. **完全可控可答辩**: 熔断阈值、降级顺序、计价口径逐行自己写的, 单测锁行为
   (`tests/unit/test_gateway.py` / `test_circuit.py`); 引入 LiteLLM 则这些行为
   由外部项目版本决定, 排障和答辩都多一层不可控。

何时翻案: Provider 数量增长到需要企业级 key 管理/代理集群时——届时 LiteLLM 可以
作为 `ModelGateway` Protocol 的一个 Provider 实现接进来, 治理层不动。
(已知代价也如实记: 自研熔断器是进程内状态, 多实例失效, 见
docs/PRODUCTION_GAP_REVIEW.md P1-15, 修复方向是状态外置 Redis。)

## 4. 自建记忆/画像 Store vs mem0

**结论**: 自建 (LangGraph PostgresStore 四命名空间 + 业务表), 不引入 mem0。

理由:
1. **schema 贴合教育域且带外键约束**: mastery_records 的 kc_id 外键引用归一化 KC
   表, 禁止裸字符串 (§5.8/§14); mem0 的通用记忆是自然语言条目, 无法承载
   "掌握度参与 FSRS 调度决策"这种可计算语义 (docs/defense/QA_TOP10.md Q1 追问)。
2. **确定性直写纪律与 LLM 抽取式记忆冲突**: 我们写死"测评/辅导结果确定性直写,
   不经 LLM" (§5.5), 只有对话洞察走 Reflection 异步抽取 (权重 0.3,
   `apps/worker/src/eduagents_worker/reflection.py`); mem0 全链路 LLM 抽取,
   记忆不可审计不可复现, 击穿 VLSE 三支柱。
3. **零新组件零缝隙**: 与 LangGraph/PG 同栈, RLS/租户/备份策略天然覆盖记忆数据;
   mem0 另起存储则隔离、删除权、备份三件套全要再做一遍 (docs/safety.md 三)。

何时翻案: 需要开放域情景记忆 (跨话题的生活化偏好、长程对话情节) 超出教育
schema 表达力时, 可在 Reflection 层引入专用记忆组件, 但掌握度主线不动。

## 5. 本地 BM25 (jieba+fastembed 系) vs BGE-M3 原生 sparse

**结论**: sparse 通道用本地 BM25 + jieba 预分词, 不用 M3 原生 lexical weights。

理由:
1. **API 现实约束**: SiliconFlow 的 embedding API 不返回 M3 的 lexical weights,
   原生 sparse 拿不到 (§4.2 主动答辩点)。
2. **自托管 M3 违反资源预算**: 为拿 sparse 权重而自托管 BGE-M3 违反 8G 单机内存
   预算 (§10 部署约束), 且引入 GPU/大内存依赖破坏"15 分钟复现"。
3. **本地 BM25 零成本零外部依赖**: jieba 分词写死 (中文 BM25 翻车点, §14 反模式
   清单), 离线可复现; 消融真实数字如实呈现——当前小语料上稀疏通道零增益
   (hit_rate@5 与 MRR 与关稀疏配置完全相同, docs/evidence/m2/ablation_summary.md),
   其预期价值在专有名词/公式编号/生僻术语查询, 待黄金集 v2 难例重测。
   开关保留 (`eval/run_retrieval_eval.py --no-sparse`), 用数据说话。

何时翻案: SiliconFlow (或替代供应商) 开放 lexical weights 返回, 或部署形态升级到
可自托管 M3 时, 在 v2 难例集上 A/B 两种 sparse 编码——通道本身有独立开关,
替换不动融合层 (RRF)。

## 6. 语义分块 (semantic chunking) 为何否决

**结论**: 分块采用"结构优先 + 父子分块 + 上下文前缀"三层 (§5.1), 否决按
embedding 相似度断点的语义分块。

理由:
1. **结构信息是溯源的载体**: heading_path 面包屑 ("第3章 函数 > 3.2 导数定义")
   直接进 citation 卡片与 chunk 预览器, 是引用定位和答辩演示的关键
   (`packages/core/src/eduagents_core/chunking/`); 语义分块以相似度曲线切割,
   丢结构边界, 页码/章节锚点退化。
2. **原子性硬约束与相似度切割冲突**: 表格禁止跨块切分、LaTeX 公式必须完整存活
   是写死的验收 (F2-A6, `$...$` 存活断言测试); 结构切分天然尊重 element 边界,
   语义分块需要额外规则打补丁, 复杂度倒挂。
3. **确定性与成本**: 结构切分是确定性纯函数, 可单测可复现 (`tests/unit/test_chunking.py`);
   语义分块依赖全量 embedding 且边界随模型/阈值漂移, 破坏黄金集与语料版本绑定
   冻结的评测纪律 (§10)。语义分块想要的"语义完整性"收益, 我们由第三层
   **上下文前缀** (cheap 档为每 chunk 生成位置/作用说明后再嵌入) 以可控成本补齐,
   两者收益高度重叠, 不重复付费。

何时翻案: 无结构纯文本语料 (如 URL 导入的长贴/转写稿) 占比显著升高, 且黄金集
v2 显示结构切分在跨段语义单元上的 recall 明显落后时——分块器在
`DocumentParser`→chunking 管线内是独立阶段, 可按 doc_type 路由不同分块策略。

---

附: 相关但不属于六组的既有决策索引——HyDE 默认关闭 (§5.2)、编排形态四子图而非
五子图+Supervisor (decisions.md D15)、stub 第四网关模式 (D2)、学习指南落
config_json 不建表 (D16①)、机构库 owner_type 判别列 (D14)。
