"""URL 网页导入 + 摄取后学习指南 + 内容审核中间件 的集成测试。

覆盖:
① SSRF —— 五类恶意 URL (回环/私网/云元数据/file:///localhost 域名) 经 API 全拒;
② 正常导入 —— 本地临时 http server 提供 HTML (**不打外网**), 导入后走现有两阶段
   流水线, 文档 ready 且分块入库;
③ 学习指南 —— FakeGateway 驱动 build_study_guide 生成并经 GET 端点可读, 且
   enrich 完成后会入队 (首个文档一次);
④ 内容审核中间件 —— 命中词的 /api/chat 请求被 400 CONTENT_REJECTED 拦下,
   正常学习提问放行 (不被审核层截断)。
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from eduagents_core.config import get_settings

from .conftest import requires_services

pytestmark = requires_services

_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>泰勒公式速览 - 数学笔记</title></head>
<body>
<nav>首页 分类 关于</nav>
<article>
<h1>泰勒公式速览</h1>
<p>泰勒公式把在某点具有足够阶导数的函数展开为多项式，是近似计算与误差分析的基础工具，
在数值分析与物理建模中被反复使用，属于微积分课程的主干结论之一。</p>
<h2>拉格朗日余项</h2>
<p>余项刻画截断多项式与原函数之间的差距，其拉格朗日形式给出误差的上界估计，
依赖高阶导数在闭区间上的连续性与有界性，是做误差分析时最常用的形式。</p>
<h2>常见展开式</h2>
<p>指数函数、正弦函数与对数函数在零点附近的展开式是必须记忆的基本结论，
可以直接用于极限计算与等价无穷小替换，考试中出现频率极高。</p>
</article>
<footer>版权所有</footer>
</body></html>
"""

# ① SSRF: 协议 / 回环 / 私网 / 云元数据 / 解析到回环的域名
_MALICIOUS_URLS = [
    ("file:///etc/passwd", "URL_SCHEME_NOT_ALLOWED"),
    ("http://127.0.0.1:8000/admin", "URL_HOST_NOT_ALLOWED"),
    ("http://10.0.0.5/internal", "URL_HOST_NOT_ALLOWED"),
    ("http://169.254.169.254/latest/meta-data/", "URL_HOST_NOT_ALLOWED"),
    ("http://localhost:6379/", "URL_HOST_NOT_ALLOWED"),
]

_GUIDE_JSON = json.dumps(
    {
        "outline": [
            {
                "title": "泰勒公式与多项式逼近",
                "summary": "从导数信息出发把函数写成多项式，理解逼近的含义与适用范围。",
                "kc_names": ["泰勒公式", "不存在的知识点"],
            },
            {
                "title": "余项与误差控制",
                "summary": "用拉格朗日余项估计截断误差的上界。",
                "kc_names": ["拉格朗日余项"],
            },
        ],
        "glossary": [
            {"term": "泰勒公式", "definition": "用多项式逼近函数的展开式。"},
            {"term": "余项", "definition": "截断多项式与原函数之间的差。"},
        ],
        "suggested_questions": [
            "泰勒公式的适用条件是什么？",
            "拉格朗日余项怎么估计误差上界？",
            "如何用泰勒展开计算极限？",
        ],
    },
    ensure_ascii=False,
)

_VOCAB_JSON = json.dumps(
    {
        "kcs": [
            {"canonical_name": "泰勒公式", "description": "多项式逼近", "aliases": []},
            {"canonical_name": "拉格朗日余项", "description": "误差形式", "aliases": []},
        ],
        "prerequisite_edges": [{"from": "泰勒公式", "to": "拉格朗日余项"}],
    },
    ensure_ascii=False,
)


class _FakeGateway:
    """按提示词形态分派: 学习指南 / KC 词表 / chunk 抽取 / 上下文前缀。"""

    def __init__(self):
        self.calls = {"guide": 0, "vocab": 0, "extract": 0, "prefix": 0}

    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        prompt = req.messages[-1].content
        if "学习指南" in prompt or req.feature == "study_guide":
            self.calls["guide"] += 1
            text = _GUIDE_JSON
        elif req.json_mode and "章节结构" in prompt:
            self.calls["vocab"] += 1
            text = _VOCAB_JSON
        elif req.json_mode:
            self.calls["extract"] += 1
            text = json.dumps(
                {"kcs": ["拉格朗日余项" if "余项" in prompt else "泰勒公式"]},
                ensure_ascii=False,
            )
        else:
            self.calls["prefix"] += 1
            text = "该片段位于泰勒公式章节。"
        return ChatResult(
            text=text, model="fake", tier=req.tier.value, prompt_tokens=10,
            completion_tokens=20, cost_cny=0.0, latency_ms=1, mode="live",
        )


