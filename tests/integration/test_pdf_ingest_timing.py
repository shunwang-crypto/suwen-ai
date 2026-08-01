"""M2-D1 验收: 真实 >=20 页 PDF (corpus/calculus.pdf, pymupdf 真实排版) 走 API
上传 -> 直调 ingest_document -> status=ready 且总耗时 <60s (perf_counter 计时);
分块预览 API 返回的 chunk 带真实页码。计时输出写入临时目录，不提交历史 evidence。

口径说明: EMBEDDING_MODE=stub (tests/conftest.py), 计时覆盖 解析+分块+入库+
向量化(stub)+Qdrant upsert; live 嵌入口径的数字须在拿到 key 后另测 (D12)。"""

import importlib.util
import time
from datetime import datetime
from pathlib import Path

from .conftest import REPO_ROOT, requires_services
from .test_rag_e2e import _register_login

pytestmark = requires_services

PDF_PATH = REPO_ROOT / "corpus" / "calculus.pdf"
EVIDENCE_PATH = Path("/tmp/eduagents-evidence/pdf_ingest_timing.txt")


def _ensure_pdf() -> None:
    """corpus/calculus.pdf 不存在时调 scripts/gen_corpus_pdf.py 现场生成。"""
    if PDF_PATH.exists():
        return
    script = REPO_ROOT / "scripts" / "gen_corpus_pdf.py"
    spec = importlib.util.spec_from_file_location("gen_corpus_pdf", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.generate()
    assert PDF_PATH.exists()


async def _fetch_all_chunks(client, headers, kb_id: str, doc_id: str) -> list[dict]:
    chunks: list[dict] = []
    offset = 0
    while True:
        r = await client.get(
            f"/api/kb/{kb_id}/documents/{doc_id}/chunks",
            params={"offset": offset, "limit": 200},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        batch = r.json()
        chunks.extend(batch)
        if len(batch) < 200:
            return chunks
        offset += 200


async def test_pdf_ingest_ready_within_60s(client):
    _ensure_pdf()
    import fitz

    with fitz.open(str(PDF_PATH)) as pdf:
        page_count = pdf.page_count
    assert page_count >= 20, f"语料 PDF 仅 {page_count} 页, 不满足 M2-D1 前提"
    data = PDF_PATH.read_bytes()

    # 走真实 API: 注册 -> 建库 -> 上传 PDF
    user_id, headers = await _register_login(client)
    r = await client.post(
        "/api/kb", json={"name": "高等数学讲义PDF", "subject": "math"}, headers=headers
    )
    assert r.status_code == 201, r.text
    kb_id = r.json()["id"]
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("calculus.pdf", data, "application/pdf")},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    assert r.json()["status"] == "pending"

    # 直调 Stage 1 摄取 (不经 ARQ 进程), perf_counter 计时
    from eduagents_worker.ingestion import ingest_document

    t0 = time.perf_counter()
    outcome = await ingest_document({}, user_id, doc_id)
    elapsed = time.perf_counter() - t0

    assert outcome == "ready", f"摄取未达 ready: {outcome}"
    r = await client.get(f"/api/kb/{kb_id}/documents", headers=headers)
    docs = {d["id"]: d for d in r.json()}
    assert docs[doc_id]["status"] == "ready", docs[doc_id].get("error_msg")
    assert elapsed < 60, f"M2-D1 超时: {elapsed:.2f}s >= 60s"

    # 分块预览: 父块+子块存在, page_start 非全 None (真实页码来自 pymupdf4llm)
    chunks = await _fetch_all_chunks(client, headers, kb_id, doc_id)
    assert chunks
    parents = [c for c in chunks if c["parent_id"] is None]
    children = [c for c in chunks if c["parent_id"] is not None]
    assert parents and children
    pages = [c["page_start"] for c in chunks if c["page_start"] is not None]
    assert pages, "chunk 页码全为 None, PDF 页码元数据丢失"
    assert max(pages) > 1  # 页码跨越多页, 不是恒 1 的假页码

    # 证据存档 (M2-D1)
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        "M2-D1 PDF 摄取计时验收 (tests/integration/test_pdf_ingest_timing.py)\n"
        f"时间: {datetime.now().isoformat(timespec='seconds')}\n"
        f"文件: corpus/calculus.pdf ({page_count} 页, {len(data)} 字节)\n"
        f"摄取耗时: {elapsed:.2f} s (阈值 <60s, perf_counter, 直调 ingest_document)\n"
        f"结果: status=ready; 父块 {len(parents)} 个, 子块 {len(children)} 个; "
        f"chunk 页码覆盖 {min(pages)}-{max(pages)}\n"
        "口径: EMBEDDING_MODE=stub / GATEWAY_MODE=stub; 解析=pymupdf4llm; "
        "live 嵌入口径待 SiliconFlow key 后重测 (docs/decisions.md D12)\n",
        encoding="utf-8",
    )
