"""M2 端到端: 上传 -> 摄取流水线 -> 分块预览 -> 混合检索 -> 带引用问答 -> 删除清理。
覆盖 F2-A2(恶意文件)/F2-A3(删除后不再返回)/引用溯源主链路。stub 网关+嵌入, 零外部 API。"""
import json
import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

_CALCULUS_MD = """# 第四章 泰勒公式

## 4.1 泰勒公式的定义

设函数 $f(x)$ 在 $x_0$ 处 $n$ 阶可导，则 $f(x)$ 可以展开为泰勒多项式:

$$f(x) = \\sum_{k=0}^{n} \\frac{f^{(k)}(x_0)}{k!}(x-x_0)^k + R_n(x)$$

其中 $R_n(x)$ 称为余项。

## 4.2 拉格朗日余项

拉格朗日余项的形式为:

$$R_n(x) = \\frac{f^{(n+1)}(\\xi)}{(n+1)!}(x-x_0)^{n+1}$$

其中 $\\xi$ 介于 $x_0$ 与 $x$ 之间。拉格朗日余项给出了泰勒逼近误差的精确表达。

## 4.3 常见函数的泰勒展开

| 函数 | 展开式 | 收敛域 |
|------|--------|--------|
| $e^x$ | $\\sum x^n/n!$ | 全体实数 |
| $\\sin x$ | $\\sum (-1)^n x^{2n+1}/(2n+1)!$ | 全体实数 |
| $\\ln(1+x)$ | $\\sum (-1)^{n-1} x^n/n$ | $(-1, 1]$ |

以上展开式在近似计算中非常有用。
"""


async def _register_login(client):
    email = f"rag-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _sse_events(client, headers, payload: dict) -> dict[str, list]:
    events: dict[str, list] = {}
    current = None
    async with client.stream(
        "POST", "/api/chat", json=payload, headers=headers, timeout=60
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current:
                events.setdefault(current, []).append(json.loads(line.split(":", 1)[1]))
    return events


async def test_rag_pipeline_end_to_end(client):
    user_id, headers = await _register_login(client)

    # 1) 建库 + 上传
    r = await client.post("/api/kb", json={"name": "高等数学", "subject": "math"}, headers=headers)
    assert r.status_code == 201, r.text
    kb_id = r.json()["id"]
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("calculus.md", _CALCULUS_MD.encode(), "text/markdown")},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    assert r.json()["status"] == "pending"

    # 2) 摄取 (测试内直接调 worker 任务函数, 不经 ARQ 进程)
    from eduagents_worker.ingestion import ingest_document

    outcome = await ingest_document({}, user_id, doc_id)
    assert outcome == "ready"
    r = await client.get(f"/api/kb/{kb_id}/documents", headers=headers)
    docs = {d["id"]: d for d in r.json()}
    assert docs[doc_id]["status"] == "ready", docs[doc_id]["error_msg"]

    # 3) 分块预览器: 有父块与子块, heading_path 面包屑存在, 公式未丢
    r = await client.get(f"/api/kb/{kb_id}/documents/{doc_id}/chunks", headers=headers)
    chunks = r.json()
    assert len(chunks) >= 2
    assert any("泰勒" in c["heading_path"] for c in chunks)
    all_text = "".join(c["text"] for c in chunks)
    assert "$$" in all_text  # F2-A6: LaTeX 公式保留

    # 4) 检索调试面板: 混合检索命中拉格朗日余项小节
    r = await client.post(
        f"/api/kb/{kb_id}/debug/retrieve",
        json={"query": "拉格朗日余项的形式是什么"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    debug = r.json()
    assert debug["chunks"], "检索不应为空"
    assert debug["used_sparse"] and debug["used_rerank"]
    top_headings = " | ".join(c["heading_path"] for c in debug["chunks"][:3])
    assert "余项" in top_headings or "泰勒" in top_headings

    # 5) 带引用问答: citation 事件 + 生成 + done 计数一致
    events = await _sse_events(
        client, headers, {"message": "泰勒公式的拉格朗日余项是什么形式?", "kb_id": kb_id}
    )
    assert "citation" in events and len(events["citation"]) >= 1
    cited = events["citation"][0]
    assert cited["doc_name"] == "calculus.md" and cited["heading_path"]
    assert "token" in events and "done" in events
    assert events["done"][0]["citations"] == len(events["citation"])
    assert any(e.get("type") == "retrieval" for e in events.get("trace_event", []))

    # 6) 库外问题 (语料无关) -> 检索仍返回若干结果或拒答, 但绝不 500
    events2 = await _sse_events(
        client, headers, {"message": "水浒传的作者是谁?", "kb_id": kb_id}
    )
    assert "done" in events2

    # 7) F2-A3: 删除文档 -> 物理清理 -> 检索不再返回其内容
    r = await client.delete(f"/api/kb/{kb_id}/documents/{doc_id}", headers=headers)
    assert r.status_code == 204
    from eduagents_worker.ingestion import cleanup_document

    await cleanup_document({}, user_id, kb_id, doc_id, "calculus.md")
    r = await client.post(
        f"/api/kb/{kb_id}/debug/retrieve",
        json={"query": "拉格朗日余项"},
        headers=headers,
    )
    assert r.json()["chunks"] == []

    # 8) 全局断言: 本次问答 usage 按 qa 归因 (F10-A1 维度扩展)
    from eduagents_api.db import tenant_session
    from eduagents_api.models import UsageLog

    async with tenant_session(user_id) as session:
        features = set(
            (await session.scalars(select(UsageLog.feature))).all()
        )
    assert "qa" in features


async def test_upload_security_rejections(client):
    """F2-A2: 伪装/超限/空文件全部被拒且错误可读。"""
    _, headers = await _register_login(client)
    r = await client.post("/api/kb", json={"name": "sec", "subject": "general"}, headers=headers)
    kb_id = r.json()["id"]

    # 伪装 pdf (魔数不符)
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("evil.pdf", b"MZ\x90\x00 not a pdf", "application/pdf")},
        headers=headers,
    )
    assert r.status_code == 400 and r.json()["code"] == "FILE_SIGNATURE_MISMATCH"

    # 不支持的类型
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("run.exe", b"MZ\x90\x00", "application/octet-stream")},
        headers=headers,
    )
    assert r.status_code == 400 and r.json()["code"] == "UNSUPPORTED_FILE_TYPE"

    # 空文件
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("empty.md", b"", "text/markdown")},
        headers=headers,
    )
    assert r.status_code == 400 and r.json()["code"] == "EMPTY_FILE"

    # 非 utf-8 的 md
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("bad.md", b"\xff\xfe\x00\x01", "text/markdown")},
        headers=headers,
    )
    assert r.status_code == 400 and r.json()["code"] == "FILE_SIGNATURE_MISMATCH"

    # 越权: 另一个用户看不到这个库
    _, headers_b = await _register_login(client)
    r = await client.get(f"/api/kb/{kb_id}/documents", headers=headers_b)
    assert r.json() == []
