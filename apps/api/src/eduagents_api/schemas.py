from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from .email_utils import normalize_email


def _validate_bcrypt_password_bytes(v: str) -> str:
    """P2-16: bcrypt 上限 72 字节, 超限在 schema 层 422 (中文提示),
    而非 hash 时 ValueError 变 500 (25 个汉字即触发)。"""
    if len(v.encode("utf-8")) > 72:
        raise ValueError("密码过长: 折算后超过 72 字节 (约 24 个汉字或 72 个英文字符), 请缩短")
    return v


def _normalize_display_name(v: str | None) -> str | None:
    if v is None:
        return None
    normalized = " ".join(v.split())
    if not normalized:
        raise ValueError("姓名或昵称不能为空")
    return normalized


class RegisterRequest(BaseModel):
    email: EmailStr
    email_code: str | None = Field(default=None, min_length=6, max_length=6, pattern=r"^\d{6}$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    # 图形验证码 (captcha_required_register 开启时必填; 测试通道见 config.py 注释)
    captcha_id: str | None = Field(default=None, max_length=64)
    captcha_answer: str | None = Field(default=None, max_length=8)
    invite_code: str | None = Field(default=None, max_length=128)

    _password_bytes = field_validator("password")(_validate_bcrypt_password_bytes)
    _display_name = field_validator("display_name")(_normalize_display_name)
    _email = field_validator("email", mode="before")(normalize_email)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    # 同邮箱连续失败 >=3 次后要求验证码 (防爆破); 平时无需携带
    captcha_id: str | None = Field(default=None, max_length=64)
    captcha_answer: str | None = Field(default=None, max_length=8)

    _email = field_validator("email", mode="before")(normalize_email)


class RegisterEmailCodeRequest(BaseModel):
    email: EmailStr
    captcha_id: str = Field(min_length=1, max_length=64)
    captcha_answer: str = Field(min_length=1, max_length=8)

    _email = field_validator("email", mode="before")(normalize_email)


class RegistrationInviteCreate(BaseModel):
    expires_in_hours: int | None = Field(default=None, ge=1, le=24 * 365)
    max_uses: int = Field(default=1, ge=1, le=100)


class RegistrationInviteBatchCreate(RegistrationInviteCreate):
    count: int = Field(default=10, ge=1, le=100)


class RegistrationInviteOut(BaseModel):
    id: uuid.UUID
    code: str | None = None
    status: Literal["active", "used", "revoked", "expired"]
    expires_at: datetime | None
    max_uses: int
    used_count: int
    created_at: datetime
    used_by: uuid.UUID | None = None
    used_by_email: str | None = None


class PublicRegistrationConfigOut(BaseModel):
    registration_mode: Literal["closed", "invite", "open"]
    email_verification_required: bool


class ForgotPasswordIn(BaseModel):
    """密码找回申请: 恒定 204 (防枚举), 验证码必填 (防批量骚扰发信)。"""

    email: EmailStr
    captcha_id: str = Field(min_length=1, max_length=64)
    captcha_answer: str = Field(min_length=1, max_length=8)

    _email = field_validator("email", mode="before")(normalize_email)


class ResetPasswordIn(BaseModel):
    """凭邮件里的一次性 token 改密。"""

    token: str = Field(min_length=16, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    _password_bytes = field_validator("new_password")(_validate_bcrypt_password_bytes)


class AccountDeleteIn(BaseModel):
    """注销 (P0-4): 必须重输密码确认, 防拿到落锁电脑/被盗 token 的人一键销号。"""

    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None = None
    role: str
    created_at: datetime
    email_verified_at: datetime | None = None


class AdminUserOut(BaseModel):
    """Minimal user record for administrator operations; never includes secrets."""

    user_id: uuid.UUID
    email: str
    role: str
    status: str
    wallet_balance: str
    created_at: datetime


class AdminUserListItemOut(AdminUserOut):
    email_verified: bool
    last_login_at: datetime | None = None
    last_active_at: datetime | None = None
    today_platform_cost_cny: str = "0.000000"
    total_platform_cost_cny: str = "0.000000"
    platform_call_count: int = 0
    byok_call_count: int = 0
    knowledge_base_count: int = 0
    error_count: int = 0
    plan_count: int = 0


class AdminUserPageOut(BaseModel):
    items: list[AdminUserListItemOut]
    page: int
    page_size: int
    total: int
    has_more: bool


class AdminModelCredentialSummaryOut(BaseModel):
    configured: bool
    provider: str | None = None
    enabled: bool | None = None
    fallback_to_platform: bool | None = None
    key_last_four: str | None = None
    normal_model: str | None = None
    reasoning_model: str | None = None


class AdminUserDetailOut(AdminUserListItemOut):
    model_credential: AdminModelCredentialSummaryOut
    recent_admin_adjustments: list[dict]


_ADMIN_DELTA_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d{1,4})?$")
_ADMIN_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class AdminWalletAdjustIn(BaseModel):
    delta: Decimal
    note: str = Field(min_length=1, max_length=256)
    reference: str = Field(min_length=1, max_length=128)

    @field_validator("delta", mode="before")
    @classmethod
    def validate_delta_string(cls, value: object) -> Decimal:
        if not isinstance(value, str):
            raise ValueError("调额必须使用字符串金额")
        raw = value.strip()
        if not _ADMIN_DELTA_RE.fullmatch(raw):
            raise ValueError("金额必须是最多四位小数的数字")
        try:
            delta = Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError("金额格式不合法") from exc
        if delta == 0:
            raise ValueError("调额不能为 0")
        if abs(delta) > Decimal("1000"):
            raise ValueError("单次调额范围为 -1000 至 1000")
        return delta

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("调额原因不能为空")
        return normalized

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        normalized = value.strip()
        if not _ADMIN_REFERENCE_RE.fullmatch(normalized):
            raise ValueError("业务凭证只能包含字母、数字、点、下划线、冒号、斜线和连字符")
        return normalized


class AdminWalletAdjustOut(BaseModel):
    user_id: uuid.UUID
    previous_balance: str
    delta: str
    balance: str
    note: str
    reference: str


class AccountProfileUpdateIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)

    _display_name = field_validator("display_name")(_normalize_display_name)


class ChatRequestIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: uuid.UUID | None = None
    # 显式回答模式。旧客户端未发送 mode 且携带 kb_id(s) 时，路由层仍按知识库模式处理。
    mode: Literal["fast", "think", "web", "research", "kb"] = "fast"
    kb_id: uuid.UUID | None = None  # 向后兼容: 等价 kb_ids=[kb_id]
    # Wave3 联合检索: 多库显式列表 (个人库+机构库), 服务端逐库校验可见性
    kb_ids: list[uuid.UUID] | None = Field(default=None, max_length=8)
    attachment_ids: list[uuid.UUID] | None = None


class ChatAttachmentOut(BaseModel):
    id: uuid.UUID
    filename: str
    mime_type: str
    kind: str
    size_bytes: int
    created_at: datetime


class RetrieveDebugIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class UrlImportIn(BaseModel):
    """URL 网页导入 (对标 ima 剪藏): 协议/内网校验在 parsing/web.py 侧完成。"""

    url: str = Field(min_length=8, max_length=2048)


class SessionOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class SessionRename(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    # P0-6: meta_json 透出 (truncated/blocked/mode/citations 等), 前端据
    # truncated 显示"回答未完成"角标与重问入口
    meta: dict = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    request_id: str


class KbCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    subject: str = Field(default="general", max_length=64)


class KbOut(BaseModel):
    id: uuid.UUID
    name: str
    subject: str
    kb_version: int
    created_at: datetime
    # Wave3 机构库标识: owner_type='org' 时带 org 名; writable=当前用户可写
    owner_type: str = "user"
    org_id: uuid.UUID | None = None
    org_name: str | None = None
    writable: bool = True


# ---- 机构 (Wave3 企业后台共享知识库) ----


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class OrgJoin(BaseModel):
    invite_code: str = Field(min_length=4, max_length=16)


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    invite_code: str | None = None  # 仅 admin 可见 (member 视图为 None)
    role: str
    member_count: int
    storage_quota_mb: int
    created_at: datetime


class OrgMemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str
    joined_at: datetime


class DocumentOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    size_bytes: int
    status: str
    parse_backend: str
    error_msg: str | None
    recognition_meta_json: dict = Field(default_factory=dict)
    version: int
    uploaded_by: uuid.UUID | None = None  # 最小审计 (机构库"谁传的"); 存量行 None
    created_at: datetime
    updated_at: datetime


class ChunkOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    text: str
    heading_path: str
    page_start: int | None
    page_end: int | None
    kc_ids: list
    embedding_status: str


class ChunkContextItem(BaseModel):
    id: uuid.UUID
    text: str
    heading_path: str
    page_start: int | None
    page_end: int | None


class ChunkContextOut(BaseModel):
    """引用回原文定位 (包A): 命中块 + 同 parent 相邻子块 + 父块全文。"""

    chunk: ChunkContextItem
    doc_id: uuid.UUID
    doc_name: str
    prev: list[ChunkContextItem]
    next: list[ChunkContextItem]
    parent_text: str | None
    parent_heading_path: str | None
