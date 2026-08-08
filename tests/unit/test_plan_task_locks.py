"""先修未达标 -> 任务锁定单测 (纯函数, 零 PG)。

锁定是派生展示态, 不进 TASK_STATUSES 也不落 structure_json —— 它依赖实时
p_mastery, 掌握度一变就该自动解锁。这里覆盖的正是这个"不持久化"的语义边界:

- 先修低于 REINFORCE_THRESHOLD 才锁, 达标即放行;
- 无掌握度记录的先修不算阻塞 (冷启动不能把整棵树锁死);
- 已完成/已跳过的任务永不锁 (做过的事不该被事后判定为不可做);
- plan_progress 的口径不受锁定影响 (locked 仍算 pending);
- enrich_structure 的 locked/lock_blockers 是纯展示字段。
"""

from __future__ import annotations

from eduagents_api.plan_adjust import (
    compute_task_locks,
    enrich_structure,
    plan_progress,
)
from eduagents_core.mastery.thresholds import REINFORCE_THRESHOLD

# 极限 -> 导数 -> 泰勒公式 (与 kc_edges 里的真实边形状一致)
PREREQ = {"导数": ["极限"], "泰勒公式": ["导数"]}


def _structure(*statuses: str) -> dict:
    """每个任务挂一个 KC, 顺序与 PREREQ 链一致。"""
    names = ["极限", "导数", "泰勒公式"]
    return {
        "stages": [
            {
                "days": [
                    {
                        "tasks": [
                            {"kc_names": [names[i]], "status": statuses[i]}
                            for i in range(len(statuses))
                        ]
                    }
                ]
            }
        ]
    }


def test_unmet_prerequisite_locks_task():
    """极限没掌握 -> 导数任务被锁, 并给出可解释的阻塞项。"""
    locks = compute_task_locks(
        _structure("pending", "pending", "pending"),
        {"极限": 0.2, "导数": 0.5},
        PREREQ,
    )

    assert "stage0.day0.task1" in locks
    blockers = locks["stage0.day0.task1"]["blockers"]
    assert [b["kc_name"] for b in blockers] == ["极限"]
    assert blockers[0]["zone"]  # 定性区间, 供前端 tooltip 直接展示


def test_met_prerequisite_does_not_lock():
    locks = compute_task_locks(
        _structure("pending", "pending", "pending"),
        {"极限": REINFORCE_THRESHOLD, "导数": 0.5},
        PREREQ,
    )

    assert "stage0.day0.task1" not in locks


def test_prerequisite_without_mastery_record_is_not_a_blocker():
    """没学过 != 没掌握: 冷启动时把整棵树锁死会让新用户寸步难行。"""
    locks = compute_task_locks(
        _structure("pending", "pending", "pending"), {"导数": 0.5}, PREREQ
    )

    assert locks == {}


def test_done_and_skipped_tasks_are_never_locked():
    """已完成/已跳过的任务不该被事后判定为不可做。"""
    locks = compute_task_locks(
        _structure("pending", "done", "skipped"), {"极限": 0.1, "导数": 0.1}, PREREQ
    )

    assert locks == {}


def test_missing_inputs_lock_nothing():
    structure = _structure("pending", "pending", "pending")
    assert compute_task_locks(structure, None, PREREQ) == {}
    assert compute_task_locks(structure, {"极限": 0.1}, None) == {}
    assert compute_task_locks(structure, {}, {}) == {}


def test_blockers_are_deduplicated():
    """同一先修被任务的多个 KC 共同依赖时只报一次。"""
    structure = {
        "stages": [{"days": [{"tasks": [{"kc_names": ["导数", "泰勒公式"], "status": "pending"}]}]}]
    }
    locks = compute_task_locks(structure, {"极限": 0.1, "导数": 0.1}, PREREQ)

    blockers = locks["stage0.day0.task0"]["blockers"]
    assert sorted(b["kc_name"] for b in blockers) == ["导数", "极限"]


def test_progress_ignores_locks():
    """locked 仍是 pending: 锁定不改变分母, 否则进度条会凭空虚高。"""
    structure = _structure("pending", "pending", "done")
    before = plan_progress(structure)
    locks = compute_task_locks(structure, {"极限": 0.1, "导数": 0.1}, PREREQ)

    assert locks  # 确有任务被锁
    assert plan_progress(structure) == before
    assert before["pending"] == 2


def test_enrich_structure_exposes_lock_fields():
    structure = _structure("pending", "pending", "pending")
    locks = compute_task_locks(structure, {"极限": 0.1, "导数": 0.5}, PREREQ)
    out = enrich_structure(structure, None, locks)

    tasks = out["stages"][0]["days"][0]["tasks"]
    assert tasks[0]["locked"] is False
    assert tasks[0]["lock_blockers"] == []
    assert tasks[1]["locked"] is True
    assert tasks[1]["lock_blockers"][0]["kc_name"] == "极限"
    # 派生态不得写回 status
    assert tasks[1]["status"] == "pending"


def test_enrich_structure_without_locks_is_unchanged_shape():
    """不传 locks 时旧调用方行为不变, 只是多两个默认字段。"""
    out = enrich_structure(_structure("pending", "done", "skipped"))

    for task in out["stages"][0]["days"][0]["tasks"]:
        assert task["locked"] is False
        assert task["lock_blockers"] == []
