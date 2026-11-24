"""Architecture backstops for the destructive student-experience refactor.

Git preserves the retired implementation.  Runtime source must therefore keep
one navigation model, one plan identity, and one mastery write boundary instead
of quietly reintroducing compatibility paths.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "apps" / "web"

RETIRED_STUDENT_ROUTES = ("kb", "resources", "errors", "profile", "reports")
PRIMARY_NAV_ROUTES = ["/today", "/chat", "/practice", "/courses", "/growth"]


def _runtime_python_files() -> list[Path]:
    roots = (
        ROOT / "apps" / "api" / "src",
        ROOT / "apps" / "worker" / "src",
        ROOT / "packages" / "core" / "src",
    )
    return [path for source_root in roots for path in source_root.rglob("*.py")]


def _web_runtime_files() -> list[Path]:
    return [
        path
        for source_root in (WEB / "app", WEB / "components", WEB / "lib")
        for path in source_root.rglob("*")
        if path.suffix in {".ts", ".tsx"} and "__tests__" not in path.parts
    ]


def test_student_navigation_primary_destinations_and_aliases() -> None:
    shell = (WEB / "components" / "app-shell.tsx").read_text(encoding="utf-8")
    navigation = shell.split("PRIMARY_NAV_ITEMS", 1)[1].split("const NAV_HIGHLIGHT_ALIASES", 1)[0]
    assert re.findall(r'href:\s*"([^"]+)"', navigation) == PRIMARY_NAV_ROUTES
    # 旧的通配高亮 hack 不许回归；高亮必须走显式别名表。
    assert "TODAY_TOOL_PATHS" not in shell
    assert "NAV_HIGHLIGHT_ALIASES" in shell


def test_retired_student_pages_and_links_are_absent() -> None:
    for route in RETIRED_STUDENT_ROUTES:
        assert not (WEB / "app" / route / "page.tsx").exists()
    assert (WEB / "app" / "materials" / "page.tsx").is_file()
    assert (WEB / "app" / "growth" / "page.tsx").is_file()

    # Match only browser paths that begin a JS string/template.  Backend API
    # contracts such as "/api/kb" remain valid and are intentionally ignored.
    # 别名映射表的 key (NAV_HIGHLIGHT_ALIASES / 中文标签表, 形如 "/errors": "/practice")
    # 是把旧 URL 高亮/标注到新页的显式声明, 不是指向下线页的链接, 故豁免
    # "闭合引号后紧跟冒号" 的对象 key 形态。
    retired_link = re.compile(
        r'(?P<quote>["\'`])/(?:kb|resources|errors|profile|reports)'
        r'(?=[/?]|(?P=quote)(?!\s*:))'
    )
    offenders = [
        str(path.relative_to(ROOT))
        for path in _web_runtime_files()
        if retired_link.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_recommendations_have_one_page_owner() -> None:
    assert not (WEB / "components" / "chat" / "opening-card.tsx").exists()
    reports = (
        ROOT / "apps" / "api" / "src" / "eduagents_api" / "routers" / "reports.py"
    ).read_text(encoding="utf-8")
    assert "_build_actions" not in reports
    assert re.search(r'["\']actions["\']\s*:', reports) is None


def test_runtime_uses_only_stable_plan_identity() -> None:
    forbidden = re.compile(r"\blatest_plan\b|\btask_key\b|\bis_current\b")
    offenders: list[str] = []
    for path in [*_runtime_python_files(), *_web_runtime_files()]:
        if forbidden.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_plan_tasks_use_one_status_field() -> None:
    sources = [
        ROOT / "apps" / "api" / "src" / "eduagents_api" / "plan_adjust.py",
        ROOT / "apps" / "api" / "src" / "eduagents_api" / "routers" / "plans.py",
        ROOT / "packages" / "core" / "src" / "eduagents_core" / "education" / "plan.py",
        WEB / "app" / "plans" / "page.tsx",
        WEB / "components" / "plans" / "plan-task-drawer.tsx",
    ]
    # ``done`` is still a valid aggregate progress field and a status value;
    # only task-level legacy reads/writes are forbidden.
    forbidden = re.compile(r'task(?:\[|\.)["\']done["\']|task\.done')
    offenders = [
        str(path.relative_to(ROOT))
        for path in sources
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_mastery_records_are_written_only_by_evidence_service() -> None:
    allowed = ROOT / "apps" / "api" / "src" / "eduagents_api" / "services.py"
    declarations = ROOT / "apps" / "api" / "src" / "eduagents_api" / "models.py"
    forbidden = re.compile(r"\bMasteryRecord\s*\(|\.p_mastery\s*=")
    offenders = [
        str(path.relative_to(ROOT))
        for path in _runtime_python_files()
        if path not in {allowed, declarations}
        and forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_model_markdown_has_no_retired_compatibility_component() -> None:
    assert (WEB / "components" / "chat" / "model-markdown.tsx").is_file()
    assert not (WEB / "components" / "chat" / "markdown-answer.tsx").exists()
    offenders = [
        str(path.relative_to(ROOT))
        for path in _web_runtime_files()
        if "MarkdownAnswer" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
