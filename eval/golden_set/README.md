# 检索黄金集 (Golden Set) 格式说明

## 文件格式

JSONL (UTF-8, 每行一个 JSON 对象), 文件名带版本号, 如 `v1.jsonl`。

每行字段:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `qid` | str | 问题唯一 id, 如 `q01` |
| `question` | str | 用户问法 (评估时原样送入检索服务) |
| `relevant_doc` | str | 相关文档的文件名, 如 `calculus.md` |
| `relevant_headings` | list[str] | 相关标题片段列表; 命中 chunk 的 `heading_path` 须包含其中之一 |
| `ground_truth` | str | 依据语料可回答的简短标准答案 (供后续答案质量评估) |
| `version` | str | 黄金集版本, 与语料版本绑定, 如 `v1-2026-07-26` |

示例行:

```json
{"qid": "q10", "question": "泰勒公式的拉格朗日余项是什么形式?", "relevant_doc": "calculus.md", "relevant_headings": ["拉格朗日余项"], "ground_truth": "R_n(x) = f^(n+1)(ξ)/(n+1)! · (x-x0)^(n+1), ξ 介于 x0 与 x 之间。", "version": "v1-2026-07-26"}
```

## relevance 判定规则 (M2)

一个检索结果 chunk 判为相关, 当且仅当:

```
any(h in chunk.heading_path for h in relevant_headings)
```

即 `chunk.heading_path` 包含 `relevant_headings` 中任一条目作为子串
(如 heading_path `"高等数学讲义 > 第四章 泰勒公式 > 4.2 拉格朗日余项"`
包含 `"拉格朗日余项"`)。

M2 采用**结构定位** (标题路径子串匹配) 是因为 chunk 边界会随切分参数变化,
标题结构则与语料版本绑定、稳定可复现; M5 将升级为 **chunk_id 级**标注
(黄金集直接冻结相关 chunk_id 列表), 消除同名标题/粗粒度匹配带来的误差。

## 版本与冻结约定

- 黄金集与语料版本绑定 (`version` 字段一致, 当前 `v1-2026-07-26`),
  评估结果入 `eval_runs` 时同时记录 `corpus_version` 与 `golden_set_version`。
- 黄金集冻结后**禁止为达标而增删改**; 任何变更须升版本号并记录 changelog。
