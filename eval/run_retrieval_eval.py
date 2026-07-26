"""检索评估 CLI (M2 起, M5 升级): 逐问题跑 RetrievalService, 产出 markdown 评估报告。

用法 (cwd = 仓库根):
    python eval/run_retrieval_eval.py --golden eval/golden_set/v2.jsonl \
        --kb-id <kb_id> --tenant-id <tenant_id> \
        --output docs/evidence/m5/retrieval_eval_v2.md [--no-sparse] [--no-rerank]

relevance 判定 (M5, 向后兼容 v1):
1. 若该题带 relevant_chunk_hints (chunk 级标注): chunk.text 含任一 hint 子串即相关,
   **优先于** heading 判定;
2. 否则回落 M2 结构定位规则: chunk.heading_path 含 relevant_headings 任一子串。

拒答题 (expect_refusal=true, relevant_headings=[]): 语料中确实无答案, 不参与
检索命中指标; 单独统计"拒答正确率" —— 判定规则: 检索结果为空, 或 top1 分数
(rerank_score 优先, 无则融合 score) < retrieval_confidence_threshold (低置信 →
下游 qa 子图 grade 环节应走 refuse 路径)。这是检索层代理指标, 端到端拒答由
qa 子图决定。

指标: hit@1 / hit@3 / hit@5 / MRR / nDCG@5 / nDCG@10 + 按 category 分桶矩阵。
--no-sparse / --no-rerank 用于消融实验, 报告中的配置摘要会如实记录各开关状态。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from eduagents_core.config import get_settings
from eduagents_core.embedding.providers import create_embedder
from eduagents_core.embedding.rerankers import create_reranker
from eduagents_core.embedding.sparse import Bm25SparseEncoder
from eduagents_core.retrieval.base import RetrievedChunk
from eduagents_core.retrieval.service import RetrievalService
from eduagents_core.vectorstore.qdrant_store import QdrantStore
from metrics import hit_rate_at_k, mrr, ndcg_at_k


@dataclass
class GoldenItem:
    qid: str
    question: str
    relevant_doc: str
    relevant_headings: list[str]
    ground_truth: str
    version: str = ""
    # --- v2 新增字段 (v1 无这些字段 -> 缺省值即向后兼容) ---
    category: str = ""
    expect_refusal: bool = False
    relevant_chunk_hints: list[str] = field(default_factory=list)


@dataclass
class QuestionOutcome:
    item: GoldenItem
    flags: list[bool]  # 检索结果按序是否相关
    top1_heading: str
    top1_score: float | None  # rerank_score 优先, 无则融合 score; 无结果为 None
    n_chunks: int
    confident: bool

    @property
    def hit(self) -> bool:
        return any(self.flags)

    @property
    def first_relevant_rank(self) -> int | None:
        for rank, flag in enumerate(self.flags, start=1):
            if flag:
                return rank
        return None


def load_golden(path: Path) -> list[GoldenItem]:
    items: list[GoldenItem] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        items.append(
            GoldenItem(
                qid=row["qid"],
                question=row["question"],
                relevant_doc=row["relevant_doc"],
                relevant_headings=list(row["relevant_headings"]),
                ground_truth=row["ground_truth"],
                version=row.get("version", ""),
                category=row.get("category", ""),
                expect_refusal=bool(row.get("expect_refusal", False)),
                relevant_chunk_hints=list(row.get("relevant_chunk_hints", [])),
            )
        )
    return items


def is_relevant(chunk: RetrievedChunk, item: GoldenItem) -> bool:
    """chunk 级 hint 判定优先 (M5); 无 hint 回落 M2 heading_path 子串规则。"""
    if item.relevant_chunk_hints:
        return any(h in chunk.text for h in item.relevant_chunk_hints)
    return any(h in chunk.heading_path for h in item.relevant_headings)


def refusal_correct(outcome: QuestionOutcome, threshold: float) -> bool:
    """拒答题判定: 检索为空, 或 top1 分数低于置信阈值 (低置信 -> 应拒答)。"""
    if outcome.n_chunks == 0:
        return True
    return outcome.top1_score is None or outcome.top1_score < threshold


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _fmt(v: float) -> str:
    return f"{v:.4f}"


def build_report(
    settings, args: argparse.Namespace, outcomes: list[QuestionOutcome], golden_version: str
) -> str:
    answerable = [o for o in outcomes if not o.item.expect_refusal]
    refusals = [o for o in outcomes if o.item.expect_refusal]
    threshold = settings.retrieval_confidence_threshold

    matrix = [o.flags for o in answerable]
    n_confident = sum(1 for o in outcomes if o.confident)

    lines: list[str] = []
    lines.append("# 检索评估报告")
    lines.append("")
    lines.append(f"- 生成时间: {datetime.now(UTC).isoformat(timespec='seconds')}")
    lines.append(f"- 黄金集: `{args.golden}` (version: {golden_version or '未标注'}, "
                 f"{len(outcomes)} 问 = {len(answerable)} 可答 + {len(refusals)} 无答案)")
    lines.append(f"- tenant_id: `{args.tenant_id}`  kb_id: `{args.kb_id}`")
    lines.append("")
    lines.append("## 配置摘要")
    lines.append("")
    lines.append("| 配置项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| sparse 通道 (retrieval_enable_sparse) | "
                 f"{'开' if settings.retrieval_enable_sparse else '关'} |")
    lines.append(f"| rerank 精排 (retrieval_enable_rerank) | "
                 f"{'开' if settings.retrieval_enable_rerank else '关'} |")
    lines.append(f"| candidate_k | {settings.retrieval_candidate_k} |")
    lines.append(f"| final_k | {settings.retrieval_final_k} |")
    lines.append(f"| rrf_k | {settings.rrf_k} |")
    lines.append(f"| confidence_threshold | {threshold} |")
    lines.append(f"| embedding_mode / model | {settings.embedding_mode} / "
                 f"{settings.embedding_model} |")
    lines.append(f"| rerank_mode / model | {settings.rerank_mode} / {settings.rerank_model} |")
    lines.append("")
    lines.append("## 指标 (仅可答题)")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| hit_rate@1 | {_fmt(hit_rate_at_k(matrix, 1))} |")
    lines.append(f"| hit_rate@3 | {_fmt(hit_rate_at_k(matrix, 3))} |")
    lines.append(f"| hit_rate@5 | {_fmt(hit_rate_at_k(matrix, 5))} |")
    lines.append(f"| MRR | {_fmt(mrr(matrix))} |")
    lines.append(f"| nDCG@5 | {_fmt(ndcg_at_k(matrix, 5))} |")
    lines.append(f"| nDCG@10 | {_fmt(ndcg_at_k(matrix, 10))} |")
    lines.append(f"| confident 比例 (全部题) | {n_confident}/{len(outcomes)} |")
    lines.append("")

    # ---- 按 category 分桶矩阵 (可答题) ----
    buckets: dict[str, list[QuestionOutcome]] = defaultdict(list)
    for o in answerable:
        buckets[o.item.category or "(未标注)"].append(o)
    if buckets:
        lines.append("## 按 category 分桶矩阵 (可答题)")
        lines.append("")
        lines.append("| category | 题数 | hit@1 | hit@3 | hit@5 | MRR |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for cat in sorted(buckets):
            rows = [o.flags for o in buckets[cat]]
            lines.append(
                f"| {_md_escape(cat)} | {len(rows)} "
                f"| {_fmt(hit_rate_at_k(rows, 1))} "
                f"| {_fmt(hit_rate_at_k(rows, 3))} "
                f"| {_fmt(hit_rate_at_k(rows, 5))} "
                f"| {_fmt(mrr(rows))} |"
            )
        lines.append("")

    # ---- 拒答正确率 (无答案题) ----
    if refusals:
        n_ok = sum(1 for o in refusals if refusal_correct(o, threshold))
        lines.append("## 拒答正确率 (无答案题)")
        lines.append("")
        lines.append(f"判定规则: 检索为空 **或** top1 分数 < 置信阈值 {threshold}"
                     f" (rerank_score 优先, 无则融合分) → 判为可拒答。"
                     f"检索层代理指标; 端到端拒答由 qa 子图 grade→refuse 决定。")
        lines.append("")
        lines.append(f"**拒答正确率: {n_ok}/{len(refusals)} = {n_ok / len(refusals):.4f}**")
        lines.append("")
        lines.append("| qid | 问题 | 返回 chunk 数 | top1 分数 | top1 heading | 判定 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for o in refusals:
            score = "-" if o.top1_score is None else f"{o.top1_score:.4f}"
            verdict = "✓ 可拒答" if refusal_correct(o, threshold) else "✗ 误报高置信"
            lines.append(
                f"| {o.item.qid} | {_md_escape(o.item.question)} | {o.n_chunks} "
                f"| {score} | {_md_escape(o.top1_heading) or '-'} | {verdict} |"
            )
        lines.append("")

    # ---- 逐问题明细 ----
    lines.append("## 逐问题明细")
    lines.append("")
    lines.append("| qid | category | 问题 | 命中 | 首个相关名次 | top1 分数 | top1 heading |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for o in outcomes:
        if o.item.expect_refusal:
            mark, rank_s = "拒答题", "-"
        else:
            mark = "✓" if o.hit else "✗"
            rank = o.first_relevant_rank
            rank_s = str(rank) if rank is not None else "-"
        score = "-" if o.top1_score is None else f"{o.top1_score:.4f}"
        lines.append(
            f"| {o.item.qid} | {_md_escape(o.item.category) or '-'} "
            f"| {_md_escape(o.item.question)} | {mark} | {rank_s} "
            f"| {score} | {_md_escape(o.top1_heading) or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


async def run_eval(args: argparse.Namespace) -> str:
    settings = get_settings()
    overrides: dict = {}
    if args.no_sparse:
        overrides["retrieval_enable_sparse"] = False
    if args.no_rerank:
        overrides["retrieval_enable_rerank"] = False
    if overrides:
        settings = settings.model_copy(update=overrides)

    embedder = create_embedder(settings)
    sparse = Bm25SparseEncoder()
    store = QdrantStore(settings)
    reranker = create_reranker(settings)
    await store.ensure_ready()
    service = RetrievalService(settings, embedder, sparse, store, reranker)

    items = load_golden(Path(args.golden))
    outcomes: list[QuestionOutcome] = []
    try:
        for item in items:
            result = await service.retrieve(args.tenant_id, args.kb_id, item.question)
            flags = [is_relevant(chunk, item) for chunk in result.chunks]
            top1 = result.chunks[0].heading_path if result.chunks else ""
            top1_score: float | None = None
            if result.chunks:
                c0 = result.chunks[0]
                top1_score = c0.rerank_score if c0.rerank_score is not None else c0.score
            outcome = QuestionOutcome(
                item=item,
                flags=flags,
                top1_heading=top1,
                top1_score=top1_score,
                n_chunks=len(result.chunks),
                confident=result.confident,
            )
            outcomes.append(outcome)
            if item.expect_refusal:
                ok = refusal_correct(outcome, settings.retrieval_confidence_threshold)
                mark = "R+" if ok else "R-"
            else:
                mark = "✓" if any(flags) else "✗"
            print(f"[{mark}] {item.qid} {item.question}")
    finally:
        store.close()  # 显式关闭, 避免解释器退出期 qdrant 本地模式 __del__ 噪音

    golden_version = items[0].version if items else ""
    return build_report(settings, args, outcomes, golden_version)


def main() -> None:
    parser = argparse.ArgumentParser(description="混合检索评估 (黄金集 -> markdown 报告)")
    parser.add_argument("--golden", default="eval/golden_set/v1.jsonl", help="黄金集 JSONL 路径")
    parser.add_argument("--kb-id", required=True, help="知识库 id")
    parser.add_argument("--tenant-id", required=True, help="租户 id")
    parser.add_argument(
        "--output", default="docs/evidence/m2/retrieval_eval.md", help="markdown 报告输出路径"
    )
    parser.add_argument("--no-sparse", action="store_true", help="关闭 sparse 通道 (消融)")
    parser.add_argument("--no-rerank", action="store_true", help="关闭 rerank 精排 (消融)")
    args = parser.parse_args()

    report = asyncio.run(run_eval(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"\n报告已写入: {output}")


if __name__ == "__main__":
    main()
