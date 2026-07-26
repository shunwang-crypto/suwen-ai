from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class TierConfig(BaseModel):
    """One model tier: a real, independently reachable OpenAI-compatible endpoint."""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    # CNY per 1M tokens; used for usage_log cost attribution
    input_price_per_m: float = 0.0
    output_price_per_m: float = 0.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # 应用连接: 必须是非超级用户角色 (超级用户会静默绕过 RLS, 见 decisions.md D9)
    database_url: str = "postgresql+asyncpg://edu_app:edu@127.0.0.1:5432/eduagents"
    # 迁移连接: 表 owner (edu); 为空则退回 database_url
    migration_database_url: str = "postgresql+asyncpg://edu:edu@127.0.0.1:5432/eduagents"
    redis_url: str = "redis://127.0.0.1:6379/0"
    qdrant_url: str = "http://localhost:6333"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "eduagents"
    minio_secret_key: str = "eduagents-secret"
    minio_bucket: str = "eduagents"
    minio_secure: bool = False

    # --- auth ---
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 14 * 24 * 3600
    bcrypt_rounds: int = 12

    # --- rate limits (token bucket, per minute capacity) ---
    auth_rate_limit_per_minute: int = 5
    api_rate_limit_per_minute: int = 120

    # --- budget ---
    user_daily_budget_cny: float = 5.0
    # 软限 (80-100%) 降档到 cheap 后的 max_tokens 硬上限, 防降档后一条长回答仍穿透预算
    budget_cheap_max_tokens: int = 1024

    # --- model gateway ---
    # live: real API calls; record: live + save cassette; replay: cassette only;
    # stub: deterministic fake output (dev/CI only, never a demo claim)
    gateway_mode: Literal["live", "record", "replay", "stub"] = "stub"
    gateway_cassette_dir: str = "cassettes"
    gateway_replay_fallback_stub: bool = True
    gateway_stream_chunk_chars: int = 8
    gateway_stream_delay_ms: int = 0
    gateway_max_retries: int = 3
    gateway_timeout_seconds: float = 60.0

    # circuit breaker
    breaker_window_seconds: float = 60.0
    breaker_min_samples: int = 6
    breaker_failure_rate: float = 0.5
    breaker_open_seconds: float = 30.0

    # tiers (flattened env: CHEAP_API_KEY, STANDARD_MODEL, ...)
    cheap_api_key: str = ""
    cheap_base_url: str = "https://api.siliconflow.cn/v1"
    cheap_model: str = "Qwen/Qwen2.5-7B-Instruct"
    cheap_input_price_per_m: float = 0.35
    cheap_output_price_per_m: float = 0.35

    standard_api_key: str = ""
    standard_base_url: str = "https://api.deepseek.com"
    standard_model: str = "deepseek-chat"
    standard_input_price_per_m: float = 2.0
    standard_output_price_per_m: float = 8.0

    strong_api_key: str = ""
    strong_base_url: str = "https://api.deepseek.com"
    strong_model: str = "deepseek-reasoner"
    strong_input_price_per_m: float = 4.0
    strong_output_price_per_m: float = 16.0

    # --- search ---
    tavily_api_key: str = ""

    # --- embeddings / rerank (M2) ---
    # stub: 确定性哈希向量 (开发/CI, 零网络); live: SiliconFlow BGE-M3
    embedding_mode: Literal["live", "stub"] = "stub"
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    rerank_mode: Literal["live", "stub"] = "stub"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # --- vector store (M2) ---
    # qdrant_local_path 非空 -> qdrant-client 纯本地模式 (无服务依赖);
    # 为空 -> 连 qdrant_url 服务端
    qdrant_local_path: str = ""
    qdrant_collection: str = "eduagents_chunks"

    # --- object storage (M2) ---
    storage_backend: Literal["local", "minio"] = "local"
    local_storage_dir: str = ""  # 空 -> ~/.local/share/eduagents/objects

    # --- retrieval (M2, 每个策略独立开关供消融) ---
    retrieval_candidate_k: int = 20
    retrieval_final_k: int = 5
    rrf_k: int = 60
    retrieval_enable_sparse: bool = True
    retrieval_enable_rerank: bool = True
    retrieval_confidence_threshold: float = 0.35

    # --- chunking (M2) ---
    chunk_child_chars: int = 700
    chunk_parent_chars: int = 2400
    chunk_overlap_chars: int = 80

    # --- upload constraints ---
    max_upload_mb: int = 50
    max_docs_per_user: int = 200
    max_storage_mb_per_user: int = 1024

    def tier(self, name: str) -> TierConfig:
        return TierConfig(
            api_key=getattr(self, f"{name}_api_key"),
            base_url=getattr(self, f"{name}_base_url"),
            model=getattr(self, f"{name}_model"),
            input_price_per_m=getattr(self, f"{name}_input_price_per_m"),
            output_price_per_m=getattr(self, f"{name}_output_price_per_m"),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
