from __future__ import annotations

from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from eduagents_core.config import get_settings
from eduagents_core.gateway import OpenAIGateway
from eduagents_core.logging import configure_logging, get_logger
from eduagents_core.storage import create_storage
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from .db import dispose_engine, get_engine
from .middleware import RequestContextMiddleware, install_error_handlers
from .ratelimit import RateLimiter
from .routers import auth, chat, health, kb
from .usage import persist_usage

log = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.settings = settings
    get_engine()
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.rate_limiter = RateLimiter(app.state.redis)
    app.state.gateway = OpenAIGateway(settings, usage_sink=persist_usage)
    app.state.storage = create_storage(settings)
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
    log.info("app_started", env=settings.app_env, gateway_mode=settings.gateway_mode)
    yield
    await app.state.arq.aclose()
    await app.state.redis.aclose()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="EduAgents API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(kb.router)
    return app


app = create_app()
