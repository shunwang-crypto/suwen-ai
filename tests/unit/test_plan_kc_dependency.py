"""计划详情的只读 KC DAG 派生视图测试。"""

from __future__ import annotations

from eduagents_api.routers.plans import _build_kc_dependency


def _structure(*names: tuple[str, str]) -> dict:
    return {
        "stages": [
            {
                "name": "基础",
                "days": [
                    {
                        "day": 1,
                        "tasks": [
                            {
                                "title": name,
                                "kc_names": [name],
                                "status": status,
                            }
                            for name, status in names
                        ],
                    }
                ],
            }
        ]
    }


def _rows(*items: tuple[str, str]) -> list[tuple[str, str]]:
    return list(items)


def _edges(*items: tuple[str, str]) -> list[tuple[str, str]]:
    return list(items)


def _node(view: dict, kc_id: str) -> dict:
    return next(node for level in view["levels"] for node in level["nodes"] if node["kc_id"] == kc_id)


def test_dependency_levels_support_multiple_prerequisites():
    view = _build_kc_dependency(
        _structure(("C", "pending")),
        _rows(("a", "A"), ("b", "B"), ("c", "C")),
        _edges(("a", "c"), ("b", "c")),
    )

    assert [[node["kc_id"] for node in level["nodes"]] for level in view["levels"]] == [
        ["a", "b"],
        ["c"],
    ]
    assert view["edges"] == [
        {"from_kc_id": "a", "to_kc_id": "c"},
        {"from_kc_id": "b", "to_kc_id": "c"},
    ]
    assert _node(view, "c")["is_current_task"] is True
    assert _node(view, "a")["is_current_task"] is False


def test_dependency_levels_render_shared_prerequisite_once_in_diamond():
    view = _build_kc_dependency(
        _structure(("D", "pending")),
        _rows(("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")),
        _edges(("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")),
    )

    ids = [node["kc_id"] for level in view["levels"] for node in level["nodes"]]
    assert ids == ["a", "b", "c", "d"]
    assert ids.count("a") == 1
    assert [[node["kc_id"] for node in level["nodes"]] for level in view["levels"]] == [
        ["a"],
        ["b", "c"],
        ["d"],
    ]


def test_dependency_view_ignores_out_of_scope_edges_and_degrades_cycles():
    view = _build_kc_dependency(
        _structure(("C", "pending")),
        _rows(("a", "A"), ("b", "B"), ("c", "C")),
        _edges(("outside", "c"), ("a", "c"), ("c", "a"), ("b", "missing")),
    )

    ids = [node["kc_id"] for level in view["levels"] for node in level["nodes"]]
    assert set(ids) == {"a", "c"}
    assert all(edge["from_kc_id"] in {"a", "c"} for edge in view["edges"])
    assert all(edge["to_kc_id"] in {"a", "c"} for edge in view["edges"])


def test_dependency_view_exposes_task_status_and_blocking_without_mastery_values():
    view = _build_kc_dependency(
        _structure(("C", "pending")),
        _rows(("a", "A"), ("c", "C")),
        _edges(("a", "c")),
        locks={"stage0.day0.task0": {"blockers": [{"kc_name": "A", "zone": "较弱"}]}},
    )

    a = _node(view, "a")
    c = _node(view, "c")
    assert a["blocking_task_keys"] == ["stage0.day0.task0"]
    assert a["is_blocking"] is True
    assert c["task_statuses"] == [{"task_key": "stage0.day0.task0", "status": "pending"}]
    serialized = str(view)
    assert "p_mastery" not in serialized
    assert "confidence" not in serialized
