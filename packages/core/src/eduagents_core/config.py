from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field
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
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # 应用连接: 必须是非超级用户角色 (超级用户会静默绕过 RLS, 见 decisions.md D9)
    database_url: str = "postgresql+asyncpg://edu_app:edu@127.0.0.1:5432/eduagents"
    # 迁移连接: 表 owner (edu); 为空则退回 database_url
    migration_database_url: str = "postgresql+asyncpg://edu:edu@127.0.0.1:5432/eduagents"
    redis_url: str = "redis://127.0.0.1:6379/0"
    qdrant_url: str = "http://localhost:6333"
    minio_endpoint: str = "localhost:9000"
    minio_app_access_key: str = Field(
        default="eduagents",
        validation_alias=AliasChoices("MINIO_APP_ACCESS_KEY", "MINIO_ACCESS_KEY"),
    )
    minio_app_secret_key: str = Field(
        default="eduagents-secret",
        validation_alias=AliasChoices("MINIO_APP_SECRET_KEY", "MINIO_SECRET_KEY"),
    )
    minio_bucket: str = "eduagents"
    minio_secure: bool = False

    # --- auth ---
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 14 * 24 * 3600
    bcrypt_rounds: int = 12
    public_registration: bool = True
    # 生产公开注册始终强制邮箱验证；测试可显式关闭以复用旧夹具。
    email_verification_required: bool = True
    email_code_ttl_seconds: int = 600
    email_code_resend_cooldown_seconds: int = 60
    email_code_max_attempts: int = 5
    email_code_per_minute: int = 5
    email_code_daily_limit: int = 20
    email_code_ip_daily_limit: int = 100

    # --- 图形验证码 (自托管 Pillow 生成, 零外部依赖) ---
    # 注册是否强制验证码: 默认开; 集成测试通道 = APP_ENV=test 或环境变量
    # CAPTCHA_REQUIRED_REGISTER=0 (tests/conftest.py 已显式关闭, 专项测试再打开)
    captcha_required_register: bool = True
    # 验证码图片获取接口独立限流 (登录页可能连刷几张, 比 auth 主桶宽)
    captcha_rate_limit_per_minute: int = 30

    # --- 密码找回 ---
    password_reset_ttl_seconds: int = 1800  # 重置 token 有效期 30min
    # 重置链接的前端地址前缀: {web_origin}/reset?token=...
    web_origin: str = "http://localhost:3000"
    # forgot 接口独立限流桶 (5/min/IP), 防批量骚扰发信
    auth_forgot_rate_limit_per_minute: int = 5

    # --- SMTP 发信 (host/user/password/from 四项齐全才启用真实发送;
    #     否则退回 DevEmailSender: 只打日志, 不伪造"已发送"的假象) ---
    smtp_host: str = ""
    smtp_port: int = 465  # 465=SSL 直连; 其他端口走 STARTTLS
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_reply_to: str = ""

    # --- rate limits (token bucket, per minute capacity) ---
    auth_rate_limit_per_minute: int = 5
    # refresh 单独更宽的桶: 每次整页刷新都会静默续期, 5/min 会把正常刷新用户
    # 锁出登录态 (UI 巡检实测发现); 撞库风险在 login/register, refresh 仅凭有效 cookie
    auth_refresh_rate_limit_per_minute: int = 60
    api_rate_limit_per_minute: int = 120

    # --- budget ---
    user_daily_budget_cny: float = 5.0
    # 软限 (80-100%) 降档到 cheap 后的 max_tokens 硬上限, 防降档后一条长回答仍穿透预算
    budget_cheap_max_tokens: int = 1024
    # 全站日成本熔断 (P0-2b): ≥80% 全站强制 cheap / ≥100% 拒非 admin / ≥150% 全档熔断;
    # <=0 关闭全局熔断 (仅保留用户日预算)
    global_daily_budget_cny: float = 50.0

    # --- billing (0006 预付额度) ---
    # 扣费 = 模型真实成本 × billing_markup (Decimal 运算统一在 apps/api/wallet.py,
    # 本处 float 仅作配置载体, 使用前一律 Decimal(str(...)) 转换, 禁 float 直接参与账目)
    billing_markup: float = 3.0
    # 注册赠送额度 (CNY 计价 credit); 幂等由 wallet_ledger 唯一部分索引保证
    signup_grant_cny: float = 10.0
    # 支付通道: mock = 演示通道 (confirm 即到账, 仅非 prod); 真实通道留接口未实现
    payment_provider: str = "mock"

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
    # Production keeps the admin fault-injection surface off unless an
    # operator explicitly enables it on a private management network.
    enable_fault_injection: bool = True

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

    # Multimodal chat attachments use the standard provider credentials and an
    # explicit VL model on that same OpenAI-compatible endpoint.
    vision_model: str = ""

    strong_api_key: str = ""
    strong_base_url: str = "https://api.deepseek.com"
    strong_model: str = "deepseek-reasoner"
    strong_input_price_per_m: float = 4.0
    strong_output_price_per_m: float = 16.0

    # --- observability: Langfuse (M3-D1) ---
    # 三项齐全才启用; 未启用时不 import langfuse, 热路径零额外开销
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""
    # P2-7: 默认不上报 prompt/completion 原文 (只上长度+摘要哈希);
    # =1 显式打开原文上报, 仅限本地调试, 生产必须保持关闭
    langfuse_log_content: bool = False

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

    # --- web import (URL 网页导入) ---
    # 仅本地集成测试打开 (临时 http server 在 127.0.0.1); 生产必须 False,
    # 否则 SSRF 防护 (内网/回环/元数据地址拦截) 被整体旁路, 见 parsing/web.py
    web_import_allow_private_hosts: bool = False

    # --- upload constraints ---
    max_upload_mb: int = 50
    max_chat_attachment_mb: int = 20
    max_chat_attachments_per_message: int = 4
    max_chat_attachment_text_chars: int = 2_000_000
    max_error_import_drafts: int = 20
    error_import_draft_ttl_hours: int = 24
    max_learning_resource_mb: int = 50
    max_learning_resources_per_user: int = 100
    max_pending_learning_resources: int = 20
    max_learning_resource_questions: int = 200
    max_learning_resource_pages: int = 100
    max_docs_per_user: int = 200
    max_storage_mb_per_user: int = 1024
    # 摄取 chunk 配额 (P0-2c): 成本按 chunk 走 (enrich 每 chunk 2~3 次 LLM),
    # 配额必须与成本同单位; 超限置 failed 而非静默截断。<=0 关闭对应配额。
    max_chunks_per_doc: int = 3000
    max_chunks_per_user: int = 20000

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
