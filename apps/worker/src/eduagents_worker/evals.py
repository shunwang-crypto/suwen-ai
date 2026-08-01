"""F9-A1: 页面上一键触发检索评估的后台任务。

**唯一决定 — 用子进程调 eval/run_retrieval_eval.py, 不 import 它的 run_eval。** 理由:

1. 权威性单一。CLI 产出的 markdown 就是答辩引用的那份文件; 走同一条命令意味着看板
   里的数字与明确环境生成的评估报告逐字同源, 没有"第二套算法"。
2. eval/ 不是包 (`run_retrieval_eval.py` 顶层 `from metrics import ...` 依赖脚本目录
   进 sys.path)。直接 import 需要往 worker 进程里塞 sys.path hack, 且会把 BGE-M3 +
   reranker 常驻进 worker 内存, 之后每个文档摄取任务都背着它。
3. 隔离。评估栈崩溃/OOM 只炸子进程, ARQ worker 本身不受影响, 失败还能拿到 stderr
   原文写进结果里给面板看。

代价是多一次模型加载 (几十秒), 对"点一下重跑一次评估"这个频次完全可接受。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from eduagents_core.logging import get_logger

log = get_logger("eval_task")

_TIMEOUT_SECONDS = 1800


async def run_retrieval_eval_task(
    ctx: dict,
    kb_id: str,
    tenant_id: str,
    golden: str = "eval/golden_set/v2.jsonl",
    no_sparse: bool = False,
    no_rerank: bool = False,
    label: str = "",
) -> dict:
    """跑一次检索评估 -> 写报告文件 -> 解析入 eval_runs。返回 {eval_run_id, ...}。"""
    from eduagents_api.evals_ingest import build_record, persist_record, repo_root

    root = repo_root()
    stem = Path(golden).stem
    suffix = "".join(s for s, on in (("_no-sparse", no_sparse), ("_no-rerank", no_rerank)) if on)
    output = root / "docs" / "evidence" / "m5" / f"retrieval_eval_{stem}{suffix}_latest.md"
    output.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "eval/run_retrieval_eval.py",
        "--golden", golden,
        "--kb-id", kb_id,
        "--tenant-id", tenant_id,
        "--output", str(output),
    ]
    if no_sparse:
        cmd.append("--no-sparse")
    if no_rerank:
        cmd.append("--no-rerank")

    log.info("eval_subprocess_start", kb_id=kb_id, golden=golden)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.error("eval_subprocess_timeout", kb_id=kb_id)
        return {"ok": False, "error": f"评估超时 (>{_TIMEOUT_SECONDS}s)", "eval_run_id": None}

    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace")[-2000:]
        log.error("eval_subprocess_failed", returncode=proc.returncode, stderr=tail)
        return {"ok": False, "error": tail or f"退出码 {proc.returncode}", "eval_run_id": None}

    record = build_record(output, "retrieval", root)
    if label:
        record["label"] = label
    run_id, created = await persist_record(record)
    log.info("eval_run_persisted", eval_run_id=run_id, created=created)
    return {
        "ok": True,
        "eval_run_id": run_id,
        "created": created,  # False = 结果与上一次逐字节相同 (幂等命中)
        "report": str(output.relative_to(root)),
        "stdout_tail": (stdout or b"").decode("utf-8", "replace")[-2000:],
        "error": None,
    }
