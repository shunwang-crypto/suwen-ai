"""KC 先修 DAG (§5.8 第 4 条): 环检测 + 拓扑排序。

用途: 学习规划任务顺序约束 (F3-A2)、学情报告归因下钻 (F7)。
DAG 是轻量的: 只覆盖主干概念, 允许稀疏。
"""

from __future__ import annotations

import heapq

from .types import PrereqEdge


def _adjacency(edges: list[PrereqEdge]) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {}
    for edge in edges:
        adj.setdefault(edge.from_kc, []).append(edge.to_kc)
        adj.setdefault(edge.to_kc, [])
    return adj


def _has_path(adj: dict[str, list[str]], start: str, target: str) -> bool:
    """迭代 DFS: start 是否可达 target。"""
    stack = [start]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj.get(node, ()))
    return False


def add_edge_safe(edges: list[PrereqEdge], new: PrereqEdge) -> bool:
    """加边前做环检测: 自环恒拒绝; 若 to_kc 已可达 from_kc 则加入必成环, 拒绝。

    成功时把 new 追加进 edges 并返回 True; 重复边幂等返回 True 不重复追加。
    """
    if new.from_kc == new.to_kc:
        return False
    if new in edges:
        return True
    if _has_path(_adjacency(edges), new.to_kc, new.from_kc):
        return False
    edges.append(new)
    return True


def validate_acyclic(edges: list[PrereqEdge]) -> bool:
    """三色迭代 DFS 全图环检测: 无环返回 True。"""
    adj = _adjacency(edges)
    state: dict[str, int] = {}  # 1=访问中(在栈上), 2=已完成
    for root in adj:
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(adj[root]))]
        while stack:
            node, neighbors = stack[-1]
            nxt = next(neighbors, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
                continue
            mark = state.get(nxt)
            if mark == 1:
                return False  # 回边 -> 有环
            if mark == 2:
                continue
            state[nxt] = 1
            stack.append((nxt, iter(adj.get(nxt, []))))
    return True


def topological_order(kc_ids: list[str], edges: list[PrereqEdge]) -> list[str]:
    """稳定 Kahn 拓扑排序: 仅约束 kc_ids 内节点, 无约束节点保持原相对顺序。

    - 两端点不都在 kc_ids 内的边直接忽略;
    - 就绪集合用"原始下标最小堆", 保证无约束时输出即输入顺序 (稳定、确定);
    - 若列表内节点间存在环 (上游未拦住的脏数据), 环内节点按原顺序兜底追加,
      保证输出节点集恒等于输入节点集。
    """
    index_of: dict[str, int] = {}
    for kc in kc_ids:
        if kc not in index_of:
            index_of[kc] = len(index_of)
    nodes = list(index_of)

    indegree = dict.fromkeys(nodes, 0)
    adj: dict[str, list[str]] = {node: [] for node in nodes}
    seen_pairs: set[tuple[str, str]] = set()
    for edge in edges:
        if edge.from_kc not in index_of or edge.to_kc not in index_of:
            continue
        if edge.from_kc == edge.to_kc:
            continue
        pair = (edge.from_kc, edge.to_kc)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        adj[edge.from_kc].append(edge.to_kc)
        indegree[edge.to_kc] += 1

    ready = [index_of[node] for node in nodes if indegree[node] == 0]
    heapq.heapify(ready)
    result: list[str] = []
    while ready:
        node = nodes[heapq.heappop(ready)]
        result.append(node)
        for neighbor in adj[node]:
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                heapq.heappush(ready, index_of[neighbor])

    if len(result) < len(nodes):
        emitted = set(result)
        result.extend(node for node in nodes if node not in emitted)
    return result
