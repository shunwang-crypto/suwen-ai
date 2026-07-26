"""M2 检索评估 CLI: 逐问题跑 RetrievalService, 产出 markdown 评估报告。

用法 (cwd = 仓库根):
    python eval/run_retrieval_eval.py --golden eval/golden_set/v1.jsonl \
        --kb-id <kb_id> --tenant-id <tenant_id> \
        --output docs/evidence/m2/retrieval_eval.md [--no-sparse] [--no-rerank]

relevance 判定: chunk.heading_path 包含黄金集 relevant_headings 中任一子串
(规则与升级路线见 eval/golden_set/README.md)。--no-sparse / --no-rerank
用于 M2-D5 消融实验, 报告中的配置摘要会如实记录各开关状态。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
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


@dataclass
class QuestionOutcome:
    item: GoldenItem
    flags: list[bool]  # 检索结果按序是否相关
    top1_heading: str
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
            )
        )
    return items


def is_relevant(chunk: RetrievedChunk, item: GoldenItem) -> bool:
    """M2 结构定位规则: heading_path 含任一 relevant_headings 子串。"""
    return any(h in chunk.heading_path for h in item.relevant_headings)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def build_report(
    settings, args: argparse.Namespace, outcomes: list[QuestionOutcome], golden_version: str
) -> str:
    matrix = [o.flags for o in outcomes]
    hr5 = hit_rate_at_k(matrix, 5)
    mrr_v = mrr(matrix)
    ndcg10 = ndcg_at_k(matrix, 10)
    n_confident = sum(1 for o in outcomes if o.confident)

    lines: list[str] = []
    lines.append("# M2 检索评估报告")
    lines.append("")
    lines.append(f"- 生成时间: {datetime.now(UTC).isoformat(timespec='seconds')}")
    lines.append(f"- 黄金集: `{args.golden}` (version: {golden_version or '未标注'}, "
                 f"{len(outcomes)} 问)")
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
    lines.append(f"| confidence_threshold | {settings.retrieval_confidence_threshold} |")
    lines.append(f"| embedding_mode / model | {settings.embedding_mode} / "
                 f"{settings.embedding_model} |")
    lines.append(f"| rerank_mode / model | {settings.rerank_mode} / {settings.rerank_model} |")
    lines.append("")
    lines.append("## 指标")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| hit_rate@5 | {hr5:.4f} |")
    lines.append(f"| MRR | {mrr_v:.4f} |")
    lines.append(f"| nDCG@10 | {ndcg10:.4f} |")
    lines.append(f"| confident 比例 | {n_confident}/{len(outcomes)} |")
    lines.append("")
    lines.append("## 逐问题明细")
    lines.append("")
    lines.append("| qid | 问题 | 命中 | 首个相关名次 | top1 heading |")
    lines.append("| --- | --- | --- | --- | --- |")
    for o in outcomes:
        rank = o.first_relevant_rank
        lines.append(
            f"| {o.item.qid} | {_md_escape(o.item.question)} "
            f"| {'✓' if o.hit else '✗'} "
            f"| {rank if rank is not None else '-'} "
            f"| {_md_escape(o.top1_heading) or '-'} |"
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
            outcomes.append(
                QuestionOutcome(
                    item=item, flags=flags, top1_heading=top1, confident=result.confident
                )
            )
            mark = "✓" if any(flags) else "✗"
            print(f"[{mark}] {item.qid} {item.question}")
    finally:
        store.close()  # 显式关闭, 避免解释器退出期 qdrant 本地模式 __del__ 噪音

    golden_version = items[0].version if items else ""
    return build_report(settings, args, outcomes, golden_version)


def main() -> None:
    parser = argparse.ArgumentParser(description="M2 混合检索评估 (黄金集 -> markdown 报告)")
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
