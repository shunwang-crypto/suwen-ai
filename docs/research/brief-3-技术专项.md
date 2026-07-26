# 四项技术决策

## 1. LangGraph 图内 token 级流式

**决策**:generate 节点内用 `get_stream_writer()`(langgraph.config,2026 当前版即 1.x 仍是此 API)把自研网关的 async token 流逐个 `writer()` 出去,API 层用 `graph.astream(stream_mode=["custom","updates"])` 消费——不要用 `astream_events`,也不要用 messages 模式(那只认 langchain LLM 的 callback)。

```python
# 节点内(Python>=3.11 async 节点可直接用;<3.11 必须改为签名注入 writer: StreamWriter)
from langgraph.config import get_stream_writer

async def generate(state: QAState) -> dict:
    writer = get_stream_writer()
    buf = []
    async for tok in gateway.stream_chat(model, msgs):     # 自研网关 async 生成器
        buf.append(tok)
        writer({"ev": "token", "t": tok, "attempt": state.get("attempt", 0)})
    return {"draft": "".join(buf)}

# API 层 SSE
async for item in graph.astream(inp, cfg, stream_mode=["custom", "updates"], subgraphs=True):
    ns, mode, chunk = item                                  # subgraphs=True 时是三元组
    if mode == "custom" and chunk.get("ev") == "token":
        yield f"event: token\ndata: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    elif mode == "updates":
        yield f"event: node\ndata: {json.dumps(list(chunk)[0])}\n\n"  # 节点进度
```

**坑**:① 多模式列表时 astream 产出 `(mode, chunk)` 元组、加 `subgraphs=True` 变 `(namespace, mode, chunk)` 三元组——你们 generate 在五子图内,updates 想看到子图节点必须开 subgraphs,解析要兼容两种形状;② Python<3.11 的 async 节点 `get_stream_writer()` 静默拿不到 writer(contextvar 不传播),确认部署镜像是 3.11+;③ 自省环会二次生成:token 事件必须带 `attempt` 序号,前端收到更高 attempt 就清空重排,否则用户看到两份拼接答案;④ 网关 replay 模式要按录制分片回放 token,否则流式路径永远没被测试覆盖;⑤ writer 每 token 调一次没问题,但 SSE 侧建议 20~40ms 微批合并降低帧开销。

## 2. 引用点击回原文定位

**决策**:本期唯一方案是「后端 chunk 上下文接口 + 前端 Sheet 抽屉文本预览(heading_path 面包屑 + 页码 + `<mark>` 高亮)」,**不引入 pdfjs-dist/react-pdf**——bundle 1MB+、worker 配置、textLayer 与 customTextRenderer 的节点错位是已知 issue、扫描版 PDF 无文本层直接失败,演示风险远大于收益;且文本抽屉对你们未来的 URL/markdown 导入天然通用。PDF 内嵌高亮若答辩前有余力,P2 用 `react-pdf-highlighter-extended` 单独加,不动本期协议。

```python
# GET /api/chunks/{chunk_id}/context?window=1   (走 RLS,tenant 过滤)
async def chunk_context(chunk_id: UUID, window: int = 1):
    hit = await repo.get_chunk(chunk_id)
    sibs = await repo.by_doc_index(hit.doc_id, hit.kb_version,
                                   hit.chunk_index - window, hit.chunk_index + window)
    return {"doc_name": hit.doc_name, "page_start": hit.page_start,
            "heading_path": hit.heading_path,          # ["第3章", "3.2 牛顿定律"]
            "prev": sibs.before, "hit": hit.text, "next": sibs.after}
```

```tsx
// 前端: shadcn Sheet;quote 子串定位失败就整块高亮,绝不报错
const i = hit.indexOf(citation.quote);
{i >= 0 ? <>{hit.slice(0,i)}<mark>{citation.quote}</mark>{hit.slice(i+q.length)}</> : <mark>{hit}</mark>}
```

**坑**:① 引用必须在生成阶段就锚定 `chunk_id`,不要靠事后文本匹配找 chunk;② 相邻 chunk 要限定同 doc 同 kb_version 按 chunk_index±1,跨版本会拼出错文;③ quote 高亮用 `indexOf` 精确子串即可,LLM 改写过的 quote 找不到时降级整块高亮;④ 抽屉头部展示 heading_path + "第 X 页" 本身就是溯源可信度的演示点,答辩时口径为"定位到章节与页码"而非"PDF 像素级高亮"。

## 3. 语义缓存

**决策**:两级缓存——L1 精确用 Redis(归一化文本 SHA256),L2 语义用 **Qdrant 独立 `answer_cache` collection**(复用已有 BGE-M3 与 Qdrant 连接,零新依赖);不用 GPTCache(重依赖、多租户/自研网关适配差、社区维护弱),不用 Redis 裸线扫(O(n) 且引入 Redis Stack 是新组件)。

