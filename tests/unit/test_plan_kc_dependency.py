"""计划详情的只读 KC DAG 派生视图测试。"""

from __future__ import annotations

from eduagents_api.routers.plans import _build_kc_dependency

TASK_ID = "00000000-0000-4000-8000-000000000001"


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
                                "task_id": TASK_ID,
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
        locks={TASK_ID: {"blockers": [{"kc_name": "A", "zone": "较弱"}]}},
    )

    a = _node(view, "a")
    c = _node(view, "c")
    assert a["blocking_task_ids"] == [TASK_ID]
    assert a["is_blocking"] is True
    assert c["task_statuses"] == [{"task_id": TASK_ID, "status": "pending"}]
    serialized = str(view)
    assert "p_mastery" not in serialized
    assert "confidence" not in serialized


# ---- primary_route: 主学习线 (2b) ----


def _route_pairs(view: dict) -> set[tuple[str, str]]:
    route = view["primary_route"]
    return set(zip(route, route[1:], strict=False))  # 相邻对: 长度天然差 1


def _edge_pairs(view: dict) -> set[tuple[str, str]]:
    return {(edge["from_kc_id"], edge["to_kc_id"]) for edge in view["edges"]}


def test_primary_route_walks_only_real_edges():
    """路径里任意相邻两点必须有真实边 —— 界面高亮不得出现 A→C 捷径假边。"""
    view = _build_kc_dependency(
        _structure(("C", "pending")),
        _rows(("a", "A"), ("b", "B"), ("c", "C")),
        _edges(("a", "b"), ("b", "c")),
    )
    assert view["primary_route"] == ["a", "b", "c"]
    assert _route_pairs(view) <= _edge_pairs(view)


def test_primary_route_takes_the_longest_supporting_chain():
    """并行前驱里取支撑链最长的那条 (关键路径), 而不是随便一条。"""
    view = _build_kc_dependency(
        _structure(("D", "pending")),
        _rows(("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")),
        # a → b → d 是长链; c → d 是短链
        _edges(("a", "b"), ("b", "d"), ("c", "d")),
    )
    assert view["primary_route"] == ["a", "b", "d"]
    assert _route_pairs(view) <= _edge_pairs(view)


def test_primary_route_targets_the_deepest_task_kc():
    """终点是"最深的计划任务 KC": 先修再多也要走到真正要学的那个任务。"""
    view = _build_kc_dependency(
        _structure(("B", "pending"), ("D", "pending")),
        _rows(("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")),
        _edges(("a", "b"), ("c", "d"), ("b", "d")),
    )
    assert view["primary_route"][-1] == "d"
    assert _route_pairs(view) <= _edge_pairs(view)


def test_primary_route_is_deterministic_for_ties():
    """深度并列时按稳定库序取, 同一份数据永远同一条线。"""
    first = _build_kc_dependency(
        _structure(("B", "pending"), ("C", "pending")),
        _rows(("b", "B"), ("c", "C")),
        _edges(),
    )
    second = _build_kc_dependency(
        _structure(("B", "pending"), ("C", "pending")),
        _rows(("b", "B"), ("c", "C")),
        _edges(),
    )
    assert first["primary_route"] == second["primary_route"] == ["b"]


def test_primary_route_is_empty_without_any_task_kc():
    """纯先修闭包 (没有任何任务 KC) 不编造终点。"""
    view = _build_kc_dependency({"stages": []}, _rows(("a", "A")), _edges())
    assert view["primary_route"] == []


def test_primary_route_survives_a_stored_cycle():
    """存量环被 add_edge_safe 降级后, 路径仍然只走被接受的边, 且不死循环。"""
    view = _build_kc_dependency(
        _structure(("B", "pending")),
        _rows(("a", "A"), ("b", "B")),
        _edges(("a", "b"), ("b", "a")),
    )
    assert view["primary_route"] == ["a", "b"]
    assert _route_pairs(view) <= _edge_pairs(view)


def test_empty_view_shapes_include_primary_route():
    """空视图形状统一, 前端不必判 undefined。"""
    view = _build_kc_dependency(None, [], [])
    assert view == {"levels": [], "edges": [], "primary_route": []}
