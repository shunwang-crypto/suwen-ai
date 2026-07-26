"""机构 (企业后台) 路由 — Wave3 共享知识库的所有者主体。

权限模型 (docs/research/brief-2-企业KB架构.md, D14):
- 建机构: 任何用户; 创建者自动 admin, 返回 8 位邀请码。
- 凭码 join: 成为 member (入会即挂载机构库, 无 mount 表)。
- 机构库: 仅 org admin 可建/可写; 成员只读可检索 (kb.py 双层拦截)。
- 邀请码查询是唯一"看见非己方 org 行"的路径: 事务内提升 app.is_admin GUC
  (复用 ops.py/usage_log 的 D7 惯例), 事务结束即失效。
"""

from __future__ import annotations

import secrets
import uuid

from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, invalidate_org_cache
from ..models import KnowledgeBase, Org, OrgMember, User
from ..schemas import KbCreate, KbOut, OrgCreate, OrgJoin, OrgMemberOut, OrgOut

router = APIRouter(prefix="/api/orgs", tags=["orgs"])
log = get_logger("orgs")

# 8 位免混淆邀请码字母表 (去 0/O/1/I/L)
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LEN = 8


def _new_invite_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))


async def _member_counts(session, org_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not org_ids:
        return {}
    rows = (
        await session.execute(
            select(OrgMember.org_id, func.count())
            .where(OrgMember.org_id.in_(org_ids))
            .group_by(OrgMember.org_id)
        )
    ).all()
    return {org_id: count for org_id, count in rows}


@router.post("", response_model=OrgOut, status_code=201)
async def create_org(
    body: OrgCreate, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    # 邀请码唯一冲突概率 ~31^-8, 撞上重试一次即可
    for attempt in range(2):
        code = _new_invite_code()
        try:
            async with tenant_session(ctx.tenant_id) as session:
                org = Org(name=body.name, invite_code=code, created_by=ctx.user_id)
                session.add(org)
                await session.flush()
                session.add(OrgMember(org_id=org.id, user_id=ctx.user_id, role="admin"))
                out = OrgOut(
                    id=org.id,
                    name=org.name,
                    invite_code=org.invite_code,
                    role="admin",
                    member_count=1,
                    storage_quota_mb=org.storage_quota_mb,
                    created_at=org.created_at,
                )
            break
        except IntegrityError:
            if attempt == 1:
                raise HTTPException(status_code=500, detail="INVITE_CODE_COLLISION") from None
    await invalidate_org_cache(request.app.state.redis, ctx.user_id)
    log.info("org_created", org_id=str(out.id), by=str(ctx.user_id))
    return out


@router.post("/join", response_model=OrgOut)
async def join_org(
    body: OrgJoin, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    code = body.invite_code.strip().upper()
    # is_admin GUC: 凭码定位 org 行 (用户尚非成员, 常规策略看不见该行, D14)
    async with tenant_session(ctx.tenant_id, is_admin=True) as session:
        org = await session.scalar(select(Org).where(Org.invite_code == code))
        if org is None:
            raise HTTPException(status_code=404, detail="INVITE_CODE_INVALID")
        existing = await session.get(OrgMember, (org.id, ctx.user_id))
        if existing is not None:
            raise HTTPException(status_code=409, detail="ALREADY_MEMBER")
        session.add(OrgMember(org_id=org.id, user_id=ctx.user_id, role="member"))
        member_count = (
            await session.scalar(
                select(func.count()).select_from(OrgMember).where(OrgMember.org_id == org.id)
            )
        ) or 1
        out = OrgOut(
            id=org.id,
            name=org.name,
            invite_code=None,  # member 不回显邀请码
            role="member",
            member_count=member_count,
            storage_quota_mb=org.storage_quota_mb,
            created_at=org.created_at,
        )
    await invalidate_org_cache(request.app.state.redis, ctx.user_id)
    log.info("org_joined", org_id=str(out.id), user=str(ctx.user_id))
    return out


@router.get("", response_model=list[OrgOut])
async def list_my_orgs(ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        memberships = (
            await session.execute(
                select(OrgMember.org_id, OrgMember.role).where(
                    OrgMember.user_id == ctx.user_id
                )
            )
        ).all()
        role_by_org = {org_id: role for org_id, role in memberships}
        if not role_by_org:
            return []
        orgs = (
            await session.scalars(
                select(Org).where(Org.id.in_(role_by_org)).order_by(Org.created_at)
            )
        ).all()
        counts = await _member_counts(session, list(role_by_org))
        return [
            OrgOut(
                id=o.id,
                name=o.name,
                invite_code=o.invite_code if role_by_org[o.id] == "admin" else None,
                role=role_by_org[o.id],
                member_count=counts.get(o.id, 1),
                storage_quota_mb=o.storage_quota_mb,
                created_at=o.created_at,
            )
            for o in orgs
        ]


@router.get("/{org_id}/members", response_model=list[OrgMemberOut])
async def list_members(org_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    if str(org_id) not in ctx.org_ids:
        # 非成员不泄漏机构存在性
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        rows = (
            await session.execute(
                select(OrgMember, User.email)
                .join(User, User.id == OrgMember.user_id)
                .where(OrgMember.org_id == org_id)
                .order_by(OrgMember.joined_at)
            )
        ).all()
        return [
            OrgMemberOut(
                user_id=m.user_id, email=email, role=m.role, joined_at=m.joined_at
            )
            for m, email in rows
        ]


@router.post("/{org_id}/kb", response_model=KbOut, status_code=201)
async def create_org_kb(
    org_id: uuid.UUID, body: KbCreate, ctx: AuthContext = Depends(api_rate_limited)
):
    """建机构库: 仅 org admin。owner_type='org', tenant_id=org_id (owner_id 语义, D14)。"""
    if str(org_id) not in ctx.org_ids:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if str(org_id) not in ctx.admin_org_ids:
        raise HTTPException(status_code=403, detail="ORG_ADMIN_REQUIRED")
    # 写事务以 org 为 owner 开启 (RLS 写策略 owner 匹配), org 名单独查作回显
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        org = await session.get(Org, org_id)
        org_name = org.name if org is not None else ""
    async with tenant_session(org_id) as session:
        kb = KnowledgeBase(
            tenant_id=org_id, owner_type="org", name=body.name, subject=body.subject
        )
        session.add(kb)
        await session.flush()
        return KbOut(
            id=kb.id,
            name=kb.name,
            subject=kb.subject,
            kb_version=kb.kb_version,
            created_at=kb.created_at,
            owner_type="org",
            org_id=org_id,
            org_name=org_name,
            writable=True,
        )
