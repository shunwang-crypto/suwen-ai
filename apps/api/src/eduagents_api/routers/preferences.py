"""用户学习偏好 (P2-3/P2-4, G8): GET/PUT /api/preferences。

- 一人一行 upsert user_preferences (user_id 即主键, models.UserPreference);
  PUT 是整资源替换语义 —— 缺省字段即清空, 设置页"清空全部偏好"= PUT 空体。
- explanation_style/verbosity 枚举白名单与 profile_summary 的标签词表同源
  (EXPLANATION_STYLES/VERBOSITY_LEVELS, 0005 迁移 CHECK 约束同口径)。
  风格枚举是内容组织维度 (严谨/口语/类比优先), 不给学习者贴任何感官类型标签 (该类分类已被证伪, 全仓禁用)。
- interests 归一化: 压缩空白、去空项、按序去重; 归一后 ≤8 项且单项 ≤20 字,
  超限 422 (前端 chips 同限, 正常路径到不了这里)。
- PUT 成功后立即失效 profile_summary 的 Redis 缓存 (裸 key + 全部 kb 指纹桶):
  偏好一保存, 下一次 QA/quiz/tutor 的画像段立即体现 (60s TTL 等不起演示即时性)。
- 偏好属于用户自报数据, 只进本人 prompt (P2-7 口径); 注销随 users FK 级联删,
  导出已纳入 worker/export.py (P2-1 地基包)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_redis
from ..models import UserPreference
from ..profile_summary import (
    EXPLANATION_STYLES,
    VERBOSITY_LEVELS,
    invalidate_profile_summary_cache,
)

router = APIRouter(prefix="/api/preferences", tags=["preferences"])

INTERESTS_MAX = 8  # 兴趣标签数量上限 (brief-5 P2-3)
INTEREST_CHAR_MAX = 20  # 单个兴趣标签字数上限


class PreferencesIn(BaseModel):
    explanation_style: str | None = None
    verbosity: str | None = None
    # 去重前预留余量 (归一化后再限 8 项); 单项超长在 normalize_interests 里 422
    interests: list[str] = Field(default_factory=list, max_length=64)
    daily_minutes: int | None = None
    goal: str | None = Field(default=None, max_length=512)


def normalize_interests(raw: list[str]) -> list[str]:
    """压缩空白 + 去空项 + 按序去重; 归一后超限 (项数/单项字数) 抛 422。"""
    items: list[str] = []
    for it in raw:
        s = " ".join(str(it).split())  # 首尾去空白, 内部空白压缩为单空格
        if not s or s in items:
            continue
        if len(s) > INTEREST_CHAR_MAX:
            raise HTTPException(status_code=422, detail="INTEREST_TOO_LONG")
        items.append(s)
    if len(items) > INTERESTS_MAX:
        raise HTTPException(status_code=422, detail="TOO_MANY_INTERESTS")
    return items


def _validate(body: PreferencesIn) -> tuple[str | None, str | None, list[str], int | None, str | None]:
    """入参 -> 归一化字段元组; 枚举/范围违规抛 422。"""
    style = body.explanation_style or None
    if style is not None and style not in EXPLANATION_STYLES:
        raise HTTPException(status_code=422, detail="INVALID_EXPLANATION_STYLE")
    verbosity = body.verbosity or None
    if verbosity is not None and verbosity not in VERBOSITY_LEVELS:
        raise HTTPException(status_code=422, detail="INVALID_VERBOSITY")
    minutes = body.daily_minutes
    if minutes is not None and not (5 <= minutes <= 1440):
        raise HTTPException(status_code=422, detail="INVALID_DAILY_MINUTES")
    goal = (body.goal or "").strip() or None
    if goal is not None and len(goal) > 200:  # 列宽 String(200) 同口径
        raise HTTPException(status_code=422, detail="GOAL_TOO_LONG")
    return style, verbosity, normalize_interests(body.interests), minutes, goal


def _row_out(row: UserPreference | None) -> dict:
    """行 -> 出参; 无行 (从未设置) 返回全空缺省, 前端不必区分 404。"""
    if row is None:
        return {
            "explanation_style": None,
            "verbosity": None,
            "interests": [],
            "daily_minutes": None,
            "goal": None,
            "personalization_enabled": True,
            "updated_at": None,
        }
    return {
        "explanation_style": row.explanation_style,
        "verbosity": row.verbosity,
        "interests": [str(x) for x in row.interests or []],
        "daily_minutes": row.daily_minutes,
        "goal": row.goal,
        "personalization_enabled": bool(row.personalization_enabled),
        "updated_at": row.updated_at,
    }


@router.get("")
async def get_preferences(ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        row = await session.get(UserPreference, ctx.user_id)
        return _row_out(row)


@router.put("")
async def put_preferences(
    body: PreferencesIn,
    ctx: AuthContext = Depends(api_rate_limited),
    redis=Depends(get_redis),
):
    """整资源 upsert; 成功后失效 profile_summary 缓存, 画像段立即反映新偏好。

    personalization_enabled 不在替换语义内 (0031): 保存偏好不动记忆总开关,
    开关只走 POST /api/preferences/personalization, 避免整包保存误关个性化。
    """
    style, verbosity, interests, minutes, goal = _validate(body)
    async with tenant_session(ctx.tenant_id) as session:
        row = await session.get(UserPreference, ctx.user_id)
        if row is None:
            row = UserPreference(
                user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                explanation_style=style,
                verbosity=verbosity,
                interests=interests,
                daily_minutes=minutes,
                goal=goal,
            )
            session.add(row)
        else:
            row.explanation_style = style
            row.verbosity = verbosity
            row.interests = interests
            row.daily_minutes = minutes
            row.goal = goal
        await session.flush()
        await session.refresh(row)  # 取回 server 端 updated_at (server_default/onupdate)
        payload = _row_out(row)
    # 事务提交后再失效缓存: 避免"删了缓存又回滚"导致旧值回填窗口
    await invalidate_profile_summary_cache(redis, ctx.user_id)
    return payload


class PersonalizationToggle(BaseModel):
    """记忆总开关 (0031): 关 = 停读画像段; 聊天历史照存, 再开即恢复。"""

    enabled: bool


@router.post("/personalization")
async def set_personalization(
    body: PersonalizationToggle,
    ctx: AuthContext = Depends(api_rate_limited),
    redis=Depends(get_redis),
):
    """切换画像注入总开关; 成功后失效 profile_summary 缓存 (下一条消息立即生效)。

    豁免清单 (设计红线, 永不受此开关影响): 未成年判定、内容审核、掌握度计算。
    关闭不删除任何画像数据 (关闭 ≠ 删除, 同 ChatGPT 语义)。
    """
    async with tenant_session(ctx.tenant_id) as session:
        row = await session.get(UserPreference, ctx.user_id)
        if row is None:
            row = UserPreference(
                user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                personalization_enabled=body.enabled,
            )
            session.add(row)
        else:
            row.personalization_enabled = body.enabled
        await session.flush()
        await session.refresh(row)
        payload = _row_out(row)
    await invalidate_profile_summary_cache(redis, ctx.user_id)
    return payload
