"""Small source contracts for the cross-page plan/filter interactions.

The repository has no Jest/RTL harness; these checks pin the public browser
contracts without starting Next.js or making network calls.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_today_uses_api_schedule_and_does_not_mark_future_tasks_today():
    source = (ROOT / "apps/web/app/today/page.tsx").read_text(encoding="utf-8")
    assert "schedule?.today_tasks" in source
    assert "schedule?.overdue_tasks" in source
    assert "Date.parse" not in source
    assert 'dueLabel: isOverdue ? "逾期" : "计划今日"' in source


def test_errors_filter_has_one_local_source_and_history_sync():
    source = (ROOT / "apps/web/app/errors/page.tsx").read_text(encoding="utf-8")
    assert "const on = kbFilter === tab.id" in source
    assert "kbFilter || urlKbFilter" not in source
    assert "window.history.pushState" in source
    assert "window.addEventListener(\"popstate\"" in source
    assert "params.delete(\"kb_id\")" in source
