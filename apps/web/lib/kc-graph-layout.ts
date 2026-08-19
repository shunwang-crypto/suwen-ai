/**
 * 库级知识图（Learning Map full 模式）的纯布局计算。
 *
 * 与计划级依赖图（`components/plans/kc-dependency-layout.ts`）的关键差别：
 * 计划级的**层由后端给**（后端有"计划任务 KC"作锚点，跑过 topological_order）；
 * 库级没有这个锚点，所以分层在前端做 —— 用给定边的最长路径深度（Kahn）算列。
 *
 * 必须说清的边界：分层是**布局**决策，不是依赖推断。这里绝不新增、不合并、不删除
 * 任何一条关系：
 * - 只画传入的边，且两端都必须在可见集合里；
 * - 数据里若存在环（KcEdge 行理论上被 add_edge_safe 挡住，但存量数据不保证），
 *   剩余节点放到末列并通过 `unlayered` 如实报出，不静默丢节点、不假装无环。
 *
 * 章节视图的取舍：库级 KC 动辄上百个，一次铺平是毛线球。所以按章展示，并且把
 * 选中章 KC 的**跨章直接前置**也带进来标为 `external` —— 跨章依赖要看得见，
 * 而不是被章节切割隐藏。每一章都可达，不做静默截断。
 */

import { COL_GAP, NODE_H, NODE_W, PAD, ROW_GAP } from "@/components/plans/kc-dependency-layout";

export interface KbKcNode {
  kc_id: string;
  kc_name: string;
  chapter: string;
  evidence_state: "unobserved" | "estimated" | "observed" | string;
  evidence_state_label: string;
  band: string | null;
}

export interface KbKcEdge {
  from_kc_id: string;
  to_kc_id: string;
}

export interface KbKcChapter {
  chapter: string;
  kc_ids: string[];
  kc_count: number;
}

export interface KbKcGraphResponse {
  kb_id: string;
  kb_name: string;
  kc_count: number;
  chapter_count: number;
  chapters: KbKcChapter[];
  nodes: KbKcNode[];
  edges: KbKcEdge[];
}

export interface PositionedKbNode {
  node: KbKcNode;
  /** 是否是被带进来的跨章前置（不属于当前选中章） */
  external: boolean;
  column: number;
  x: number;
  y: number;
}

export interface PositionedKbEdge {
  from: string;
  to: string;
  d: string;
}

export interface KbKcLayout {
  nodes: PositionedKbNode[];
  edges: PositionedKbEdge[];
  width: number;
  height: number;
  /** 因成环而无法定层、被放到末列的节点数（如实报出，不静默隐藏） */
  unlayered: number;
}

const EMPTY: KbKcLayout = { nodes: [], edges: [], width: 0, height: 0, unlayered: 0 };

/** 选中章的可见集合：本章 KC + 它们的跨章直接前置。 */
export function visibleKcIds(
  chapter: string,
  nodes: KbKcNode[],
  edges: KbKcEdge[],
): { ids: Set<string>; external: Set<string> } {
  const inChapter = new Set(
    nodes.filter((node) => node.chapter === chapter).map((node) => node.kc_id),
  );
  const external = new Set<string>();
  for (const edge of edges) {
    if (inChapter.has(edge.to_kc_id) && !inChapter.has(edge.from_kc_id)) {
      external.add(edge.from_kc_id);
    }
  }
  const known = new Set(nodes.map((node) => node.kc_id));
  for (const id of [...external]) {
    if (!known.has(id)) external.delete(id); // 只带真实存在的节点
  }
  return { ids: new Set([...inChapter, ...external]), external };
}

