from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from ..config import Settings
from ..gateway.base import ModelGateway


def append_messages(left: list[dict], right: list[dict]) -> list[dict]:
    """messages reducer: 追加式合并。消息 dict: {role, content}。"""
    return [*left, *right]


@dataclass
class GraphDeps:
    """子图外部依赖注入 (构图时闭包捕获, 不进 state — state 必须可序列化进 checkpointer)。

    web_search: async (query) -> list[{title, url, content}] | None (不可用时 None, 见 §9 Tavily 兜底)
    retrieval:  RetrievalService | None (未配置知识库栈时 None)
    """

    settings: Settings
    gateway: ModelGateway
    retrieval: Any = None
    web_search: Callable[..., Any] | None = None


class QaState(TypedDict, total=False):
    """qa 子图状态 (§5.3 Agentic RAG 自省环)。trace 事件由 API 层转发为 SSE trace_event。"""

    messages: Annotated[list[dict], append_messages]
    query: str
    tenant_id: str
    user_id: str
    request_id: str
    kb_id: str  # 空串 = 未绑定知识库 (联合检索时为 kb_ids[0], 路由判定用)
    kb_ids: list[str]  # Wave3 联合检索: 多库列表 (个人+机构), 检索层 MatchAny
    owner_ids: list[str]  # 上述库的 owner_id 集 (Qdrant tenant 纵深过滤)
    requested_route: str  # 用户显式选择: web | kb；空串时仍由规则/LLM 自动路由
    answer_tier: str  # standard | strong；只控制最终回答模型，不改变资料路由
    route: str  # kb | web | direct
    route_reason: str
    evidences: list[dict]  # RetrievedChunk 序列化: chunk_id/doc_id/text/heading_path/page_start/score
    web_results: list[dict]
    retry_count: int
    confident: bool
    grade_passed: bool
    answer: str
    refused: bool
    followups: list[str]  # 末节点追问建议 (至多 3 条, 失败降级为空; API 层发 followups 事件)
    profile_summary: str  # 学生画像摘要 (≤300 字, API 层 profile_summary.py 产出; 空=无画像)
    profile_weak_points: list[str]  # 上述摘要中的薄弱 KC 名 (进 trace, 供前端可见)
    minor: bool  # 未成年模式 (P0-5): generate 追加安全约束段, web 结果过审用 strict 阈值
    trace: Annotated[list[dict], operator.add]


class QuizState(TypedDict, total=False):
    """quiz 子图 (§5.4): blueprint → interrupt 确认 → 生成 → schema 校验重试 → 入库。"""

    tenant_id: str
    user_id: str
    request_id: str
    kb_id: str
    topic: str
    num_questions: int
    difficulty_mix: dict  # {"easy": n, "mid": n, "hard": n}
    kc_names: list[str]  # 定向薄弱点 (M4 画像接入后自动填充)
    blueprint: dict  # {items: [{kc_name, type, difficulty}], summary}
    blueprint_confirmed: bool
    questions: list[dict]  # 严格 schema 见 quiz_graph.QUESTION_SCHEMA
    validation_errors: list[str]
    retry_count: int
    trace: Annotated[list[dict], operator.add]


class PlanState(TypedDict, total=False):
    """planner 子图: 目标解析 → (摸底题由 quiz 复用) → 检索资源 → 生成计划 → interrupt 确认。"""

    tenant_id: str
    user_id: str
    request_id: str
    kb_id: str
    goal: str
    budget_hours_per_day: float
    total_days: int
    diagnosis: dict  # {kc_name: 掌握度初值} (M3 用自报+摸底简版)
    resources: list[dict]  # 知识库章节引用 / web 资源
    plan: dict  # {stages:[{name, days:[{day, tasks:[{title, minutes, resource_ref, kc_names}]}]}]}
    plan_confirmed: bool
    trace: Annotated[list[dict], operator.add]


class TutorState(TypedDict, total=False):
    """tutor 子图 (F4 苏格拉底): 三层提示状态机, hint_level 0..3, 3 之前禁止完整答案。"""

    messages: Annotated[list[dict], append_messages]
    tenant_id: str
    user_id: str
    request_id: str
    problem: str
    hint_level: int  # 0=开场提问 1=方向性 2=关键步骤 3=展开讲解(完整解答)
    answer_requests: int  # 学生索要答案次数: 第1次温和坚持, 第2次给答案
    stuck_type: str  # concept | calculation | misread | unknown
    reply: str
    session_summary: str  # 辅导小结 (写入画像, M4)
    trace: Annotated[list[dict], operator.add]
