"""按知识点的结构化讲解：block 契约、校验与缓存键（纯逻辑，零 DB 零 LLM）。

为什么是结构化 block 而不是一坨 Markdown：分段与信号提示是有实证的教学设计
（Mayer 的分段/信号原则），而且结构化之后每一块都可校验、可按块重生成、可强制
带出处。四类 block 各有教育心理学出处：

- ``concept`` 概念解释；
- ``worked_example`` 样例演练（worked-example effect）；
- ``misconception`` 常见误解辨析（refutation text effect）；
- ``diagram`` 结构图示（仅当结构本身有助于理解时）。

三条硬约束（本模块负责，不靠提示词自觉）：

1. **每个 block 必须引用学生本人资料里的真实 chunk**。``source_refs`` 只能是本次
   传入的候选 chunk id；引用不到就丢弃该 block —— 讲解不许脱离资料凭空生成。
2. **diagram 必须结构可用**。nodes 非空、id 唯一；edges **允许为空**（单节点概念图
   本身不算错），但只要有边就必须两端存在、无自环、无环。
   一个"看起来对其实错"的图比没有图更糟；这里只做最小结构校验，不做题型专项校验。
3. **不出练习题**。讲解模块不生成题目/选项/答案/判分（练习有独立模块与证据口径）。

缓存失效：``EXPLANATION_GENERATOR_VERSION``（教学契约变更）+ ``context_hash``
（KC 名 / 掌握档 / 参与生成的 chunk 内容变更）。两者任一不匹配即 stale，读接口
如实标记并给重新生成入口 —— 不静默拿旧内容当新内容，也不因为 stale 就清空内容。
"""

from __future__ import annotations

import hashlib
from typing import Any

#: 教学契约版本。改动 block 语义、提示词教学要求或校验口径时 bump。
EXPLANATION_GENERATOR_VERSION = "kc-explanation-v1"

#: v1 支持的 block 类型（顺序即建议展示顺序）。
BLOCK_TYPES: tuple[str, ...] = ("concept", "worked_example", "misconception", "diagram")

#: 掌握档取值：evidence_state != observed 时一律 unobserved，其余取 mastery_band。
BANDS: tuple[str, ...] = ("unobserved", "weak", "reinforce", "near", "mastered")

TITLE_MAX = 80
CONTENT_MAX = 4000
MAX_BLOCKS = 8
MAX_DIAGRAM_NODES = 24

#: 讲解里禁止出现的练习/判分语义（模块边界，见 test_kc_explanation）。
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"options", "answer", "correct_answer", "score", "grading", "quiz", "question"}
)


class BlockIssue(Exception):
    """单个 block 不合契约（被丢弃的原因，用于日志与测试断言）。"""


def band_for(evidence_state: str, mastery_band: str | None) -> str:
    """证据不足一律按 unobserved 讲（讲法不建立在没有证据的掌握度估计上）。"""
    if evidence_state != "observed" or not mastery_band:
        return "unobserved"
    return mastery_band if mastery_band in BANDS else "unobserved"


def context_hash(kc_name: str, band: str, source_texts: list[str]) -> str:
    """确定性上下文指纹：KC 名 + 掌握档 + 参与生成的资料原文。

    资料改了、档位变了、KC 改名了都会换指纹 → 读接口标记 stale。
    不含时间戳与随机数：同一输入永远同一指纹（否则缓存永远失效）。
    """
    digest = hashlib.sha256()
    digest.update(kc_name.strip().encode("utf-8"))
    digest.update(b"\x00")
    digest.update(band.encode("utf-8"))
    for text in source_texts:
        digest.update(b"\x00")
        digest.update((text or "").strip().encode("utf-8"))
    return digest.hexdigest()


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _validate_diagram(data: dict[str, Any]) -> dict[str, Any]:
    """diagram 的最小结构校验：不做题型专项校验，但绝不放过结构上不可用的图。"""
    raw_nodes = data.get("nodes")
    raw_edges = data.get("edges")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise BlockIssue("diagram 缺少 nodes")
    # edges 允许为空数组: 只有节点的概念图不算结构错误, 不因风格问题丢内容。
    # 但只要给了边, 就必须过引用/自环/环三道校验。
    if not isinstance(raw_edges, list):
        raise BlockIssue("diagram 的 edges 必须是数组")
    nodes: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in raw_nodes[:MAX_DIAGRAM_NODES]:
        if not isinstance(item, dict):
            continue
        node_id = _clean_text(item.get("id"), 64)
        label = _clean_text(item.get("label") or item.get("name"), TITLE_MAX)
        if not node_id or not label or node_id in seen_ids:
            continue
        seen_ids.add(node_id)
        nodes.append({"id": node_id, "label": label})
    if not nodes:
        raise BlockIssue("diagram 没有可用节点")

    edges: list[dict[str, str]] = []
    adjacency: dict[str, list[str]] = {node["id"]: [] for node in nodes}
    for item in raw_edges:
        if not isinstance(item, dict):
            continue
        source = _clean_text(item.get("source") or item.get("from"), 64)
        target = _clean_text(item.get("target") or item.get("to"), 64)
        if source not in seen_ids or target not in seen_ids:
            raise BlockIssue("diagram 的边引用了不存在的节点")
        if source == target:
            raise BlockIssue("diagram 出现自环")
        edges.append({"source": source, "target": target})
        adjacency[source].append(target)

    if _has_cycle(adjacency):
        raise BlockIssue("diagram 成环，结构不可用")
    return {"nodes": nodes, "edges": edges}