export function layoutKbKcGraph(
  nodes: KbKcNode[],
  edges: KbKcEdge[],
  visible: Set<string>,
  external: Set<string> = new Set(),
): KbKcLayout {
  const shown = nodes.filter((node) => visible.has(node.kc_id));
  if (shown.length === 0) return EMPTY;
  const shownIds = new Set(shown.map((node) => node.kc_id));

  // 只保留两端可见的边（隐藏节点不得出边），并去重、去自环
  const kept: KbKcEdge[] = [];
  const seen = new Set<string>();
  for (const edge of edges) {
    if (!shownIds.has(edge.from_kc_id) || !shownIds.has(edge.to_kc_id)) continue;
    if (edge.from_kc_id === edge.to_kc_id) continue;
    const key = `${edge.from_kc_id}->${edge.to_kc_id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    kept.push(edge);
  }

  // Kahn + 最长路径定层（只用给定边）
  const indegree = new Map<string, number>();
  const outgoing = new Map<string, string[]>();
  for (const node of shown) {
    indegree.set(node.kc_id, 0);
    outgoing.set(node.kc_id, []);
  }
  for (const edge of kept) {
    outgoing.get(edge.from_kc_id)?.push(edge.to_kc_id);
    indegree.set(edge.to_kc_id, (indegree.get(edge.to_kc_id) ?? 0) + 1);
  }
  const depth = new Map<string, number>();
  const queue = shown.filter((node) => (indegree.get(node.kc_id) ?? 0) === 0).map((n) => n.kc_id);
  queue.forEach((id) => depth.set(id, 0));
  for (let cursor = 0; cursor < queue.length; cursor += 1) {
    const current = queue[cursor];
    for (const target of outgoing.get(current) ?? []) {
      depth.set(target, Math.max(depth.get(target) ?? 0, (depth.get(current) ?? 0) + 1));
      const left = (indegree.get(target) ?? 0) - 1;
      indegree.set(target, left);
      if (left === 0) queue.push(target);
    }
  }
  // 成环残留：放末列并如实计数
  const maxDepth = shown.reduce((acc, node) => Math.max(acc, depth.get(node.kc_id) ?? 0), 0);
  let unlayered = 0;
  for (const node of shown) {
    if (!depth.has(node.kc_id)) {
      depth.set(node.kc_id, maxDepth + 1);
      unlayered += 1;
    }
  }

  const rows = new Map<number, number>();
  const positioned: PositionedKbNode[] = [];
  const byId = new Map<string, PositionedKbNode>();
  for (const node of shown) {
    const column = depth.get(node.kc_id) ?? 0;
    const row = rows.get(column) ?? 0;
    rows.set(column, row + 1);
    const item: PositionedKbNode = {
      node,
      external: external.has(node.kc_id),
      column,
      x: PAD + column * (NODE_W + COL_GAP),
      y: PAD + row * (NODE_H + ROW_GAP),
    };
    positioned.push(item);
    byId.set(node.kc_id, item);
  }

  const laidOutEdges: PositionedKbEdge[] = [];
  for (const edge of kept) {
    const source = byId.get(edge.from_kc_id);
    const target = byId.get(edge.to_kc_id);
    if (!source || !target) continue;
    laidOutEdges.push({
      from: edge.from_kc_id,
      to: edge.to_kc_id,
      d: kbEdgePath(source, target),
    });
  }

  const columnCount = Math.max(...positioned.map((item) => item.column)) + 1;
  const maxRows = Math.max(...[...rows.values()]);
  return {
    nodes: positioned,
    edges: laidOutEdges,
    width: PAD * 2 + columnCount * NODE_W + Math.max(0, columnCount - 1) * COL_GAP,
    height: PAD * 2 + maxRows * NODE_H + Math.max(0, maxRows - 1) * ROW_GAP,
    unlayered,
  };
}

function kbEdgePath(source: PositionedKbNode, target: PositionedKbNode): string {
  const x1 = source.x + NODE_W;
  const y1 = source.y + NODE_H / 2;
  const x2 = target.x;
  const y2 = target.y + NODE_H / 2;
  if (target.column <= source.column) {
    const lift = NODE_H / 2 + ROW_GAP;
    return `M ${x1} ${y1} C ${x1 + COL_GAP / 2} ${y1 - lift}, ${x2 - COL_GAP / 2} ${y2 - lift}, ${x2} ${y2}`;
  }
  const control = Math.max(16, (x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + control} ${y1}, ${x2 - control} ${y2}, ${x2} ${y2}`;
}
