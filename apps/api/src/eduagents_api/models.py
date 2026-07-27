from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
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


# user_id 外键 (0003, P0-4 注销可执行): attempts/chat_sessions/error_items/mastery_log/
# mastery_records/plans/quizzes/org_members 的 user_id 在 DB 层有 FK(users.id)
# ON DELETE CASCADE; usage_log 为 ON DELETE SET NULL (审计留档, 决策见 0003 迁移头注释)。
# ORM 列保持裸 UUID 不声明 ForeignKey: 0001 从零重放用 create_all(当前元数据),
# 若在 ORM 声明会在 0001 就建出约束, 与 0003 的 create_foreign_key 重复。

# RLS 生效表清单 (owner_id 语义): 迁移脚本据此启用 ENABLE + FORCE ROW LEVEL SECURITY。
# orgs/org_members 也启用 RLS, 但策略形状不同 (无 tenant_id 列), 由 0002 单独建。
# 注意: 本清单被 0001 迭代建策略, 0001 之后新建的租户表 (feedback/learner_insights/
# user_preferences) **不得**追加进来 —— 从零重放时 0001 执行会撞"表不存在";
# 它们的 ENABLE+FORCE+策略由各自迁移 (0004/0005) 按同一 GUC 口径单独建。
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
    # 账号生命周期 (0003, P1-3 封禁): active|disabled; 非 active 一律 403 ACCOUNT_DISABLED
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    # 未成年人保护 (0003, P0-5 未成年线消费): 出生年份声明 + 监护人同意
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    guardian_consent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # 套餐骨架 (0003, P2-14 预算线消费): free|pilot|...; override 非空时覆盖全局日预算
    plan: Mapped[str] = mapped_column(
        String(16), nullable=False, default="free", server_default="free"
    )
    daily_budget_cny_override: Mapped[float | None] = mapped_column(Float, nullable=True)
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


class PasswordResetToken(Base):
    """密码找回 token (原文只进邮件链接, 库里只存 sha256, 同 refresh_tokens 口径)。

    协调约定 (计费线 x 认证线, 全仓唯一定义在此): 表由计费线迁移 0006 以
    ORM 子集 create_all 建出, 本定义即建表结构; 认证线 (routers/auth.py) 只
    import 使用, 不写迁移。0001 的建表清单已冻结 (_TABLES_AT_0001), 从零重放
    不会提前建出本表; 新表可直接声明 FK (同 learner_insights, 见 0003 头注释)。

    RLS (0006): ENABLE+FORCE, 仅服务层策略 prt_service_all — 读写都要求事务内
    set_config('app.billing_service', 'true', true); 用户侧无任何直读路径。
    单次性: used_at 非空即作废; 同用户重新申请时旧未用行直接删除。
    """

    __tablename__ = "password_reset_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    # 0003: FK(users.id) ON DELETE SET NULL — 注销后成本行留档但断开用户关联 (可空)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), index=True, nullable=True
    )
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


class LearnerInsight(Base):
    """学习者长期记忆条目 (P2-1, G10): 补上"他在什么语境下困惑"。

    现状缺口: Reflection 与 tutor 小结只往 BKT 留数字, 学生原话/误解描述全被丢弃 —
    本表把匹配上 KC 词表的洞察证据落库, 是 F8-A2"新会话主动关联历史卡点"的地基。
    - source: chat_reflection (对话洞察, kind=confused|understood, text=学生原话)
            | tutor_summary (辅导小结, kind=misconception, text=误解描述)
    - text ≤600 字, 写入口统一截断 (services.persist_learner_insights);
    - 每用户只保留最近 INSIGHTS_KEEP_PER_USER=100 条, 超出删最旧 (同一写入口);
    - kc_id 可空: tutor 误解文本匹配不上词表也如实保留原文 (不新建 KC, F5-A2)。
    - 用户可见可删 (GET/DELETE /api/profile/insights, 用户纠正权);
    - PIPL: user_id FK CASCADE 随注销级联删; 导出纳入 worker/export.py。
    新表可直接在 ORM 声明外键: 0001 的建表范围已冻结, 本表只由 0005 create_all
    建出, 不存在 0003 注释里"重复建约束"的问题。
    索引: 0005 建 (user_id, created_at DESC) 复合索引, 前缀即覆盖 user_id 查询。
    """

    __tablename__ = "learner_insights"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # KC 删除时置空保留学生原话 (记忆属于用户, 不随知识库结构陪葬)
    kc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_components.id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()


