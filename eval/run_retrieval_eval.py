"""本地检索评估 CLI：逐问题运行 RetrievalService，产出 Markdown 报告。

用法 (cwd = 仓库根):
    python eval/run_retrieval_eval.py --golden eval/golden_set/v2.jsonl \
        --kb-id <kb_id> --tenant-id <tenant_id> \
        --output /tmp/eduagents-retrieval-eval.md [--no-sparse] [--no-rerank]

relevance 判定：
先匹配标注教材，再用 chunk hint 或章节标题判定命中。--corpus-dir corpus 会在临时
Qdrant 集合中从三本 Markdown 教材重建语料，不访问用户知识库。
无答案题不计入检索命中率。仅在启用精排时按其置信阈值列出拒答代理指标。
端到端引用与拒答由教材问答工作流单独验证。

指标: hit@1 / hit@3 / hit@5 / MRR / nDCG@5 / nDCG@10 + 按 category 分桶矩阵。
--no-sparse / --no-rerank 用于消融实验, 报告中的配置摘要会如实记录各开关状态。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
import uuid
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

if __package__:
    from .local_model import prepare_ollama
    from .metrics import hit_rate_at_k, mrr, ndcg_at_k
else:
    from local_model import prepare_ollama
    from metrics import hit_rate_at_k, mrr, ndcg_at_k


@dataclass
class GoldenItem:
    qid: str
    question: str
    relevant_doc: str
    relevant_headings: list[str]
    ground_truth: str
    version: str = ""
    # v2 字段；v1 缺失时使用默认值。
    category: str = ""
    expect_refusal: bool = False
    relevant_chunk_hints: list[str] = field(default_factory=list)


@dataclass
class QuestionOutcome:
    item: GoldenItem
    flags: list[bool]  # 检索结果按序是否相关
    top1_heading: str
    top1_score: float | None  # 仅精排分数，关闭精排或没有结果时为 None
    n_chunks: int
    confident: bool
    latency_ms: int = 0

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


def is_relevant(chunk: RetrievedChunk, item: GoldenItem, documents: dict[str, str]) -> bool:
    """chunk 级 hint 判定优先；无 hint 时使用 heading_path 子串规则。"""
    return source_matches(item, documents.get(chunk.doc_id), chunk.text, chunk.heading_path)


def source_matches(item, filename, text, heading):
    if filename != item.relevant_doc:
        return False
    if item.relevant_chunk_hints:
        return any(h in text for h in item.relevant_chunk_hints)
    return any(h in heading for h in item.relevant_headings)


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

    lines.append(f"每次检索平均用时 {sum(o.latency_ms for o in outcomes) / max(1, len(outcomes)):.0f} ms。")

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
    if refusals and settings.retrieval_enable_rerank:
        n_ok = sum(1 for o in refusals if refusal_correct(o, threshold))
        lines.append("## 拒答正确率 (无答案题)")
        lines.append("")
        lines.append(f"判定规则: 检索为空 **或** top1 分数 < 置信阈值 {threshold}"
                     f" (rerank_score) → 判为可拒答。"
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


async def index_corpus(directory, settings, store, embedder, sparse, tenant, kb):
    from eduagents_core.chunking.base import chunk_document
    from eduagents_core.parsing.markdown import MarkdownParser
    from eduagents_core.vectorstore.base import VectorPoint

    documents = {}
    for path in sorted(directory.glob("*.md")):
        doc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, path.name))
        documents[doc_id] = path.name
        parents = chunk_document(MarkdownParser().parse(path), child_chars=settings.chunk_child_chars,
                                 parent_chars=settings.chunk_parent_chars, overlap_chars=settings.chunk_overlap_chars)
        for pi, parent in enumerate(parents):
            texts = [f"{child.heading_path}\n{child.text}" for child in parent.children]
            dense = await embedder.embed(texts)
            await store.upsert([VectorPoint(
                str(uuid.uuid5(uuid.UUID(doc_id), f"{pi}:{ci}")), vector, sparse.encode_document(text),
                {"tenant_id": tenant, "kb_id": kb, "doc_id": doc_id, "text": child.text,
                 "heading_path": child.heading_path, "page_start": child.page_start, "page_end": child.page_end},
            ) for ci, (child, text, vector) in enumerate(zip(parent.children, texts, dense, strict=True))])
    return documents


async def run_eval(args: argparse.Namespace) -> str:
    settings = get_settings()
    overrides: dict = {}
    if args.no_sparse:
        overrides["retrieval_enable_sparse"] = False
    if args.no_rerank:
        overrides["retrieval_enable_rerank"] = False
    if overrides:
        settings = settings.model_copy(update=overrides)

    with tempfile.TemporaryDirectory(prefix="eduagents-retrieval-") as temp:
        if args.corpus_dir:
            settings = settings.model_copy(update={"qdrant_local_path": temp})
            args.tenant_id, args.kb_id = "offline", "corpus"
        store = QdrantStore(settings)
        try:
            return await measure(args, settings, store)
        finally:
            store.close()


async def run_grounded_qa(args, settings, service, documents, items):
    from eduagents_core.education.context import EducationDeps
    from eduagents_core.education.prompt_evolution import EvaluationGateway
    from eduagents_core.gateway import OpenAIGateway
    from eduagents_core.gateway.base import GatewayError
    from eduagents_core.harness.task import AgentTask

    configured, runtime = await prepare_ollama(settings, args.qa_ollama_model)
    gateway = EvaluationGateway(OpenAIGateway(configured), max_calls=len(items) * 32)
    deps = EducationDeps(configured, gateway, retrieval=service)
    model = runtime["model"]
    lines = [
        "", "## 原生教材问答", "",
        f"模型 `{model}`，每题一个独立会话，检索真实临时索引。标注答案只用于评测。",
        "", "| 题目 | 预期 | 工作流结果 | 模型调用 |", "|---|---|---|---:|",
    ]
    positives = negatives = 0
    for item in items:
        start = gateway.calls
        run_id = uuid.uuid4().hex
        try:
            record = await AgentTask("qa", deps).run(run_id, {
                "tenant_id": args.tenant_id, "kb_id": args.kb_id, "user_id": "offline-evaluator",
                "query": item.question, "request_id": run_id, "messages": [{"role": "user", "content": item.question}],
            })
            state = record.state
            selected = set(state.get("source_ids") or [])
            cited = any(source["chunk_id"] in selected and source_matches(
                item, documents.get(source["doc_id"]), source["text"], source["heading_path"]
            ) for source in state.get("evidences") or [])
            reason = state.get("refusal_reason")
            if record.status != "completed":
                outcome = "任务未完成"
            elif reason == "verification_unavailable":
                outcome = "核验服务未完成"
            elif state.get("refused"):
                outcome = "拒答"
                negatives += int(item.expect_refusal)
            elif cited:
                outcome = "通过核验并引用标注来源"
                positives += int(not item.expect_refusal)
            else:
                outcome = "已回答，未引用标注来源"
        except GatewayError as exc:
            outcome = exc.code
        lines.append(f"| {item.qid} | {'拒答' if item.expect_refusal else '可答'} | {outcome} | {gateway.calls-start} |")
        print(f"[QA] {item.qid}: {outcome}", flush=True)
        gateway.clear_trace()
    n = sum(item.expect_refusal for item in items)
    lines.extend(["", f"可答题中通过核验并引用标注来源 {positives}/{len(items)-n}，缺资料题中明确拒答 {negatives}/{n}。",
                  "该指标检查引用命中和核验流程，答案的教学正确性需要另行评分。"])
    return "\n".join(lines)


async def measure(args, settings, store):
    embedder = create_embedder(settings)
    sparse = Bm25SparseEncoder()
    reranker = create_reranker(settings)
    await store.ensure_ready()
    service = RetrievalService(settings, embedder, sparse, store, reranker)

    if args.corpus_dir:
        documents = await index_corpus(args.corpus_dir, settings, store, embedder, sparse, args.tenant_id, args.kb_id)
    else:
        from eduagents_api.db import tenant_session
        from eduagents_api.models import Document
        from sqlalchemy import select

        async with tenant_session(args.tenant_id) as session:
            rows = await session.execute(select(Document.id, Document.filename).where(
                Document.kb_id == args.kb_id, Document.tenant_id == args.tenant_id,
            ))
            documents = {str(doc_id): name for doc_id, name in rows}
    items = load_golden(Path(args.golden))
    missing = {item.relevant_doc for item in items if not item.expect_refusal} - set(documents.values())
    if missing:
        raise ValueError(f"知识库缺少评测教材: {sorted(missing)}")
    outcomes: list[QuestionOutcome] = []
    for item in items:
        start = time.monotonic()
        result = await service.retrieve(args.tenant_id, args.kb_id, item.question)
        flags = [is_relevant(chunk, item, documents) for chunk in result.chunks]
        top1 = result.chunks[0].heading_path if result.chunks else ""
        top1_score: float | None = None
        if result.chunks:
            c0 = result.chunks[0]
            top1_score = c0.rerank_score
        outcome = QuestionOutcome(
            item=item,
            flags=flags,
            top1_heading=top1,
            top1_score=top1_score,
            n_chunks=len(result.chunks),
            confident=result.confident,
            latency_ms=round((time.monotonic() - start) * 1000),
        )
        outcomes.append(outcome)
        if item.expect_refusal:
            ok = refusal_correct(outcome, settings.retrieval_confidence_threshold)
            mark = ("R+" if ok else "R-") if settings.retrieval_enable_rerank else "无校准拒答分数"
        else:
            mark = "✓" if any(flags) else "✗"
        print(f"[{mark}] {item.qid} {item.question}")

    golden_version = items[0].version if items else ""
    report = build_report(settings, args, outcomes, golden_version)
    if args.qa_ollama_model:
        chosen = set(args.qa_case_id or [item.qid for item in items])
        if chosen - {item.qid for item in items}:
            raise ValueError("unknown QA case ID")
        report += await run_grounded_qa(args, settings, service, documents, [item for item in items if item.qid in chosen])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="混合检索评估 (黄金集 -> markdown 报告)")
    parser.add_argument("--golden", default="eval/golden_set/v1.jsonl", help="黄金集 JSONL 路径")
    parser.add_argument("--corpus-dir", type=Path, help="在临时索引评测此目录中的教材，无需应用数据库")
    parser.add_argument("--kb-id", help="知识库 id")
    parser.add_argument("--tenant-id", help="租户 id")
    parser.add_argument(
        "--output", default="/tmp/eduagents-evidence/retrieval_eval.md", help="markdown 报告输出路径"
    )
    parser.add_argument("--no-sparse", action="store_true", help="关闭 sparse 通道 (消融)")
    parser.add_argument("--no-rerank", action="store_true", help="关闭 rerank 精排 (消融)")
    parser.add_argument("--qa-ollama-model", help="在临时语料上运行本机生成模型，验证端到端引用与拒答")
    parser.add_argument("--qa-case-id", action="append", help="选择端到端问答案例，可重复")
    args = parser.parse_args()
    if args.qa_ollama_model and not args.corpus_dir:
        parser.error("--qa-ollama-model 需要 --corpus-dir")
    if args.qa_case_id and not args.qa_ollama_model:
        parser.error("--qa-case-id 需要 --qa-ollama-model")
    if not args.corpus_dir and not (args.kb_id and args.tenant_id):
        parser.error("提供 --corpus-dir 或 --kb-id 与 --tenant-id")

    report = asyncio.run(run_eval(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"\n报告已写入: {output}")


if __name__ == "__main__":
    main()
