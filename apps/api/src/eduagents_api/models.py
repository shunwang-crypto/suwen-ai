from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _tenant_col() -> Mapped[uuid.UUID]:
    # 租户语义 (Wave3 泛化, decisions.md D14): tenant_id = owner_id —
    # knowledge_bases.owner_type='user' 时为 user_id, ='org' 时为 org_id;
    # 下游 documents/chunks 沿用其所属 kb 的 owner_id。列名保持 tenant_id 不改。
    return mapped_column(UUID(as_uuid=True), nullable=False, index=True)


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# RLS 生效表清单 (owner_id 语义): 迁移脚本据此启用 ENABLE + FORCE ROW LEVEL SECURITY。
# orgs/org_members 也启用 RLS, 但策略形状不同 (无 tenant_id 列), 由 0002 单独建。
TENANT_TABLES = [
    "knowledge_bases",
    "documents",
    "chunks",
    "knowledge_components",
    "kc_edges",
    "mastery_records",
    "mastery_log",
    "quizzes",
    "questions",
    "attempts",
    "error_items",
    "plans",
    "plan_adjustments",
    "chat_sessions",
    "chat_messages",
    "usage_log",
]


class Organization(Base):
    """M0 遗留占位表 (users.org_id 的 FK 目标), 已被 Wave3 的 Org/OrgMember 取代。
    保留仅为不动 users 表结构; 无任何读写路径, 见 decisions.md D14。"""

    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Org(Base):
    """机构 (企业后台共享知识库的所有者主体, Wave3)。

    id 直接作为机构库的 owner_id 落在 knowledge_bases.tenant_id 上 (owner_type='org'),
    因此 documents/chunks/Qdrant payload 的 tenant_id 传播链一行不改。
    """

    __tablename__ = "orgs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 8 位免混淆大写字母+数字; 邀请即凭码 join (无邮件邀请, P1)
    invite_code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    storage_quota_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=512)
    created_at: Mapped[datetime] = _created_at()


class OrgMember(Base):
    """机构成员。入会即挂载机构库 (无 mount 表): 成员可读, 仅 admin 可写。"""

    __tablename__ = "org_members"
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    joined_at: Mapped[datetime] = _created_at()


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="student")
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rotated_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created_at()


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()  # owner_id
    # 判别列: 'user' -> tenant_id 是 user_id; 'org' -> tenant_id 是 orgs.id
    owner_type: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="user", default="user"
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    kb_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 两阶段状态机: pending→parsing→chunking→embedding→ready→enriching→enriched | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    parse_backend: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 最小审计 (机构库演示"谁传的"); 存量行为 NULL, 不回填
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    context_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heading_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kc_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = _created_at()


class KnowledgeComponent(Base):
    __tablename__ = "knowledge_components"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = _created_at()


class KcEdge(Base):
    __tablename__ = "kc_edges"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    from_kc: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_components.id", ondelete="CASCADE")
    )
    to_kc: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_components.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String(24), nullable=False, default="prerequisite")


class MasteryRecord(Base):
    __tablename__ = "mastery_records"
    __table_args__ = (UniqueConstraint("user_id", "kc_id", name="uq_mastery_user_kc"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    kc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_components.id", ondelete="CASCADE")
    )
    p_mastery: Mapped[float] = mapped_column(Float, nullable=False, default=0.3)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fsrs_card_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MasteryLog(Base):
    __tablename__ = "mastery_log"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    kc_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    old_p: Mapped[float] = mapped_column(Float, nullable=False)
    new_p: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    at: Mapped[datetime] = _created_at()


class Quiz(Base):
    __tablename__ = "quizzes"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    kb_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    blueprint_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    created_at: Mapped[datetime] = _created_at()


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quizzes.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # single/multi/blank/short
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kc_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    difficulty: Mapped[str] = mapped_column(String(8), nullable=False, default="mid")


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE")
    )
    response: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    llm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    scored_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = _created_at()


class ErrorItem(Base):
    __tablename__ = "error_items"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE")
    )
    kc_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 错因五分类: concept_confusion/formula_error/calc_slip/misread/knowledge_gap
    cause: Mapped[str] = mapped_column(String(32), nullable=False, default="knowledge_gap")
    # 状态机: new→reviewing→mastered→archived
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    variant_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    fsrs_card_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    budget_hours: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    structure_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created_at()


class PlanAdjustment(Base):
    __tablename__ = "plan_adjustments"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    diff_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    at: Mapped[datetime] = _created_at()


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="新对话")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()


class UsageLog(Base):
    __tablename__ = "usage_log"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    feature: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="live")
    at: Mapped[datetime] = _created_at()


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    metrics_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    corpus_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    golden_set_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    at: Mapped[datetime] = _created_at()
