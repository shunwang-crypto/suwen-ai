from __future__ import annotations

from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from eduagents_core.config import get_settings
from eduagents_core.gateway import OpenAIGateway
from eduagents_core.logging import configure_logging, get_logger
from eduagents_core.observability import flush_langfuse
from eduagents_core.storage import create_storage
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from .db import dispose_engine, get_engine
from .middleware import (
    ContentModerationMiddleware,
    RequestContextMiddleware,
    install_error_handlers,
)
from .ratelimit import RateLimiter
from .routers import (
    account,
    admin,
    auth,
    billing,
    chat,
    chat_attachments,
    evals,
    feedback,
    health,
    kb,
    next_actions,
    ops,
    orgs,
    plans,
    preferences,
    profile,
    quiz,
    reports,
    reviews,
    teacher,
    threads,
)
from .security import assert_production_secrets
from .usage import persist_usage

log = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 修正#2: prod 弱/默认 JWT secret 启动即炸, 不给"忘了改"留活路
    assert_production_secrets(settings)
    configure_logging(settings.log_level)
    app.state.settings = settings
    get_engine()
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.rate_limiter = RateLimiter(app.state.redis)
    # 网关入口预检 (0006): 先钱包 (余额<=0 -> 402) 后预算 (<80% 正常 / 80-100%
    # 降 cheap / ≥100% 拒绝 429) — WalletBudgetGuard 组合两者, 见 wallet.py
    from .wallet import create_billing_guard

    app.state.gateway = OpenAIGateway(
        settings,
        usage_sink=persist_usage,
        budget_guard=create_billing_guard(app.state.redis, settings),
    )
    app.state.storage = create_storage(settings)
    # 两级语义缓存 (包B): L1 Redis 精确 + L2 Qdrant answer_cache
    try:
        from eduagents_core.cache import AnswerCache
        from eduagents_core.embedding.providers import create_embedder

        app.state.answer_cache = AnswerCache(settings, redis=app.state.redis)
        app.state.embedder = create_embedder(settings)
    except Exception:  # noqa: BLE001 — 缓存栈缺失时问答照常 (未命中语义)
        app.state.answer_cache = None
        app.state.embedder = None
        log.exception("answer_cache_unavailable")
    app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        from eduagents_core.embedding.providers import create_embedder
        from eduagents_core.embedding.rerankers import create_reranker
        from eduagents_core.embedding.sparse import Bm25SparseEncoder
        from eduagents_core.retrieval.service import RetrievalService
        from eduagents_core.vectorstore.qdrant_store import QdrantStore

        app.state.retrieval = RetrievalService(
            settings,
            create_embedder(settings),
            Bm25SparseEncoder(),
            QdrantStore(settings),
            create_reranker(settings),
        )
    except Exception:  # noqa: BLE001 — 检索栈缺失时 API 其余功能照常, kb 问答 503
        app.state.retrieval = None
        log.exception("retrieval_stack_unavailable")
    try:
        from eduagents_core.graphs.state import GraphDeps
        from eduagents_core.tools.web_search import TavilySearch

        from .graph_runtime import GraphRuntime

        tavily = TavilySearch(settings)

        async def _web_search(query: str, *, required: bool = False):
            results, from_cache = await tavily.search(query, raise_on_failure=required)
            return (results, from_cache) if results else None

        deps = GraphDeps(
            settings=settings,
            gateway=app.state.gateway,
            retrieval=app.state.retrieval,
            web_search=_web_search if tavily.available else None,
        )
        app.state.graphs = GraphRuntime(settings, deps)
        await app.state.graphs.start()
    except Exception:  # noqa: BLE001 — 图运行时缺失时基础问答仍可用
        app.state.graphs = None
        log.exception("graph_runtime_unavailable")
    log.info("app_started", env=settings.app_env, gateway_mode=settings.gateway_mode)
    yield
    # 先停止仍在后台生成的对话，再关闭其依赖的图运行时/Redis/数据库连接。
    await chat.shutdown_chat_runs()
    if getattr(app.state, "graphs", None) is not None:
        await app.state.graphs.stop()
    await app.state.arq.aclose()
    await app.state.redis.aclose()
    await dispose_engine()
    # M3-D1: Langfuse OTel 后台队列冲干净 (未启用时零操作, 内部 try/except)
    flush_langfuse()


def create_app() -> FastAPI:
    app = FastAPI(
        title="EduAgents API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    # 先 add 的在栈内层: 审核中间件跑在 RequestContext 之内, 拿得到 request_id
    app.add_middleware(ContentModerationMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Chat-Session-ID", "X-Chat-Run-ID"],
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(chat_attachments.router)
    app.include_router(kb.router)
    app.include_router(orgs.router)
    app.include_router(threads.router)
    app.include_router(quiz.router)
    app.include_router(plans.router)
    app.include_router(plans.tutor_router)
    app.include_router(profile.router)
    app.include_router(preferences.router)  # P2-3 用户偏好 (讲解风格/兴趣/时长/目标)
    app.include_router(reports.router)
    app.include_router(ops.router)
    app.include_router(reviews.router)
    app.include_router(reviews.errors_router)  # 错题本 /api/errors (F6)
    app.include_router(reviews.appeals_router)  # P2-3 判分申诉
    app.include_router(next_actions.router)  # P2-2/P2-5 可解释推荐 (F8-A2 开场卡数据源)
    app.include_router(admin.router)
    app.include_router(evals.router)  # F9 评估看板
    app.include_router(teacher.router)  # U-A4 教师组卷导出
    app.include_router(account.router)  # P0-4 注销 + P1-8 数据导出
    app.include_router(feedback.router)  # P2-1 用户反馈
    app.include_router(feedback.admin_router)
    app.include_router(billing.router)  # 0006 预付额度: 钱包/流水/定价/充值订单
    return app


app = create_app()