class UserPreference(Base):
    """用户学习偏好 (P2-1/P2-3, G8): 注入 QA/quiz/tutor 画像段的确定性来源。

    - explanation_style: rigorous|colloquial|analogy (严谨/口语/类比优先) —
      **内容组织维度**, 不是任何感官类型分类 (该类说法已被证伪, 全仓禁用);
    - verbosity: concise|detailed; interests: list[str] ≤8 项/单项 ≤20 字 (应用层校验);
    - 一人一行 (user_id 即主键), FK CASCADE 随注销级联删; 导出纳入 worker/export.py。
    """

    __tablename__ = "user_preferences"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    explanation_style: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verbosity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    interests: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    daily_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goal: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PendingCleanup(Base):
    """删除 outbox (0003, P1-5): 业务删除与清理任务同事务提交, 消灭"PG 已删、
    ARQ 没入队"的永久孤儿。worker cron (process_pending_cleanup) 每分钟消费。

    kind: doc (删文档) | kb (删库) | tenant (注销)。payload 按 kind 携带
    tenant_id/kb_id/doc_id/filename/session_ids。无 RLS: 内部运维表, API 只在
    删除事务内写入, worker 全表扫描, 无任何用户读路径。
    """

    __tablename__ = "pending_cleanup"
    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    metrics_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    corpus_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    golden_set_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    at: Mapped[datetime] = _created_at()


# ---------- 预付额度计费 (0006) ----------
# 四表由 0006 迁移 create_all 建出 (新表可直接声明 FK, 同 0005 头注释口径);
# 不追加进 TENANT_TABLES (0001 策略循环已冻结), RLS 由 0006 单独建:
# 读 = 本人 (app.tenant_id GUC); 写 = 仅服务层 (app.billing_service GUC,
# WalletService 在事务内 set_config 标记, 见 wallet.py)。金额一律 NUMERIC(12,4)
# + Python Decimal, 禁 float 参与账目运算。


class WalletAccount(Base):
    """预付钱包 (一人一行, user_id 即主键): balance = CNY 计价的剩余额度。

    - 允许透支为负: 进行中的调用不打断 (扣费在 usage_log 同事务落账),
      余额 <=0 由下一次请求的网关入口 precheck 拦截 (402 INSUFFICIENT_CREDITS);
    - 注销: FK CASCADE 随 users 行删除 (FK 级联内部操作不受 RLS 约束)。
    """

    __tablename__ = "wallet_accounts"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, default=Decimal("0"), server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WalletLedger(Base):
    """钱包流水 (append-only: RLS 无 UPDATE/DELETE 策略 + 0006 REVOKE 双保险)。

    - kind 白名单 CHECK: signup_grant/consume/recharge/refund/admin_adjust;
    - ref: consume=request_id / recharge=order_id (幂等凭证);
    - 幂等唯一部分索引 (0006): (user_id) WHERE kind='signup_grant' 注册赠送只发一次;
      (user_id, ref) WHERE kind='consume' 同 request_id 重试不双扣;
    - 读路径索引 (user_id, created_at DESC): 本人最近 N 条 (同 0005 惯例)。
    """

    __tablename__ = "wallet_ledger"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    delta: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = _created_at()


class PaymentOrder(Base):
    """充值订单: status 状态机 pending→paid|failed|expired (CHECK 白名单, 0006)。

    幂等: mock-pay/回调入账前必须 SELECT ... FOR UPDATE 且 status='pending' 才能
    过渡到 paid 并调 recharge_apply — 重复确认返回 409, 不双入账。
    """

    __tablename__ = "payment_orders"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount_cny: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    credits: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    provider: Mapped[str] = mapped_column(
        String(24), nullable=False, default="mock", server_default="mock"
    )
    provider_txn_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# (password_reset_tokens 的 ORM 定义在上方 RefreshToken 之后 — 认证线先落盘,
#  计费线 0006 引用同一定义建表; 此处不再重复定义, 见该类 docstring 协调注释。)
