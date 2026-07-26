"""F9 评估看板的数据入口: 把 docs/evidence 下**已产出的真实评测产物**解析入 eval_runs。

为什么是"解析已有产物"而不是"看板自己再算一遍":
评测的权威版本是 CLI 跑出来的 markdown / results.json (答辩材料引用的就是这些文件)。
若看板另算一份, 两个数字就有分歧风险。因此看板只做**一份数据的两种呈现**, 平台内
展示的每个数字都能在 docs/evidence 下的原始文件里逐字找到。

幂等: 按文件**内容** sha256 去重 (不是路径也不是 mtime) —— 同一份报告换个路径重复
导入不会产生第二条; 重跑评估内容变了才是新的一条。哈希存 config_json.content_sha256。

解析器对缺字段一律宽容 (M2 的老报告没有 category 分桶/拒答段落), 缺什么就少什么,
绝不用默认值填充 —— 看板宁可少显示一块, 不可显示一个编造的数字。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .db import plain_session
from .models import EvalRun

# 仓库根: routers/../../../../../ -> apps/api/src/eduagents_api/ 上溯 5 层。
# 容器/异构部署用 EDUAGENTS_REPO_ROOT 覆盖。
_DEFAULT_ROOT = Path(__file__).resolve().parents[4]


def repo_root() -> Path:
    return Path(os.environ.get("EDUAGENTS_REPO_ROOT", str(_DEFAULT_ROOT)))


# 扫描目录 (相对仓库根)。顺序即导入顺序。
EVIDENCE_DIRS: tuple[str, ...] = ("docs/evidence/m2", "docs/evidence/m5")


# --------------------------------------------------------------------------
# markdown 基础设施
# --------------------------------------------------------------------------


def _sections(text: str) -> dict[str, list[str]]:
    """按 `## ` 切段, 返回 {标题: 该段的行}。同名标题后者覆盖前者 (报告里不会重名)。"""
    out: dict[str, list[str]] = {}
    current = ""
    out[current] = []
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            out[current] = []
        else:
            out[current].append(line)
    return out


def _table(lines: Iterable[str]) -> list[list[str]]:
    """抽 markdown 表格的数据行 (跳过表头与 --- 分隔行)。"""
    rows: list[list[str]] = []
    for raw in lines:
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= {"-", ":"} and c for c in cells):
            continue  # 分隔行
        rows.append(cells)
    return rows[1:] if rows else []  # 首行是表头


def _num(text: str) -> float | None:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return None


# 报告里的指标名 -> 看板统一键名 (看板只认统一键, 新旧报告因此可直接对比)
_METRIC_ALIASES = {
    "hit_rate@1": "hit@1",
    "hit_rate@3": "hit@3",
    "hit_rate@5": "hit@5",
    "hit@1": "hit@1",
    "hit@3": "hit@3",
    "hit@5": "hit@5",
    "mrr": "mrr",
    "ndcg@5": "ndcg@5",
    "ndcg@10": "ndcg@10",
}


# --------------------------------------------------------------------------
# 检索评估报告 (markdown)
# --------------------------------------------------------------------------


def parse_retrieval_report(text: str) -> dict[str, Any]:
    """解析 eval/run_retrieval_eval.py 产出的 markdown, 返回 metrics_json。"""
    secs = _sections(text)
    head = "\n".join(secs.get("", []))

    title = ""
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    metrics: dict[str, Any] = {"kind": "retrieval", "title": title}

    if m := re.search(r"生成时间:\s*(\S+)", head):
        metrics["generated_at"] = m.group(1)
    if m := re.search(r"黄金集:\s*`([^`]+)`", head):
        metrics["golden_set"] = m.group(1)
    if m := re.search(r"version:\s*([^,)\s]+)", head):
        metrics["golden_set_version"] = m.group(1)
    # "60 问 = 54 可答 + 6 无答案" / v1 只有 "15 问"
    if m := re.search(r"(\d+)\s*问\s*=\s*(\d+)\s*可答\s*\+\s*(\d+)\s*无答案", head):
        metrics["n_questions"] = int(m.group(1))
        metrics["n_answerable"] = int(m.group(2))
        metrics["n_refusal"] = int(m.group(3))
    elif m := re.search(r"(\d+)\s*问", head):
        metrics["n_questions"] = metrics["n_answerable"] = int(m.group(1))
        metrics["n_refusal"] = 0
    if m := re.search(r"kb_id:\s*`([^`]+)`", head):
        metrics["kb_id"] = m.group(1)

    # --- 配置摘要 ---
    cfg: dict[str, str] = {}
    for row in _table(secs.get("配置摘要", [])):
        if len(row) < 2:
            continue
        key, value = row[0], row[1]
        if key.startswith("sparse"):
            cfg["sparse"] = value
        elif key.startswith("rerank 精排"):
            cfg["rerank"] = value
        elif key.startswith("embedding_mode"):
            mode, _, model = value.partition("/")
            cfg["embedding_mode"], cfg["embedding_model"] = mode.strip(), model.strip()
        elif key.startswith("rerank_mode"):
            mode, _, model = value.partition("/")
            cfg["rerank_mode"], cfg["rerank_model"] = mode.strip(), model.strip()
        else:
            cfg[key] = value
    if cfg:
        metrics["report_config"] = cfg

    # --- 指标表 (标题可能带 "(仅可答题)" 后缀) ---
    metric_sec = next(
        (v for k, v in secs.items() if k.startswith("指标")), []
    )
    headline: dict[str, float] = {}
    for row in _table(metric_sec):
        if len(row) < 2:
            continue
        name = _METRIC_ALIASES.get(row[0].strip().lower())
        if name is not None and (value := _num(row[1])) is not None:
            headline[name] = value
        elif row[0].startswith("confident 比例") and (
            m := re.match(r"(\d+)\s*/\s*(\d+)", row[1])
        ):
            metrics["confident"] = {"n": int(m.group(1)), "total": int(m.group(2))}
    if headline:
        metrics["headline"] = headline

    # --- 按 category 分桶矩阵 ---
    bucket_sec = next((v for k, v in secs.items() if k.startswith("按 category")), [])
    buckets = []
    for row in _table(bucket_sec):
        if len(row) < 6:
            continue
        buckets.append(
            {
                "category": row[0],
                "n": int(_num(row[1]) or 0),
                "hit@1": _num(row[2]),
                "hit@3": _num(row[3]),
                "hit@5": _num(row[4]),
                "mrr": _num(row[5]),
            }
        )
    if buckets:
        metrics["by_category"] = buckets

    # --- 拒答正确率 ---
    refusal_sec = next((v for k, v in secs.items() if k.startswith("拒答正确率")), [])
    if refusal_sec:
        body = "\n".join(refusal_sec)
        refusal: dict[str, Any] = {}
        if m := re.search(r"拒答正确率:\s*(\d+)\s*/\s*(\d+)\s*=\s*([\d.]+)", body):
            refusal["correct"] = int(m.group(1))
            refusal["total"] = int(m.group(2))
            refusal["rate"] = float(m.group(3))
        rows = []
        for row in _table(refusal_sec):
            if len(row) < 6:
                continue
            rows.append(
                {
                    "qid": row[0],
                    "question": row[1],
                    "n_chunks": int(_num(row[2]) or 0),
                    "top1_score": _num(row[3]),
                    "top1_heading": row[4],
                    "ok": row[5].startswith("✓"),
                }
            )
        if rows:
            refusal["rows"] = rows
        if refusal:
            metrics["refusal"] = refusal

    return metrics


def _ablation_tag(cfg: dict[str, str]) -> str:
    """从配置摘要还原消融标签 —— F9-A2 的"参数调优→指标变化"靠它区分同一批报告。"""
    off = []
    if cfg.get("sparse") == "关":
        off.append("关稀疏")
    if cfg.get("rerank") == "关":
        off.append("关重排")
    return "+".join(off) if off else "完整链路"


def _corpus_version(golden_path: str) -> str:
    """从黄金集反查语料口径 (distinct relevant_doc), 读不到就留空 —— 不编。"""
    path = repo_root() / golden_path
    if not path.is_file():
        return ""
    docs: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            docs.add(str(json.loads(line).get("relevant_doc", "")).strip())
        except json.JSONDecodeError:
            continue
    docs.discard("")
    if not docs:
        return ""
    return f"{len(docs)} 文档: {', '.join(sorted(docs))}"[:64]


def _retrieval_summary(metrics: dict) -> list[dict]:
    headline = metrics.get("headline", {})
    out = [
        {"key": key, "value": headline[key], "display": f"{headline[key]:.4f}"}
        for key in ("hit@1", "hit@5", "mrr", "ndcg@10")
        if key in headline
    ]
    refusal = metrics.get("refusal") or {}
    if "rate" in refusal:
        out.append(
            {
                "key": "拒答正确率",
                "value": refusal["rate"],
                "display": f"{refusal['correct']}/{refusal['total']}",
            }
        )
    return out


def build_retrieval_record(text: str, source: str, label_hint: str = "") -> dict:
    """markdown -> 一条待入库的 eval_run (config/metrics/summary 全备好)。"""
    metrics = parse_retrieval_report(text)
    cfg = metrics.get("report_config", {})
    golden_version = metrics.get("golden_set_version", "")
    mode = cfg.get("embedding_mode", "")
    # 标签必须带 stub/live: 同一个黄金集的 stub 跑分与 live 跑分只差这一项,
    # 混在同一张历史表里而标签相同, 会读成"同一次跑分出了两个数"。
    label = label_hint or " · ".join(
        p
        for p in (
            metrics.get("title") or "检索评估",
            golden_version,
            f"{mode} 嵌入" if mode else "",
            _ablation_tag(cfg),
        )
        if p
    )
    data_basis = (
        f"嵌入 {cfg.get('embedding_mode', '?')} / 重排 {cfg.get('rerank_mode', '?')}"
        f" · {_ablation_tag(cfg)}"
    )
    return {
        "kind": "retrieval",
        "label": label,
        "source": source,
        "metrics": metrics,
        "corpus_version": _corpus_version(metrics.get("golden_set", "")),
        "golden_set_version": golden_version,
        "data_basis": data_basis,
        "generated_at": metrics.get("generated_at", ""),
        "summary": _retrieval_summary(metrics),
        "report_config": cfg,
    }


# --------------------------------------------------------------------------
# 闭环仿真实验 (results.json)
# --------------------------------------------------------------------------

_ARMS = ("adaptive", "random", "ladder")


def _mean_curve(curves: list[list[float]]) -> list[float]:
    """跨 seed 逐轮取均值 (最短曲线为准)。原始 10 条曲线太胖, 看板只要平均线。"""
    curves = [c for c in curves if c]
    if not curves:
        return []
    n = min(len(c) for c in curves)
    return [round(sum(c[i] for c in curves) / len(curves), 6) for i in range(n)]


def parse_simulation_results(raw: dict) -> dict[str, Any]:
    """results.json -> metrics_json。**剥掉逐学生大数组** (theta/p_final/all_questions
    各 2400/300 项, 原文件 ~390KB): 入库的是看板要画的东西, 原始明细留在 results.json,
    看板上给出源文件路径可回溯。"""
    metrics: dict[str, Any] = {
        "kind": "simulation",
        "generated_at": raw.get("generated_at", ""),
        "elapsed_seconds": raw.get("elapsed_seconds"),
        "seeds": raw.get("seeds", []),
        "config": raw.get("config", {}),
    }

    exp_a = {}
    for arm in _ARMS:
        node = (raw.get("experiment_a") or {}).get(arm)
        if not isinstance(node, dict):
            continue
        curve = _mean_curve(node.get("mae_curves") or [])
        exp_a[arm] = {
            "r2": node.get("r2"),
            "spearman": node.get("spearman"),
            "convergence_round": node.get("convergence_round"),
            "min_mae": node.get("min_mae"),
            "min_mae_round": node.get("min_mae_round"),
            "final_mae": curve[-1] if curve else None,
            "mae_curve_mean": curve,
        }
    if exp_a:
        metrics["experiment_a"] = exp_a

    raw_b = raw.get("experiment_b") or {}
    arms = {}
    for arm in _ARMS:
        node = (raw_b.get("arms") or {}).get(arm)
        if not isinstance(node, dict):
            continue
        arms[arm] = {
            "mean": node.get("mean"),
            "ci95": node.get("ci95"),
            "grad_rate": node.get("grad_rate"),
            "final_theta_mean": node.get("final_theta_mean"),
            "seed_mean_questions": node.get("seed_mean_questions", []),
        }
    if arms or raw_b.get("comparisons"):
        metrics["experiment_b"] = {
            "arms": arms,
            "comparisons": raw_b.get("comparisons", {}),
        }

    if raw.get("sensitivity"):
        metrics["sensitivity"] = raw["sensitivity"]
    return metrics


def _simulation_summary(metrics: dict) -> list[dict]:
    arms = (metrics.get("experiment_b") or {}).get("arms") or {}
    out = [
        {
            "key": f"{arm} 毕业题量",
            "value": arms[arm].get("mean"),
            "display": f"{arms[arm]['mean']:.1f} 题",
        }
        for arm in _ARMS
        if isinstance(arms.get(arm), dict) and arms[arm].get("mean") is not None
    ]
    cmp_ar = ((metrics.get("experiment_b") or {}).get("comparisons") or {}).get(
        "adaptive_vs_random"
    )
    if isinstance(cmp_ar, dict) and cmp_ar.get("p_value") is not None:
        out.append(
            {
                "key": "adaptive vs random p",
                "value": cmp_ar["p_value"],
                "display": f"p={cmp_ar['p_value']:.4f}",
            }
        )
    return out


def build_simulation_record(raw: dict, source: str, label_hint: str = "") -> dict:
    metrics = parse_simulation_results(raw)
    cfg = metrics.get("config", {})
    scale = (
        f"N={cfg.get('n_students', '?')}×K={cfg.get('n_kcs', '?')}"
        f"×{len(metrics.get('seeds', []))} seeds"
    )
    return {
        "kind": "simulation",
        "label": label_hint or f"合成学生闭环实验 · {scale}",
        "source": source,
        "metrics": metrics,
        "corpus_version": f"synthetic ({scale})"[:64],
        "golden_set_version": "",
        "data_basis": "纯数值仿真 (IRT 环境 + BKT 估计器), 零 LLM 调用",
        "generated_at": metrics.get("generated_at", ""),
        "summary": _simulation_summary(metrics),
        "report_config": cfg,
    }


# --------------------------------------------------------------------------
# 扫描 + 落库
# --------------------------------------------------------------------------


def _is_retrieval_report(path: Path) -> bool:
    return path.suffix == ".md" and path.stem.startswith("retrieval_eval")


def discover(root: Path | None = None) -> list[tuple[Path, str]]:
    """扫描 evidence 目录, 返回 [(绝对路径, kind)]。"""
    base = root or repo_root()
    found: list[tuple[Path, str]] = []
    for rel in EVIDENCE_DIRS:
        directory = base / rel
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if _is_retrieval_report(path):
                found.append((path, "retrieval"))
            elif path.name == "results.json":
                found.append((path, "simulation"))
    return found


def build_record(path: Path, kind: str, root: Path | None = None) -> dict:
    """读文件 -> 待入库记录 (含 content_sha256 与相对 source 路径)。"""
    base = root or repo_root()
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    try:
        source = str(path.relative_to(base))
    except ValueError:
        source = str(path)
    text = data.decode("utf-8")
    record = (
        build_retrieval_record(text, source)
        if kind == "retrieval"
        else build_simulation_record(json.loads(text), source)
    )
    record["content_sha256"] = digest
    return record


def to_config_json(record: dict) -> dict:
    return {
        "kind": record["kind"],
        "source": record["source"],
        "content_sha256": record["content_sha256"],
        "label": record["label"],
        "data_basis": record["data_basis"],
        "generated_at": record.get("generated_at", ""),
        "summary": record.get("summary", []),
        "report_config": record.get("report_config", {}),
        "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


async def persist_record(record: dict) -> tuple[str, bool]:
    """写一条 eval_run。返回 (id, created)。已存在同哈希则原样返回旧 id。

    eval_runs 不在 TENANT_TABLES (无 RLS, 平台级评测数据), 故走 plain_session。
    """
    digest = record["content_sha256"]
    async with plain_session() as session:
        existing = await session.scalar(
            select(EvalRun).where(EvalRun.config_json["content_sha256"].astext == digest)
        )
        if existing is not None:
            return str(existing.id), False
        row = EvalRun(
            config_json=to_config_json(record),
            metrics_json=record["metrics"],
            corpus_version=record.get("corpus_version", "")[:64],
            golden_set_version=record.get("golden_set_version", "")[:64],
        )
        session.add(row)
        await session.flush()
        return str(row.id), True


async def import_evidence(root: Path | None = None) -> dict:
    """扫描并入库全部评估产物 (幂等)。"""
    items: list[dict] = []
    imported = skipped = 0
    for path, kind in discover(root):
        try:
            record = build_record(path, kind, root)
            run_id, created = await persist_record(record)
        except Exception as exc:  # noqa: BLE001 — 单个坏文件不该让整次导入失败
            items.append(
                {"source": str(path), "kind": kind, "status": "error", "error": str(exc)}
            )
            continue
        imported += created
        skipped += not created
        items.append(
            {
                "source": record["source"],
                "kind": kind,
                "status": "imported" if created else "skipped",
                "id": run_id,
                "label": record["label"],
            }
        )
    return {"imported": imported, "skipped": skipped, "items": items}