```python
async def cache_lookup(uid, kb_id, kb_ver, query):
    k = f"qc:{uid}:{kb_id}:{kb_ver}:{sha256(normalize(query))}"
    if exact := await redis.get(k): return json.loads(exact), "exact"
    vec = await embed(query)
    hits = await qdrant.search("answer_cache", vec, limit=1, score_threshold=0.95,
        query_filter=must_eq(tenant_id=uid, kb_id=kb_id, kb_version=kb_ver))
    return (hits[0].payload, "semantic") if hits else (None, None)

async def sse_replay(answer):                    # 命中后伪流式,前端协议完全不变
    for i in range(0, len(answer["text"]), 24):
        yield sse("token", {"t": answer["text"][i:i+24]}); await asyncio.sleep(0.012)
    yield sse("citations", answer["citations"])
    yield sse("meta", {"cache": "hit", "cost_saved": answer["cost"]})
```

**失效策略**:`kb_version` 直接进 filter,重新摄取后旧缓存天然失效、无需主动删;文档删除时 `delete(filter=kb_id)`;Qdrant 无原生 TTL,用 ARQ 定时任务按 `created_at` 清 7 天前的点;L1 Redis TTL 24h。

**坑**:① BGE-M3 对中文短 query 的 0.95 阈值偏松——"牛顿第一定律"vs"牛顿第三定律"可能超阈值,命中后加一道数字/否定词正则比对,不一致则视为 miss(或阈值提 0.97 后实测);② 只缓存自省环**通过后的最终答案**,且绝不缓存混入学情个性化上下文的回答;③ 命中也要写 usage_log(cost=0)——成本面板展示"缓存节省 X 元"是竞赛加分演示;④ 回放必须走与真流式相同的 SSE 事件类型,否则前端两套渲染路径必出 bug。

## 4. 用户日预算强制

**决策**:在**网关 sink 前置检查**(自研网关 completion 入口处),不用 FastAPI 中间件——LLM 调用发生在 LangGraph 节点和 ARQ 后台任务里,一次 HTTP 请求因自省环会触发多次 LLM 调用,中间件既拦不全也拦不准,网关是唯一 choke point;Redis 天粒度整数计数器(微元为单位,INCRBY 避免浮点),usage_log 的 PG SUM 只做对账不做实时判断。

```python
SOFT, HARD = int(0.8 * DAILY_MICRO), DAILY_MICRO
def _key(uid): return f"budget:{uid}:{now_cn():%Y%m%d}"     # 固定 Asia/Shanghai

async def precheck(uid, est_micro) -> str:                  # 网关调用前
    spent = int(await redis.get(_key(uid)) or 0)
    if spent + est_micro >= HARD:
        raise BudgetExceeded(retry_after=seconds_to_cn_midnight())   # API 层转 429
    return "cheap" if spent >= SOFT else "normal"           # cheap: 降档模型+压 max_tokens

async def settle(uid, actual_micro):                        # 网关拿到真实 usage 后
    pipe = redis.pipeline()
    pipe.incrby(_key(uid), actual_micro)
    pipe.expire(_key(uid), 172800, nx=True)                 # 48h 防跨天边界
    await pipe.execute()
```

**降级链**:<80% 正常档 → 80%~100% 强制 cheap 档(切最便宜模型、压 max_tokens、跳过自省环二次调用)→ ≥100% 抛 `BudgetExceeded`,API 层转 429 + `Retry-After`,SSE 场景发 `event: error` 而非静默断流。预检用「已花 + max_tokens×单价的预估」防最后一笔穿透,结算记实际值。

**坑**:① 并发请求同时读到 spent<HARD 会少量超支——竞赛级可接受,严格版用 Lua 原子「预扣+结算返还」,答辩被问到时能说出来即可;② 日期 key 必须固定时区,跟 UTC 混用会出现"半夜预算复活"的诡异现象;③ 网关 replay/stub 模式 cost 必须为 0 且不写计数器,否则跑测试把演示账号预算烧光;④ TTL 用 48h 而非 24h,否则 23:59 创建的 key 次日争用窗口内读到空值;⑤ cheap 档本身也要有 max_tokens 硬上限,防止降档后一条长回答仍然穿透预算。

Sources: [LangGraph Streaming 官方文档](https://docs.langchain.com/oss/python/langgraph/streaming), [get_stream_writer API 参考](https://reference.langchain.com/python/langgraph/config/get_stream_writer), [langgraph#4610 async 节点 writer 问题](https://github.com/langchain-ai/langgraph/issues/4610), [react-pdf#1042 customTextRenderer 错位](https://github.com/wojtekmaj/react-pdf/issues/1042), [react-pdf-highlighter-extended](https://github.com/DanielArnould/react-pdf-highlighter), [2026 语义缓存方案对比](https://www.getmaxim.ai/articles/top-semantic-caching-solutions-for-ai-applications-in-2026/)