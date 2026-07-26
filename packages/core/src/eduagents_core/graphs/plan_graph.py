"""planner 子图 (F3 学习规划, M3 简版)。

流程: parse_goal → diagnose (摸底简版) → gather_resources → draft_plan (硬校验) → confirm_plan (interrupt)。

设计要点:
- 全部 LLM 调用经 deps.gateway (json_mode), 解析失败重试一次, 仍失败走确定性降级并写 trace,
  节点不向图外抛裸异常 (降级不是冒充: trace 里明确记录 fallback 事件);
- 计划合规性 (总天数 / 每日分钟预算 / resource_ref 越界 / KC 先后顺序) 由程序硬校验修正,
  不依赖模型自觉; 每项修正都以 event="fix" 写入 trace (F3-A1/A2);
- confirm_plan 用 langgraph interrupt() 挂起, 等前端 Command(resume=...) 确认 (F3: 生成后供用户编辑确认)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..gateway.base import ChatMessage, ChatRequest, Tier

# 容错 JSON 解析 (整体 -> 截取首尾大括号), 与 KC 抽取共用同一套宽容策略
from ..kc.extraction import _parse_json_object
from .state import GraphDeps, PlanState

# 本文件位于 packages/core/src/eduagents_core/graphs/ -> parents[5] 为仓库根
_PROMPTS_DIR = Path(__file__).resolve().parents[5] / "prompts"
_MAX_ATTEMPTS = 2  # 首次 + 重试一次
_DEFAULT_MASTERY = 0.3  # 无证据时的保守掌握度初值
_DAY_MIN_RATIO = 0.7  # 每日任务分钟和下限 = 0.7 * 预算
_DAY_MAX_RATIO = 1.15  # 上限 = 1.15 * 预算
_TOPIC_LIMIT = 5  # gather_resources 逐 topic 检索的 topic 上限
_KB_TOP_K = 3  # 每 topic 取知识库 top3
_WEB_TOP_K = 2  # 每 topic 取 web top2


class PlanGraphState(PlanState, total=False):
    """PlanState 的图内扩展通道 (契约 state.py 不动)。

    parsed_goal: parse_goal 节点产物 {"subject", "key_topics", "level_hint"};
    kc_order: 主题先后约束 (list[str]), M3 由调用方注入。
        TODO(M4): 应来自 KC 先修 DAG 的 kc.dag.topological_order(kc_ids, prerequisite_edges);
        GraphDeps 目前无 kc_edges 字段, 故 M3 简化为 state 直接注入, 拓扑约束 M4 接真实 DAG。
    """

    parsed_goal: dict
    kc_order: list[str]


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


async def _chat_json(
    deps: GraphDeps,
    state: dict,
    *,
    feature: str,
    prompt: str,
    tier: Tier,
    temperature: float = 0.2,
) -> dict[str, Any] | None:
    """cheap/standard 档 json_mode 调用: 解析失败自动重试一次, 仍失败返回 None (调用方降级)。"""
    request = ChatRequest(
        messages=(ChatMessage(role="user", content=prompt),),
        tier=tier,
        temperature=temperature,
        json_mode=True,
        feature=feature,
        tenant_id=state.get("tenant_id", ""),
        user_id=state.get("user_id", ""),
        request_id=state.get("request_id", ""),
    )
    for _ in range(_MAX_ATTEMPTS):
        result = await deps.gateway.chat(request)
        data = _parse_json_object(result.text)
        if data is not None:
            return data
    return None


# ---------------------------------------------------------------- 计划硬校验 (纯函数, 可单测)


def _coerce_task(raw: object) -> dict | None:
    """任务字段容错落地; 非 dict 直接丢弃。"""
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    title = title.strip() if isinstance(title, str) and title.strip() else "学习任务"
    try:
        minutes = int(round(float(raw.get("minutes", 0))))
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        minutes = 30  # 无效分钟给中性值, 随后由日预算缩放兜底
    raw_kcs = raw.get("kc_names")
    kc_names = (
        [k.strip() for k in raw_kcs if isinstance(k, str) and k.strip()]
        if isinstance(raw_kcs, list)
        else []
    )
    ref = raw.get("resource_ref")
    resource_ref = ref if isinstance(ref, int) and not isinstance(ref, bool) else None
    return {"title": title, "minutes": minutes, "kc_names": kc_names, "resource_ref": resource_ref}


def _coerce_plan(data: object) -> dict | None:
    """LLM 草稿结构化落地: stages/days/tasks 逐层容错; 完全不可用返回 None。"""
    if not isinstance(data, dict):
        return None
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list):
        return None
    stages: list[dict] = []
    for idx, raw_stage in enumerate(raw_stages):
        if not isinstance(raw_stage, dict):
            continue
        raw_days = raw_stage.get("days")
        days: list[dict] = []
        for raw_day in raw_days if isinstance(raw_days, list) else []:
            if not isinstance(raw_day, dict):
                continue
            raw_tasks = raw_day.get("tasks")
            tasks = [
                task
                for task in (
                    _coerce_task(t) for t in (raw_tasks if isinstance(raw_tasks, list) else [])
                )
                if task is not None
            ]
            days.append({"day": 0, "tasks": tasks})  # day 编号统一由 _renumber_days 重排
        if not days:
            continue
        name = raw_stage.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else f"阶段{idx + 1}"
        stages.append({"name": name, "days": days})
    if not stages:
        return None
    return {"stages": stages}


def _fallback_plan(state: dict, total_days: int, budget_minutes: float) -> dict:
    """LLM 草稿两次解析失败后的确定性降级: 按 diagnosis 主题轮转铺满天数。

    这不是冒充模型输出 — draft_plan 会在 trace 里记录 draft_fallback, 供上层提示降级。
    """
    diagnosis = state.get("diagnosis") or {}
    topics = list(diagnosis) or [state.get("goal") or "自主学习"]
    days = []
    for day in range(1, total_days + 1):
        topic = topics[(day - 1) % len(topics)]
        days.append(
            {
                "day": day,
                "tasks": [
                    {
                        "title": f"学习: {topic}",
                        "minutes": max(int(round(budget_minutes)), 1),
                        "kc_names": [topic] if topic in diagnosis else [],
                        "resource_ref": None,
                    }
                ],
            }
        )
    return {"stages": [{"name": "基础学习", "days": days}]}


def _iter_days(plan: dict) -> list[dict]:
    return [day for stage in plan["stages"] for day in stage["days"]]


def _fix_total_days(plan: dict, total_days: int, budget_minutes: float, fixes: list[dict]) -> None:
    """总天数硬校验: 多则从尾部裁, 少则在末阶段补"综合复习"填充日。"""
    count = len(_iter_days(plan))
    if count == total_days:
        return
    if count > total_days:
        to_remove = count - total_days
        for stage in reversed(plan["stages"]):
            while stage["days"] and to_remove:
                stage["days"].pop()
                to_remove -= 1
        plan["stages"] = [s for s in plan["stages"] if s["days"]]
        fixes.append({"fix": "total_days_trimmed", "from": count, "to": total_days})
    else:
        filler = [
            {
                "day": 0,
                "tasks": [
                    {
                        "title": "综合复习与查漏补缺",
                        "minutes": max(int(round(budget_minutes)), 1),
                        "kc_names": [],
                        "resource_ref": None,
                    }
                ],
            }
            for _ in range(total_days - count)
        ]
        if plan["stages"]:
            plan["stages"][-1]["days"].extend(filler)
        else:
            plan["stages"].append({"name": "综合复习", "days": filler})
        fixes.append({"fix": "total_days_padded", "from": count, "to": total_days})


def _renumber_days(plan: dict) -> None:
    for number, day in enumerate(_iter_days(plan), start=1):
        day["day"] = number


def _fix_kc_order(plan: dict, kc_order: list[str], fixes: list[dict]) -> None:
    """KC 先后约束 (F3-A2 简版): 任务按其 kc 在 kc_order 中的最小序稳定重排, 违序任务被顺延。

    只交换"带已排序 KC 的任务"所占的槽位, 无约束任务与日结构 (每天任务数) 保持不变;
    分钟数随任务移动, 因此本修正必须发生在每日分钟缩放之前。
    """
    if not kc_order:
        return
    rank_of = {name: i for i, name in enumerate(kc_order)}

    def rank(task: dict) -> int | None:
        ranks = [rank_of[k] for k in task["kc_names"] if k in rank_of]
        return min(ranks) if ranks else None

    slots: list[tuple[list, int]] = []  # (所在日的 tasks 列表, 下标), 按时间顺序
    for day in _iter_days(plan):
        for i in range(len(day["tasks"])):
            slots.append((day["tasks"], i))
    ranked_slots = [(lst, i) for lst, i in slots if rank(lst[i]) is not None]
    ranked_tasks = [lst[i] for lst, i in ranked_slots]
    ranks_now = [rank(t) for t in ranked_tasks]
    if ranks_now == sorted(ranks_now):  # type: ignore[type-var]
        return
    reordered = sorted(ranked_tasks, key=rank)  # type: ignore[arg-type]  # 稳定排序: 同序任务相对位置不变
    moved = [t["title"] for old, t in zip(ranked_tasks, reordered, strict=True) if old is not t]
    for (lst, i), task in zip(ranked_slots, reordered, strict=True):
        lst[i] = task
    fixes.append({"fix": "kc_order_deferred", "moved": moved})


def _fix_resource_refs(plan: dict, n_resources: int, fixes: list[dict]) -> None:
    for day in _iter_days(plan):
        for task in day["tasks"]:
            ref = task["resource_ref"]
            if ref is not None and not (0 <= ref < n_resources):
                task["resource_ref"] = None
                fixes.append({"fix": "resource_ref_nulled", "task": task["title"], "ref": ref})


def _fix_day_minutes(plan: dict, budget_minutes: float, fixes: list[dict]) -> None:
    """每日任务分钟和硬校验: 超出 [0.7, 1.15]*预算 时按比例缩放到预算值 (F3-A1)。"""
    low = _DAY_MIN_RATIO * budget_minutes
    high = _DAY_MAX_RATIO * budget_minutes
    target = max(int(round(budget_minutes)), 1)
    for day in _iter_days(plan):
        tasks = day["tasks"]
        if not tasks:
            tasks.append(
                {"title": "自主复习", "minutes": target, "kc_names": [], "resource_ref": None}
            )
            fixes.append({"fix": "empty_day_filled", "day": day["day"]})
            continue
        total = sum(t["minutes"] for t in tasks)
        if low <= total <= high:
            continue
        factor = budget_minutes / total
        for task in tasks:
            task["minutes"] = max(int(round(task["minutes"] * factor)), 1)
        # 取整误差回填到最大任务, 保证缩放后日总和恰为预算值 (必然落在区间内)
        diff = target - sum(t["minutes"] for t in tasks)
        largest = max(tasks, key=lambda t: t["minutes"])
        largest["minutes"] = max(largest["minutes"] + diff, 1)
        fixes.append(
            {
                "fix": "day_minutes_scaled",
                "day": day["day"],
                "from": total,
                "to": sum(t["minutes"] for t in tasks),
            }
        )


# ---------------------------------------------------------------- 图构建


def build_plan_graph(deps: GraphDeps) -> StateGraph:
    """构建 planner 子图; deps 由闭包捕获 (不进 state, 见 GraphDeps 文档)。调用方负责 compile。"""

    async def parse_goal(state: PlanGraphState) -> dict:
        goal = state.get("goal", "")
        prompt = _load_prompt("plan_parse_goal.md").replace("{{GOAL}}", goal)
        data = await _chat_json(
            deps, state, feature="plan_parse_goal", prompt=prompt, tier=Tier.cheap, temperature=0.0
        )
        parsed = {"subject": "", "key_topics": [], "level_hint": ""}
        degraded = data is None
        if data is not None:
            subject = data.get("subject")
            parsed["subject"] = subject.strip() if isinstance(subject, str) else ""
            topics = data.get("key_topics")
            if isinstance(topics, list):
                parsed["key_topics"] = [t.strip() for t in topics if isinstance(t, str) and t.strip()]
            hint = data.get("level_hint")
            parsed["level_hint"] = hint.strip() if isinstance(hint, str) else ""
        if not parsed["key_topics"] and goal.strip():
            # 兜底: 目标本身充当唯一主题, 保证后续节点仍有输入
            parsed["key_topics"] = [goal.strip()[:30]]
        event = "llm_parse_failed_fallback" if degraded else "parsed"
        return {
            "parsed_goal": parsed,
            "trace": [{"node": "parse_goal", "event": event, "parsed_goal": parsed}],
        }

    async def diagnose(state: PlanGraphState) -> dict:
        existing = state.get("diagnosis")
        if existing:
            # 外部摸底结果 (如已做过测评) 直接采用; 真摸底考卷 M4 接 quiz 子图
            return {
                "trace": [
                    {"node": "diagnose", "event": "external_diagnosis_used", "kc_count": len(existing)}
                ]
            }
        parsed = state.get("parsed_goal") or {}
        topics = parsed.get("key_topics") or []
        diagnosis: dict[str, float] = {}
        if not topics:
            return {
                "diagnosis": diagnosis,
                "trace": [{"node": "diagnose", "event": "no_topics"}],
            }
        prompt = (
            _load_prompt("plan_diagnose.md")
            .replace("{{KEY_TOPICS}}", "\n".join(f"- {t}" for t in topics))
            .replace("{{LEVEL_HINT}}", parsed.get("level_hint") or "(未提供)")
        )
        data = await _chat_json(
            deps, state, feature="plan_diagnose", prompt=prompt, tier=Tier.cheap, temperature=0.0
        )
        raw = data.get("diagnosis") if isinstance(data, dict) else None
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(key, str) and isinstance(value, int | float):
                    diagnosis[key.strip()] = min(max(float(value), 0.0), 1.0)
        # 保守兜底: LLM 漏掉 (或解析失败) 的主题一律取 0.3
        for topic in topics:
            diagnosis.setdefault(topic, _DEFAULT_MASTERY)
        return {
            "diagnosis": diagnosis,
            "trace": [
                {"node": "diagnose", "event": "conservative_initialized", "kc_count": len(diagnosis)}
            ],
        }

    async def gather_resources(state: PlanGraphState) -> dict:
        parsed = state.get("parsed_goal") or {}
        topics = (parsed.get("key_topics") or list(state.get("diagnosis") or {}))[:_TOPIC_LIMIT]
        resources: list[dict] = []
        trace: list[dict] = []
        kb_id = state.get("kb_id", "")
        if kb_id and deps.retrieval is not None:
            seen_chunks: set[str] = set()
            for topic in topics:
                try:
                    result = await deps.retrieval.retrieve(state.get("tenant_id", ""), kb_id, topic)
                except Exception as exc:  # 单 topic 检索失败不阻断规划
                    trace.append(
                        {"node": "gather_resources", "event": "kb_error", "topic": topic, "error": str(exc)}
                    )
                    continue
                for chunk in result.chunks[:_KB_TOP_K]:
                    if chunk.chunk_id in seen_chunks:
                        continue
                    seen_chunks.add(chunk.chunk_id)
                    resources.append(
                        {
                            "type": "kb",
                            "title": chunk.heading_path or chunk.text[:30],
                            "doc_id": chunk.doc_id,
                            "chunk_id": chunk.chunk_id,
                        }
                    )
        if deps.web_search is not None:
            seen_urls: set[str] = set()
            for topic in topics:
                try:
                    results = await deps.web_search(topic)
                except Exception as exc:
                    trace.append(
                        {"node": "gather_resources", "event": "web_error", "topic": topic, "error": str(exc)}
                    )
                    continue
                for item in (results or [])[:_WEB_TOP_K]:
                    url = item.get("url") if isinstance(item, dict) else None
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    resources.append({"type": "web", "title": item.get("title") or url, "url": url})
        # 知识库与 web 均不可用: 资源标"待补充", 计划仍生成
        event = "gathered" if resources else "resources_pending"
        trace.append({"node": "gather_resources", "event": event, "count": len(resources)})
        return {"resources": resources, "trace": trace}

    async def draft_plan(state: PlanGraphState) -> dict:
        total_days = max(int(state.get("total_days") or 1), 1)
        budget_minutes = max(float(state.get("budget_hours_per_day") or 1.0) * 60.0, 1.0)
        resources = state.get("resources") or []
        parsed = state.get("parsed_goal") or {}
        diagnosis = state.get("diagnosis") or {}
        kc_order = [k for k in (state.get("kc_order") or []) if isinstance(k, str)]
        resources_block = "\n".join(
            f"{i}. [{r.get('type', '')}] {r.get('title', '')}" for i, r in enumerate(resources)
        ) or "(暂无资源, 所有任务 resource_ref 一律填 null)"
        prompt = (
            _load_prompt("plan_draft.md")
            .replace("{{TOTAL_DAYS}}", str(total_days))
            .replace("{{BUDGET_MINUTES}}", str(int(round(budget_minutes))))
            .replace("{{GOAL}}", state.get("goal", ""))
            .replace("{{SUBJECT}}", parsed.get("subject") or "(未知)")
            .replace("{{DIAGNOSIS}}", json.dumps(diagnosis, ensure_ascii=False))
            .replace("{{RESOURCES}}", resources_block)
            .replace("{{KC_ORDER}}", " -> ".join(kc_order) or "(无硬性先后约束)")
        )
        data = await _chat_json(
            deps, state, feature="plan_draft", prompt=prompt, tier=Tier.standard, temperature=0.4
        )
        fixes: list[dict] = []
        plan = _coerce_plan(data)
        if plan is None:
            plan = _fallback_plan(state, total_days, budget_minutes)
            fixes.append({"fix": "draft_fallback", "reason": "llm_json_unparsable_after_retry"})
        # 硬校验顺序: 天数 -> 编号 -> KC 先后 (会挪动任务所在日) -> 资源引用 -> 每日分钟缩放
        _fix_total_days(plan, total_days, budget_minutes, fixes)
        _renumber_days(plan)
        _fix_kc_order(plan, kc_order, fixes)
        _fix_resource_refs(plan, len(resources), fixes)
        _fix_day_minutes(plan, budget_minutes, fixes)
        trace = [{"node": "draft_plan", "event": "drafted", "fix_count": len(fixes)}]
        trace.extend({"node": "draft_plan", "event": "fix", **fix} for fix in fixes)
        return {"plan": plan, "trace": trace}

    async def confirm_plan(state: PlanGraphState) -> dict:
        # interrupt 挂起等待前端确认; resume 后本节点从头重放 (前面无副作用)
        decision = interrupt(
            {
                "kind": "plan_confirm",
                "plan": state.get("plan") or {},
                "resources": state.get("resources") or [],
            }
        )
        approved = (
            bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        )
        if not approved:
            return {"trace": [{"node": "confirm_plan", "event": "plan_rejected"}]}
        updates: dict[str, Any] = {"plan_confirmed": True}
        event = "plan_approved"
        if isinstance(decision, dict):
            edited = decision.get("plan")
            if isinstance(edited, dict) and edited.get("stages"):
                updates["plan"] = edited  # 用户在确认界面编辑后的版本, 以用户为准
                event = "plan_approved_with_edits"
        updates["trace"] = [{"node": "confirm_plan", "event": event}]
        return updates

    graph = StateGraph(PlanGraphState)
    graph.add_node("parse_goal", parse_goal)
    graph.add_node("diagnose", diagnose)
    graph.add_node("gather_resources", gather_resources)
    graph.add_node("draft_plan", draft_plan)
    graph.add_node("confirm_plan", confirm_plan)
    graph.add_edge(START, "parse_goal")
    graph.add_edge("parse_goal", "diagnose")
    graph.add_edge("diagnose", "gather_resources")
    graph.add_edge("gather_resources", "draft_plan")
    graph.add_edge("draft_plan", "confirm_plan")
    graph.add_edge("confirm_plan", END)
    return graph
