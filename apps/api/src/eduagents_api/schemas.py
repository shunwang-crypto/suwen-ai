from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    created_at: datetime


class ChatRequestIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: uuid.UUID | None = None
    kb_id: uuid.UUID | None = None  # 绑定知识库 -> RAG 引用溯源问答


class RetrieveDebugIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


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


class DocumentOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    size_bytes: int
    status: str
    parse_backend: str
    error_msg: str | None
    version: int
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
