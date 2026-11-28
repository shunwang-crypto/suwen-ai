from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RegistrationMode = Literal["closed", "invite", "open"]
RegistrationModeSource = Literal["REGISTRATION_MODE", "PUBLIC_REGISTRATION", "default"]


class TierConfig(BaseModel):
    """One model tier: a real, independently reachable OpenAI-compatible endpoint."""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    # CNY per 1M tokens; used for usage_log cost attribution
    input_price_per_m: float = 0.0
    output_price_per_m: float = 0.0
    # The gateway encodes this with DeepSeek's thinking object or Qwen's
    # enable_thinking flag. None preserves the provider's default mode.
    enable_thinking: bool | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    _registration_mode_source: RegistrationModeSource = PrivateAttr(default="default")

    @property
    def registration_mode_source(self) -> RegistrationModeSource:
        """Return only the setting name used, never its value."""
        return self._registration_mode_source

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
    # REGISTRATION_MODE is the canonical switch. PUBLIC_REGISTRATION remains
    # a compatibility input for older local/server env files and is normalized
    # to the canonical mode below; it is never used by application code.
    registration_mode: RegistrationMode | None = None
    public_registration: bool | None = None
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

    # --- learning model shadow flags (B3/B4/B5/B7) ---
    # These controls are intentionally independent from FSRS scheduling.  The
    # offline evaluator does not read or mutate application state, and every
    # online/shadow flag remains disabled unless an operator opts in.
    elo_shadow_enabled: bool = False
    afm_pfa_shadow_enabled: bool = False
    kc_edge_validation_enabled: bool = False
    practice_target_success_enabled: bool = False
    practice_target_success: float = Field(default=0.75, gt=0.0, lt=1.0)
    fsrs_desired_retention: float = Field(default=0.90, gt=0.0, le=1.0)

    # --- billing (0006 预付额度) ---
    # 扣费 = 模型真实成本 × billing_markup (Decimal 运算统一在 apps/api/wallet.py,
    # 本处 float 仅作配置载体, 使用前一律 Decimal(str(...)) 转换, 禁 float 直接参与账目)
    billing_markup: float = 3.0
    # 注册赠送额度 (CNY 计价 credit); 幂等由 wallet_ledger 唯一部分索引保证
    signup_grant_cny: float = 10.0
    # 支付通道: mock = 演示通道 (confirm 即到账, 仅非 prod); 真实通道留接口未实现
    payment_provider: str = "mock"
    # 人工核验的硅基流动邀请活动。点击只跳转，绝不自动发放站内额度。
    referral_campaign_enabled: bool = False
    siliconflow_referral_url: str = ""
    referral_reward_credits: float = 30.0

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
    harness_timeout_seconds: float = Field(default=180.0, gt=0, le=600)
    harness_max_concurrency: int = Field(default=8, ge=1, le=32)
    harness_max_model_calls: int = Field(default=16, ge=2, le=40)
    harness_context_window_tokens: int = Field(default=32768, ge=4096, le=262144)
    worker_max_jobs: int = Field(default=10, ge=1, le=64)
    plan_generation_mode: Literal["guided", "single", "team"] = "guided"
    agent_collaboration_enabled: bool = True
    agent_collaboration_max_agents: int = Field(default=8, ge=3, le=24)
    agent_collaboration_max_parallel: int = Field(default=3, ge=1, le=8)
    agent_collaboration_max_calls: int = Field(default=96, ge=12, le=600)
    agent_collaboration_max_cost_cny: float = Field(default=3.0, gt=0, le=100)
    agent_collaboration_max_revisions: int = Field(default=2, ge=0, le=5)
    agent_collaboration_timeout_seconds: float = Field(default=360.0, gt=0, le=1200)
    memory_semantic_mode: Literal["auto", "local", "disabled"] = "auto"
    memory_model_cache_dir: str = "~/.cache/eduagents/models"
    # Immutable prompt resources: select a new version for new tasks; existing
    # sessions keep the version and digest saved in their checkpoint.
    education_prompt_version: str = Field(default="edu-v1", pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    # Explicit business-route provider chains.  False preserves the legacy
    # CHEAP/STANDARD/STRONG/PLAN behavior for existing deployments.
    model_routing_enabled: bool = False
    trenddao_base_url: str = "https://trenddao.com/v1"
    trenddao_api_key: str = ""
    trenddao_text_model: str = "muse-spark-1.2-contributor"
    trenddao_vision_model: str = "muse-spark-1.2-contributor"
    trenddao_input_price_per_m: float = 0.0
    trenddao_output_price_per_m: float = 0.0

    # --- user-owned text model credentials (BYOK, 0017) ---
    # Disabled by default in local/test environments.  When enabled the
    # server must have a real 32-byte base64 key; no development fallback key
    # is accepted.
    user_model_credentials_enabled: bool = False
    user_model_credentials_encryption_key: str = ""
    user_model_credentials_key_version: int = Field(default=1, ge=1, le=2**31 - 1)
    deepseek_byok_base_url: str = "https://api.deepseek.com"
    deepseek_byok_allowed_models: str = "deepseek-chat,deepseek-reasoner"
    # Production keeps the admin fault-injection surface off unless an
    # operator explicitly enables it on a private management network.
    enable_fault_injection: bool = True

    # Learning Core second-wave gates.  Personal FSRS parameters and numeric
    # Explain-back quality are disabled by default; shadow artifacts may be
    # recorded without changing scheduling or the student-facing response.
    fsrs_personalization_enabled: bool = False
    fsrs_personalization_shadow_enabled: bool = True
    explain_back_quality_enabled: bool = False
    explain_back_quality_shadow_enabled: bool = True
    explain_back_quality_display_enabled: bool = False
    explain_back_quality_min_agreement: float = Field(default=0.8, ge=0.0, le=1.0)
    overcorrection_ranking_enabled: bool = False
    overcorrection_ranking_shadow_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def normalize_empty_route_prices(cls, values):
        """Allow optional price env vars to be left empty without changing
        the established empty-string semantics of unrelated secrets such as
        SMTP credentials.
        """
        if not isinstance(values, dict):
            return values
        defaults = {
            "trenddao_input_price_per_m": 0.0,
            "trenddao_output_price_per_m": 0.0,
        }
        for name, default in defaults.items():
            if values.get(name) == "":
                values[name] = default
        return values

    @model_validator(mode="after")
    def resolve_registration_mode(self) -> Settings:
        legacy_mode = None
        canonical_supplied = self.registration_mode is not None
        if self.public_registration is not None:
            legacy_mode = "open" if self.public_registration else "closed"
        if self.registration_mode is None:
            # Keep existing dev/test env files working, while production with
            # no registration setting fails closed.
            self.registration_mode = legacy_mode or ("open" if self.app_env != "prod" else "closed")
        elif legacy_mode is not None and legacy_mode != self.registration_mode:
            raise ValueError(
                "REGISTRATION_MODE conflicts with legacy PUBLIC_REGISTRATION; remove the legacy setting"
            )
        self._registration_mode_source = (
            "REGISTRATION_MODE"
            if canonical_supplied
            else "PUBLIC_REGISTRATION"
            if legacy_mode is not None
            else "default"
        )
        self.public_registration = self.registration_mode == "open"
        if self.referral_campaign_enabled:
            if not self.siliconflow_referral_url.startswith(
                "https://cloud.siliconflow.cn/"
            ):
                raise ValueError(
                    "enabled referral campaign requires an HTTPS cloud.siliconflow.cn URL"
                )
            if not 0 < self.referral_reward_credits <= 1000:
                raise ValueError("REFERRAL_REWARD_CREDITS must be within (0, 1000]")
        if self.user_model_credentials_enabled:
            try:
                key = base64.urlsafe_b64decode(
                    self.user_model_credentials_encryption_key.encode("ascii")
                )
            except (ValueError, UnicodeError, binascii.Error) as exc:
                raise ValueError(
                    "USER_MODEL_CREDENTIALS_ENCRYPTION_KEY must be base64"
                ) from exc
            if len(key) != 32:
                raise ValueError(
                    "USER_MODEL_CREDENTIALS_ENCRYPTION_KEY must decode to 32 bytes"
                )
            if not self.deepseek_byok_base_url.startswith("https://"):
                raise ValueError("DEEPSEEK_BYOK_BASE_URL must be HTTPS")
            if not any(item.strip() for item in self.deepseek_byok_allowed_models.split(",")):
                raise ValueError("DEEPSEEK_BYOK_ALLOWED_MODELS cannot be empty")
        if self.model_routing_enabled:
            route_urls = {
                "TRENDDAO_BASE_URL": self.trenddao_base_url,
            }
            for name, url in route_urls.items():
                if url and urlparse(url).scheme != "https":
                    raise ValueError(f"{name} must be HTTPS")
            route_prices = {
                "TRENDDAO_INPUT_PRICE_PER_M": self.trenddao_input_price_per_m,
                "TRENDDAO_OUTPUT_PRICE_PER_M": self.trenddao_output_price_per_m,
            }
            if any(price < 0 for price in route_prices.values()):
                raise ValueError("provider candidate prices cannot be negative")
            if self.app_env == "prod":
                missing_routes = [
                    route
                    for route, configured in (
                        ("text", self.routing_text_enabled),
                        ("vision", self.routing_vision_enabled),
                        ("plan", self.routing_plan_enabled),
                    )
                    if not configured
                ]
                if missing_routes:
                    raise ValueError(
                        "MODEL_ROUTING_ENABLED requires at least one configured "
                        f"candidate for routes: {', '.join(missing_routes)}"
                    )
        else:
            # Validate newly introduced endpoint settings even while the
            # feature is disabled, so an accidental later enable cannot turn
            # an HTTP endpoint into a credential-bearing production call.
            for name in (
                "trenddao_base_url",
            ):
                url = getattr(self, name)
                if url and urlparse(url).scheme != "https":
                    raise ValueError(f"{name.upper()} must be HTTPS")
        return self

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
    cheap_enable_thinking: bool | None = None

    standard_api_key: str = ""
    standard_base_url: str = "https://api.deepseek.com"
    standard_model: str = "deepseek-chat"
    standard_input_price_per_m: float = 2.0
    standard_output_price_per_m: float = 8.0
    standard_enable_thinking: bool | None = None

    # Multimodal chat attachments use the standard provider credentials and an
    # explicit VL model on that same OpenAI-compatible endpoint.
    vision_model: str = ""

    strong_api_key: str = ""
    strong_base_url: str = "https://api.deepseek.com"
    strong_model: str = "deepseek-reasoner"
    strong_input_price_per_m: float = 4.0
    strong_output_price_per_m: float = 16.0
    strong_enable_thinking: bool | None = None

    # Learning plans use a dedicated provider instead of sharing the generic
    # strong tier with thinking chat and other complex tasks.
    plan_api_key: str = ""
    plan_base_url: str = "https://api.deepseek.com"
    plan_model: str = "deepseek-chat"
    plan_input_price_per_m: float = 1.0
    plan_output_price_per_m: float = 2.0
    plan_enable_thinking: bool | None = None

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
    # stub: 离线确定性向量。live: SiliconFlow。ollama: 本地 BGE-M3。
    embedding_mode: Literal["live", "stub", "ollama"] = "stub"
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = Field(default=1024, ge=1)
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_chat_model: str = ""
    ollama_require_gpu: bool = False

    @property
    def retired_chunk_collection(self) -> str | None:
        if self.embedding_mode == "ollama":
            return self.qdrant_collection.removesuffix("_ollama_bge_m3_v1")
        return None

    @model_validator(mode="after")
    def configure_ollama_embeddings(self):
        if self.ollama_chat_model:
            self.model_routing_enabled = False
            self.gateway_replay_fallback_stub = False
            for tier in ("cheap", "standard", "strong", "plan"):
                setattr(self, f"{tier}_model", self.ollama_chat_model)
                setattr(self, f"{tier}_base_url", self.ollama_base_url.rstrip("/") + "/v1")
                setattr(self, f"{tier}_api_key", "ollama")
                setattr(self, f"{tier}_input_price_per_m", 0.0)
                setattr(self, f"{tier}_output_price_per_m", 0.0)
                setattr(self, f"{tier}_enable_thinking", None)
        if self.embedding_mode == "ollama":
            # A different embedding space needs its own index even at the same dimension.
            self.embedding_model = "bge-m3:latest"
            self.embedding_dim = 1024
            suffix = "_ollama_bge_m3_v1"
            if not self.qdrant_collection.endswith(suffix):
                self.qdrant_collection += suffix
        return self

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
    # ge=1: 0 不是"关闭"而是配置错误 —— 上限配置手误必须启动期拒绝,
    # 不能退化成无上限读入 (fail-closed)。tutor/resources 的 0=不限量是
    # 文档化语义, 不在此列。
    max_upload_mb: int = Field(default=50, ge=1)
    # Business-neutral PDF inspection/rendering limits.  Tutor may apply
    # tighter page-selection limits, but all PDF consumers share these safety
    # and classification thresholds.
    max_pdf_pages: int = 100
    pdf_native_min_chars: int = 180
    pdf_native_no_visual_min_chars: int = 60
    pdf_native_min_printable_ratio: float = 0.80
    pdf_native_min_text_coverage: float = 0.015
    pdf_body_min_char_ratio: float = 0.55
    pdf_mixed_image_area_ratio: float = 0.18
    pdf_mixed_image_count: int = 3
    pdf_render_dpi: int = 144
    pdf_max_render_pixels: int = 12_000_000
    pdf_max_render_mb: int = 6
    pdf_vision_max_pages: int = 12
    pdf_vision_concurrency: int = 2
    pdf_page_timeout_seconds: float = 45.0
    pdf_document_timeout_seconds: float = 300.0
    pdf_question_structuring_max_tokens: int = Field(default=8000, ge=1000, le=12000)
    # 同 max_upload_mb: 0 曾被解释为不限量, 变成 fail-open (review 36af54e)
    max_chat_attachment_mb: int = Field(default=20, ge=1)
    max_chat_attachments_per_message: int = 4
    max_chat_attachment_text_chars: int = 2_000_000
    # Tutor attachment extraction is intentionally bounded independently from
    # general learning resources because scanned pages consume vision quota.
    max_tutor_attachment_pages: int = 50
    max_tutor_default_pages: int = 3
    max_tutor_recognition_pages: int = 8
    max_tutor_ocr_dpi: int = 144
    max_tutor_ocr_image_mb: int = 4
    max_tutor_ocr_concurrency: int = 2
    tutor_ocr_timeout_seconds: float = 45.0
    tutor_recognition_timeout_seconds: float = 120.0
    tutor_draft_ttl_hours: int = 24
    max_tutor_recognition_attempts: int = 3
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
            enable_thinking=getattr(self, f"{name}_enable_thinking"),
        )

    @property
    def routing_text_enabled(self) -> bool:
        if not self.model_routing_enabled:
            return bool(self.standard_api_key and self.standard_model)
        return bool(self.trenddao_api_key and self.trenddao_base_url and self.trenddao_text_model)

    @property
    def routing_vision_enabled(self) -> bool:
        """Whether at least one explicitly configured visual route candidate exists."""
        if self.model_routing_enabled:
            return bool(self.trenddao_api_key and self.trenddao_base_url and self.trenddao_vision_model)
        return bool(self.vision_model)

    @property
    def routing_plan_enabled(self) -> bool:
        if not self.model_routing_enabled:
            return bool(self.plan_api_key and self.plan_model)
        return bool(self.trenddao_api_key and self.trenddao_base_url and self.trenddao_text_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