def _has_cycle(adjacency: dict[str, list[str]]) -> bool:
    """三色迭代 DFS（不递归，避免深图爆栈）。"""
    state: dict[str, int] = {}
    for root in adjacency:
        if state.get(root):
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        state[root] = 1
        while stack:
            node, index = stack.pop()
            targets = adjacency.get(node, [])
            if index >= len(targets):
                state[node] = 2
                continue
            stack.append((node, index + 1))
            nxt = targets[index]
            if state.get(nxt) == 1:
                return True
            if state.get(nxt) != 2:
                state[nxt] = 1
                stack.append((nxt, 0))
    return False


def validate_block(raw: Any, allowed_source_ids: set[str]) -> dict[str, Any]:
    """校验并规范化单个 block；不合契约抛 ``BlockIssue``（由调用方丢弃并记录）。"""
    if not isinstance(raw, dict):
        raise BlockIssue("block 不是对象")
    block_type = _clean_text(raw.get("type"), 32)
    if block_type not in BLOCK_TYPES:
        raise BlockIssue(f"未知 block 类型: {block_type or '(空)'}")
    title = _clean_text(raw.get("title"), TITLE_MAX)
    if not title:
        raise BlockIssue("block 缺少标题")

    refs_raw = raw.get("source_refs")
    refs = [
        ref
        for ref in (refs_raw if isinstance(refs_raw, list) else [])
        if isinstance(ref, str) and ref in allowed_source_ids
    ]
    # 约束 1：出处必须落在本次候选 chunk 上，否则讲解就脱离了学生的资料
    if not refs:
        raise BlockIssue("block 没有可核对的资料出处")

    block: dict[str, Any] = {
        "type": block_type,
        "title": title,
        "content": _clean_text(raw.get("content"), CONTENT_MAX),
        "source_refs": sorted(set(refs)),
    }
    if block_type == "diagram":
        data = raw.get("data")
        block["data"] = _validate_diagram(data if isinstance(data, dict) else {})
    elif not block["content"]:
        raise BlockIssue("文字类 block 内容为空")

    flat = " ".join(
        [block["title"], block["content"], _flatten(block.get("data"))]
    ).lower()
    for word in FORBIDDEN_KEYS:
        if f'"{word}"' in flat:
            raise BlockIssue(f"block 含练习/判分语义: {word}")
    return block


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flatten(item) for item in value)
    return str(value) if value is not None else ""


def validate_blocks(
    raw_blocks: Any, allowed_source_ids: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 ``(合格 blocks, 被丢弃原因)``。

    丢弃而不是整份失败：模型偶发产出一块坏内容不该让整份讲解不可用；但若一块都不
    剩，调用方必须按失败处理（界面显示"暂不可用"），绝不落空内容。
    """
    blocks: list[dict[str, Any]] = []
    issues: list[str] = []
    seen_titles: set[tuple[str, str]] = set()
    for raw in raw_blocks if isinstance(raw_blocks, list) else []:
        if len(blocks) >= MAX_BLOCKS:
            issues.append("超出 block 上限, 已截断")
            break
        try:
            block = validate_block(raw, allowed_source_ids)
        except BlockIssue as exc:
            issues.append(str(exc))
            continue
        key = (block["type"], block["title"])
        if key in seen_titles:
            issues.append(f"重复 block: {block['type']}/{block['title']}")
            continue
        seen_titles.add(key)
        blocks.append(block)
    # 展示顺序按 BLOCK_TYPES 稳定排序（同类保持模型给的先后）
    order = {name: index for index, name in enumerate(BLOCK_TYPES)}
    blocks.sort(key=lambda item: order.get(item["type"], len(order)))
    return blocks, issues


def is_stale(stored_version: str, stored_hash: str, current_hash: str) -> bool:
    """教学契约版本或上下文指纹任一不匹配 → stale（内容仍可读，但要如实标记）。"""
    return stored_version != EXPLANATION_GENERATOR_VERSION or stored_hash != current_hash
