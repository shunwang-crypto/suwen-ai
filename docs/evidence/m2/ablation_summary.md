# M2 检索消融摘要（live 口径 · 2026-07-26）

配置：BGE-M3 稠密（SiliconFlow）+ jieba/BM25 稀疏 + RRF + bge-reranker-v2-m3。
语料 corpus/calculus.md（v1-2026-07-26，13814 字符），黄金集 15 问（heading 子串判相关）。

| 配置 | hit_rate@5 | MRR | nDCG@10 | 报告 |
|---|---|---|---|---|
| 完整（dense+sparse+RRF+rerank） | 1.0000 | 0.9222 | 0.9269 | retrieval_eval_live_full.md |
| 关稀疏（--no-sparse） | 1.0000 | 0.9222 | 0.9269 | retrieval_eval_live_no-sparse.md |
| 关重排（--no-rerank） | 1.0000 | **1.0000** | **0.9843** | retrieval_eval_live_no-rerank.md |

（对照：stub 哈希嵌入口径 hit_rate@5=0.867 —— 仅管线验证，见 decisions.md D12）

## 如实解读（M2-D5：真实差异无论方向）

1. **M2-D3 达标**：live 口径 hit_rate@5 = 1.0 ≥ 0.8。
2. **指标近饱和的原因要诚实**：单文档小语料 + 15 问 + heading 级宽松判相关 → dense 单路已 1.0 命中。
   该黄金集能证明管线正确性，**不足以区分策略优劣**。M5 消融结论必须在更大语料
   （多文档、跨页、干扰章节）+ 更难黄金集上重测。
3. **重排为何反而降 MRR**：dense 排序已完美时，reranker 对语义相近段落（如"泰勒公式定义"
   vs"拉格朗日余项"）的重排引入扰动；且本黄金集相关性按结构（heading）判定，与 reranker 的
   纯文本相关性口径存在偏差。处置：保留开关（默认开），大语料重测后再定默认值——这本身
   是"每个策略可开关、用数据说话"设计的价值演示。
4. **稀疏通道本轮零增益**：中文数学语料上 dense 已饱和；稀疏通道的预期价值在专有名词/
   公式编号/生僻术语查询（M5 黄金集需补充此类难例再评）。