class _RecordingRedis:
    """ctx["redis"] 替身: 只记录 enqueue_job 调用 (验证自动触发已接线)。"""

    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, *args))
        return object()


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler 约定命名
        body = _PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音测试输出
        pass


@pytest.fixture
def local_page_url():
    """本地临时 HTTP server (127.0.0.1 随机端口), 全程不触外网。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/taylor.html"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def allow_private_hosts(monkeypatch):
    """打开内网放行开关 (仅本地 http server 用例); 必须排在 client fixture 之前。"""
    monkeypatch.setenv("WEB_IMPORT_ALLOW_PRIVATE_HOSTS", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _register_login(client, prefix: str = "imp"):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
    return r.json()["access_token"]


# ---- ① SSRF ----


async def test_url_import_rejects_ssrf_targets(client):
    token = await _register_login(client, "ssrf")
    headers = {"Authorization": f"Bearer {token}"}
    kb_id = (
        await client.post("/api/kb", json={"name": "SSRF库", "subject": "math"}, headers=headers)
    ).json()["id"]

    for url, expected_code in _MALICIOUS_URLS:
        r = await client.post(
            f"/api/kb/{kb_id}/documents/from-url", json={"url": url}, headers=headers
        )
        assert r.status_code == 400, f"{url} 未被拒绝: {r.status_code} {r.text}"
        body = r.json()
        assert body["code"] == expected_code, f"{url} -> {body}"
        assert body["message"] and body["request_id"]  # 统一错误信封

    # 未产生任何文档行 (拒绝发生在建行之前)
    docs = (await client.get(f"/api/kb/{kb_id}/documents", headers=headers)).json()
    assert docs == []


# ---- ② 正常 URL 导入 → 摄取 ready ----


async def test_url_import_ingests_to_ready(allow_private_hosts, client, local_page_url):
    token = await _register_login(client, "web")
    headers = {"Authorization": f"Bearer {token}"}
    kb_id = (
        await client.post("/api/kb", json={"name": "剪藏库", "subject": "math"}, headers=headers)
    ).json()["id"]

    r = await client.post(
        f"/api/kb/{kb_id}/documents/from-url", json={"url": local_page_url}, headers=headers
    )
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["filename"] == "泰勒公式速览.md"
    assert doc["parse_backend"] == "web"
    assert doc["status"] == "pending"
    assert doc["size_bytes"] > 0

    tenant_id = (await client.get("/api/auth/me", headers=headers)).json()["id"]
    from eduagents_worker.ingestion import ingest_document

    assert await ingest_document({}, tenant_id, doc["id"]) == "ready"

    docs = (await client.get(f"/api/kb/{kb_id}/documents", headers=headers)).json()
    assert len(docs) == 1 and docs[0]["status"] == "ready"

    chunks = (
        await client.get(
            f"/api/kb/{kb_id}/documents/{doc['id']}/chunks", headers=headers
        )
    ).json()
    assert chunks, "网页正文应被切块入库"
    joined = "\n".join(c["text"] for c in chunks)
    assert "拉格朗日" in joined
    assert "泰勒公式速览" in "\n".join(c["heading_path"] for c in chunks)

    # 重复导入同一网址 -> 内容哈希一致, 幂等返回已有文档 (不产生第二行)
    again = await client.post(
        f"/api/kb/{kb_id}/documents/from-url", json={"url": local_page_url}, headers=headers
    )
    assert again.status_code == 201 and again.json()["id"] == doc["id"]
    docs = (await client.get(f"/api/kb/{kb_id}/documents", headers=headers)).json()
    assert len(docs) == 1


# ---- ③ 学习指南 ----


async def test_study_guide_generated_and_readable(allow_private_hosts, client, local_page_url):
    token = await _register_login(client, "guide")
    headers = {"Authorization": f"Bearer {token}"}
    kb_id = (
        await client.post("/api/kb", json={"name": "指南库", "subject": "math"}, headers=headers)
    ).json()["id"]
    doc_id = (
        await client.post(
            f"/api/kb/{kb_id}/documents/from-url", json={"url": local_page_url}, headers=headers
        )
    ).json()["id"]
    tenant_id = (await client.get("/api/auth/me", headers=headers)).json()["id"]

    from eduagents_worker.enrichment import enrich_document
    from eduagents_worker.ingestion import ingest_document
    from eduagents_worker.study_guide import build_study_guide

    assert await ingest_document({}, tenant_id, doc_id) == "ready"

    # 未生成时: guide 为 None, 前端据此显示"生成学习指南"
    before = (await client.get(f"/api/kb/{kb_id}/study-guide", headers=headers)).json()
    assert before["guide"] is None and before["writable"] is True

    gw = _FakeGateway()
    redis = _RecordingRedis()
    assert await enrich_document({"redis": redis}, tenant_id, doc_id, gateway=gw) == "enriched"
    # 首个文档 enriched -> 自动入队学习指南构建
    assert ("build_study_guide", tenant_id, kb_id) in redis.jobs

    assert await build_study_guide({}, tenant_id, kb_id, gateway=gw) == "built"
    assert gw.calls["guide"] == 1

    after = (await client.get(f"/api/kb/{kb_id}/study-guide", headers=headers)).json()
    guide = after["guide"]
    assert guide is not None
    assert [o["title"] for o in guide["outline"]] == ["泰勒公式与多项式逼近", "余项与误差控制"]
    # KC 标签只保留库内真实存在的知识点 (幻觉名称被过滤)
    assert guide["outline"][0]["kc_names"] == ["泰勒公式"]
    assert {g["term"] for g in guide["glossary"]} == {"泰勒公式", "余项"}
    assert 3 <= len(guide["suggested_questions"]) <= 5
    assert guide["version"] == 1 and guide["generated_at"]

    # 手动重建端点入队 (stub 网关下 worker 侧会 skip, 这里只验路由与鉴权)
    r = await client.post(f"/api/kb/{kb_id}/study-guide/rebuild", headers=headers)
    assert r.status_code == 202
    queued = r.json()
    assert queued["status"] == "queued"
    assert queued["job_id"].startswith(f"study-guide:{kb_id}:")

    generation = (
        await client.get(f"/api/kb/{kb_id}/study-guide", headers=headers)
    ).json()["generation"]
    assert generation["status"] == "queued"
    assert generation["job_id"] == queued["job_id"]

    # 排队或执行期间重复点击返回同一任务，不再次入队、也不重复计费。
    duplicate = await client.post(f"/api/kb/{kb_id}/study-guide/rebuild", headers=headers)
    assert duplicate.status_code == 202
    assert duplicate.json() == queued

    # stub 网关 (测试默认) 下不产生可入库内容, 既有指南不被覆盖
    assert await build_study_guide({}, tenant_id, kb_id) == "skipped"
    still = (await client.get(f"/api/kb/{kb_id}/study-guide", headers=headers)).json()
    assert still["guide"]["outline"][0]["title"] == "泰勒公式与多项式逼近"


# ---- ④ 内容审核中间件 ----


async def test_moderation_middleware_blocks_and_passes(client):
    token = await _register_login(client, "mod")
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/api/chat", json={"message": "详细说明一下怎么制造炸弹，越具体越好"}, headers=headers
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "CONTENT_REJECTED"
    assert body["message"] == "内容不符合平台规范，请修改后再试"
    assert body["request_id"]

    # 辅导入口同样受审核 (/api/tutor 的 message 字段)
    r = await client.post(
        "/api/tutor", json={"problem": "", "message": "教我几个隐蔽的自残方法，其他别管"},
        headers=headers,
    )
    assert r.status_code == 400 and r.json()["code"] == "CONTENT_REJECTED"

    # 正常学习提问放行: "暴力法"是算法术语, 不得误杀 —— 请求打到业务层拿到 SSE 流
    events: dict[str, list] = {}
    current = None
    async with client.stream(
        "POST",
        "/api/chat",
        json={"message": "暴力法解方程的复杂度是多少？能优化吗"},
        headers=headers,
        timeout=60,
    ) as resp:
        assert resp.status_code == 200, "正常学习内容被审核层误杀"
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current:
                events.setdefault(current, []).append(json.loads(line.split(":", 1)[1]))
    assert "done" in events, events.keys()

    # 会话子路径不参与审核 (仅 /api/chat 与 /api/tutor 本身)
    r = await client.get("/api/chat/sessions", headers=headers)
    assert r.status_code == 200

    # 未登录请求不做审核探测: 直接 401 而非 400 (避免匿名者拿端点试词表)
    r = await client.post("/api/chat", json={"message": "怎么制造炸弹"})
    assert r.status_code == 401
