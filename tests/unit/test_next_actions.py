"""可解释推荐聚合单测 (Wave-P2 P2-2/P2-5, G13): 纯函数, 零 PG/零 LLM。

覆盖:
- compose_next_actions 优先级 review > resume_insight > weak_practice > plan_task, ≤5 条;
- 冷启动兜底: 四类全空给「摸底测评」「制定计划」两条 (话术对齐 reports.py fallback);
- 理由脱敏 (P2-7): 全部条目无掌握度小数, weak_practice 只用 zone 定性词;
- resume_insight 分流: confused 链 /chat?q= 续问 (带学生原话), misconception 链 /tutor;
- plan_today_snapshot: 截至今天的未完成任务口径 (含日程号缺失/全完成/未来任务)。
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from urllib.parse import unquote

from eduagents_api.plan_adjust import plan_schedule_snapshot
from eduagents_api.routers.next_actions import (
    MAX_ITEMS,
    compose_next_actions,
    plan_today_snapshot,
)
from eduagents_core.mastery.thresholds import zone_label

_INSIGHT = {"kc_name": "导数定义", "kind": "confused", "text": "为什么不能直接把 x=1 代进去?"}


def _weak(name: str, p: float, recent: bool = False) -> dict:
    """weak_point_kcs 输出形状 (带 p_mastery 小数, 用于验证不泄漏进理由)。"""
    from eduagents_api.profile_summary import difficulty_rationale

    rationale = difficulty_rationale(name, p, recent)
    return {
        "kc_id": "kc-" + name,
        "name": name,
        "p_mastery": round(p, 4),
        "recent_error": recent,
        "zone": rationale["zone"],
        "difficulty": rationale["difficulty"],
        "reason": rationale["reason"],
    }


def _all_inputs() -> dict:
    return {
        "due_count": 3,
        "insight": dict(_INSIGHT),
        "weak_points": [_weak("极值判定", 0.35, recent=True), _weak("泰勒公式", 0.52)],
        "plan": {
            "goal": "期末高数 90 分", "day_no": 4, "undone": 2,
            "plan_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4()),
            "title": "计划任务", "action_type": "practice", "topic": "高数",
            "count": 3, "difficulty": "mid",
        },
    }


# ---- 优先级与上限 ----


def test_priority_order_and_cap():
    items = compose_next_actions(**_all_inputs())
    assert [it["type"] for it in items] == [
        "review",
        "resume_insight",
        "weak_practice",
        "weak_practice",
        "plan_task",
    ]
    assert len(items) <= MAX_ITEMS
    # 每条都能回答"为什么": type/title/reason/href 四字段齐且非空
    for it in items:
        assert set(it) == {"type", "title", "reason", "href"}
        assert it["title"] and it["reason"] and it["href"]


def test_review_item_cites_due_count():
    items = compose_next_actions(
        due_count=4, insight=None, weak_points=[], plan=None
    )
    assert items[0]["type"] == "review"
    assert "4 道错题到期" in items[0]["title"]
    assert "FSRS" in items[0]["reason"]  # 理由指向排程依据, 不是空话
    assert items[0]["href"] == "/reviews"


def test_weak_points_capped_at_two_within_five_total():
    items = compose_next_actions(
        due_count=1,
        insight=dict(_INSIGHT),
        weak_points=[_weak(f"知识点{i}", 0.3 + i * 0.05) for i in range(5)],
        plan={
            "goal": "目标", "day_no": 1, "undone": 1,
            "plan_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4()),
            "title": "计划任务", "action_type": "practice", "topic": "目标",
            "count": 3, "difficulty": "mid",
        },
    )
    assert len(items) == MAX_ITEMS == 5
    assert sum(1 for it in items if it["type"] == "weak_practice") == 2
    assert items[-1]["type"] == "plan_task"  # 计划不被薄弱点挤掉


# ---- 冷启动兜底 ----


def test_cold_start_fallback_matches_reports_wording():
    items = compose_next_actions(due_count=0, insight=None, weak_points=[], plan=None)
    assert [it["type"] for it in items] == ["cold_start"]
    assert [it["href"] for it in items] == ["/quiz"]
    assert "摸底" in items[0]["title"]
    assert "真实作答证据" in items[0]["reason"]


def test_no_fallback_when_any_category_present():
    items = compose_next_actions(due_count=0, insight=None, weak_points=[_weak("极值", 0.4)], plan=None)
    assert [it["type"] for it in items] == ["weak_practice"]


# ---- 理由脱敏 (P2-7): 定性词, 无掌握度小数 ----


def test_reasons_qualitative_no_mastery_decimals():
    items = compose_next_actions(**_all_inputs())
    decimal = re.compile(r"\d\.\d")  # 0.35 / 0.5 之类的小数
    for it in items:
        assert not decimal.search(it["title"]), it
        assert not decimal.search(it["reason"]), it
    weak_items = [it for it in items if it["type"] == "weak_practice"]
    assert "0.35" not in str(weak_items)  # p_mastery 不得渗入任何字段
    # weak 理由携带 zone 定性词 (thresholds.zone_label 同源)
    assert zone_label(0.35) in weak_items[0]["reason"]


# ---- resume_insight 分流 (F8-A2) ----


def test_resume_confused_links_chat_with_student_words():
    items = compose_next_actions(
        due_count=0, insight=dict(_INSIGHT), weak_points=[], plan=None
    )
    (item,) = items
    assert item["type"] == "resume_insight"
    assert "上次你在『导数定义』卡住了" in item["title"]
    assert _INSIGHT["text"] in item["reason"]  # 理由引用记忆里的学生原话
    assert item["href"].startswith("/chat?q=")
    q = unquote(item["href"].removeprefix("/chat?q="))
    assert "导数定义" in q and _INSIGHT["text"] in q  # 续问带上下文, 只进本人 prompt


def test_resume_misconception_links_tutor():
    items = compose_next_actions(
        due_count=0,
        insight={"kc_name": "", "kind": "misconception", "text": "以为洛必达对任何极限都能用"},
        weak_points=[],
        plan=None,
    )
    (item,) = items
    assert item["type"] == "resume_insight"
    assert item["href"] == "/tutor"
    assert "洛必达" in item["title"]  # 无 KC 名时用原话片段点名卡点
    assert "易错点" in item["reason"]


def test_resume_blank_text_skipped():
    items = compose_next_actions(
        due_count=0,
        insight={"kc_name": "导数定义", "kind": "confused", "text": "  "},
        weak_points=[],
        plan=None,
    )
    assert [it["type"] for it in items] == ["cold_start"]  # 空原话不成卡, 落冷启动


# ---- plan_today_snapshot: "截至今天"的未完成任务口径 ----

_STRUCTURE = {
    "stages": [
        {
            "name": "阶段一",
            "days": [
                {"day": 1, "tasks": [{"task_id": str(uuid.uuid4()), "title": "t1", "status": "done"}, {"task_id": str(uuid.uuid4()), "title": "t2", "status": "pending"}]},
                {"day": 2, "tasks": [{"task_id": str(uuid.uuid4()), "title": "t3", "status": "pending"}]},
                {"day": 9, "tasks": [{"task_id": str(uuid.uuid4()), "title": "未来任务", "status": "pending"}]},
            ],
        }
    ]
}


def test_plan_snapshot_counts_undone_up_to_today():
    snap = plan_today_snapshot("期末冲刺", str(uuid.uuid4()), None, date(2026, 7, 25), _STRUCTURE, date(2026, 7, 26))
    # 创建当天为第 1 天 -> 今天是第 2 天; day9 的未来任务不计
    assert snap["goal"] == "期末冲刺" and snap["day_no"] == 2 and snap["undone"] == 2


def test_plan_snapshot_none_when_all_done_so_far():
    structure = {
        "stages": [
            {
                "days": [
                    {"day": 1, "tasks": [{"task_id": str(uuid.uuid4()), "title": "t", "status": "done"}]},
                    {"day": 5, "tasks": [{"task_id": str(uuid.uuid4()), "title": "later", "status": "pending"}]},
                ]
            }
        ]
    }
    assert plan_today_snapshot("g", str(uuid.uuid4()), None, date(2026, 7, 27), structure, date(2026, 7, 27)) is None


def test_plan_snapshot_missing_day_number_counts_as_due():
    structure = {"stages": [{"days": [{"tasks": [{"title": "无日程号", "status": "pending"}]}]}]}
    task_id = str(uuid.uuid4())
    structure["stages"][0]["days"][0]["tasks"][0]["task_id"] = task_id
    snap = plan_today_snapshot("g", str(uuid.uuid4()), None, date(2026, 7, 27), structure, date(2026, 7, 27))
    assert snap is not None and snap["undone"] == 1


def test_plan_item_wording():
    items = compose_next_actions(
        due_count=0,
        insight=None,
        weak_points=[],
        plan={
            "goal": "期末高数 90 分", "day_no": 4, "undone": 2,
            "plan_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4()),
            "title": "计划任务", "action_type": "practice", "topic": "高数",
            "count": 3, "difficulty": "mid",
        },
    )
    (item,) = items
    assert item["type"] == "plan_task"
    assert "计划任务" in item["title"]
    assert "第 4 天" in item["reason"] and "期末高数 90 分" in item["reason"]
    assert item["href"].startswith("/quiz?plan_id=")


def test_plan_schedule_selects_only_the_server_calculated_current_day():
    structure = {
        "stages": [{
            "days": [
                {"day": 1, "tasks": [{"task_id": str(uuid.uuid4()), "title": "逾期"}]},
                {"day": 2, "tasks": [{"task_id": str(uuid.uuid4()), "title": "今天"}]},
                {"day": 3, "tasks": [{"task_id": str(uuid.uuid4()), "title": "未来"}]},
            ],
        }],
    }
    snapshot = plan_schedule_snapshot(date(2026, 7, 25), structure, date(2026, 7, 26))
    task_ids = [task["task_id"] for day in structure["stages"][0]["days"] for task in day["tasks"]]
    assert snapshot == {
        "current_day": 2,
        "today_tasks": [task_ids[1]],
        "overdue_tasks": [task_ids[0]],
        "upcoming_tasks": [task_ids[2]],
    }


def test_plan_schedule_marks_remaining_tasks_overdue_after_plan_end():
    structure = {
        "stages": [{
            "days": [
                {"day": 1, "tasks": [{"task_id": str(uuid.uuid4()), "title": "已过期"}]},
                {"day": 3, "tasks": [{"task_id": str(uuid.uuid4()), "title": "也已过期"}]},
            ],
        }],
    }
    snapshot = plan_schedule_snapshot(date(2026, 7, 25), structure, date(2026, 7, 30))
    assert snapshot["current_day"] == 6
    assert snapshot["today_tasks"] == []
    assert snapshot["overdue_tasks"] == [
        structure["stages"][0]["days"][0]["tasks"][0]["task_id"],
        structure["stages"][0]["days"][1]["tasks"][0]["task_id"],
    ]
    assert snapshot["upcoming_tasks"] == []
