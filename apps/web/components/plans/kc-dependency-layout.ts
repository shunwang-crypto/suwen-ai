/**
 * 知识点依赖图的纯布局计算（无 DOM、无副作用，供单测直接调用）。
 *
 * 分层不由前端推断：`levels` 是后端 `_build_kc_dependency` 用先修边跑
 * `kc.dag.topological_order` 得到的拓扑层，前端只把层映射成列、把**后端给的边**
 * 映射成连线。前端不新增、不猜测任何依赖关系（项目红线：不推断学习语义），
 * 所以这里也不需要图布局引擎。
 *
 * 两条必须成立的渲染契约（见 __tests__/kc-dependency-layout.test.ts）：
 * 1. 画出的每条边都必须来自 `dependency.edges`，且两端节点都在渲染集合里
 *    —— 隐藏节点不得出边；
 * 2. 层内顺序与后端给的节点顺序一致 —— 顺序是稳定的数据库顺序，不重排。
 */

import type { PlanKcDependency, PlanKcDependencyNode } from "@/lib/types";

export const NODE_W = 176;
export const NODE_H = 68;
export const COL_GAP = 40;
export const ROW_GAP = 12;
export const PAD = 8;

export interface PositionedKcNode {
  node: PlanKcDependencyNode;
  /** 所在列（0 起，按 levels 数组顺序，不用后端 index 以防层号有空洞） */
  column: number;
  /** 后端层号，仅用于展示"第 N 层" */
  levelIndex: number;
  x: number;
  y: number;
}

export interface PositionedKcEdge {
  from: string;
  to: string;
  /** SVG path d 属性（三次贝塞尔，水平控制点） */
  d: string;
}

export interface KcDependencyLayout {
  nodes: PositionedKcNode[];
  edges: PositionedKcEdge[];
  width: number;
  height: number;
  columnCount: number;
}

const EMPTY: KcDependencyLayout = {
  nodes: [],
  edges: [],
  width: 0,
  height: 0,
  columnCount: 0,
};

export function layoutKcDependency(
  dependency: PlanKcDependency | null | undefined,
): KcDependencyLayout {
  const levels = dependency?.levels ?? [];
  if (levels.length === 0) return EMPTY;

  const nodes: PositionedKcNode[] = [];
  const byId = new Map<string, PositionedKcNode>();
  let maxRows = 0;

  levels.forEach((level, column) => {
    // 层内顺序沿用后端给的顺序（稳定数据库顺序），不做任何重排
    level.nodes.forEach((node, row) => {
      if (!node.kc_id || byId.has(node.kc_id)) return; // 同一 KC 只画一次
      const positioned: PositionedKcNode = {
        node,
        column,
        levelIndex: level.index,
        x: PAD + column * (NODE_W + COL_GAP),
        y: PAD + row * (NODE_H + ROW_GAP),
      };
      nodes.push(positioned);
      byId.set(node.kc_id, positioned);
    });
    maxRows = Math.max(maxRows, level.nodes.length);
  });

  if (nodes.length === 0) return EMPTY;

  const edges: PositionedKcEdge[] = [];
  const seen = new Set<string>();
  for (const edge of dependency?.edges ?? []) {
    const source = byId.get(edge.from_kc_id);
    const target = byId.get(edge.to_kc_id);
    // 契约 1：两端都必须在渲染集合里，否则不画（隐藏节点不得出边）
    if (!source || !target || source === target) continue;
    const key = `${edge.from_kc_id}->${edge.to_kc_id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    edges.push({ from: edge.from_kc_id, to: edge.to_kc_id, d: edgePath(source, target) });
  }

  const columnCount = levels.length;
  return {
    nodes,
    edges,
    width: PAD * 2 + columnCount * NODE_W + Math.max(0, columnCount - 1) * COL_GAP,
    height: PAD * 2 + maxRows * NODE_H + Math.max(0, maxRows - 1) * ROW_GAP,
    columnCount,
  };
}

function edgePath(source: PositionedKcNode, target: PositionedKcNode): string {
  const x1 = source.x + NODE_W;
  const y1 = source.y + NODE_H / 2;
  const x2 = target.x;
  const y2 = target.y + NODE_H / 2;
  if (target.column <= source.column) {
    // 同列或回指（数据稀疏时可能出现）：绕行画弧，不隐瞒这条边也不改层
    const lift = NODE_H / 2 + ROW_GAP;
    return `M ${x1} ${y1} C ${x1 + COL_GAP / 2} ${y1 - lift}, ${x2 - COL_GAP / 2} ${y2 - lift}, ${x2} ${y2}`;
  }
  const control = Math.max(16, (x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + control} ${y1}, ${x2 - control} ${y2}, ${x2} ${y2}`;
}

/** 节点视觉档位：当前任务 / 阻塞后续 / 递归先修（无任务）。 */
export type KcNodeKind = "current_task" | "blocking" | "prerequisite";

export function kcNodeKind(node: PlanKcDependencyNode): KcNodeKind {
  if (node.is_blocking) return "blocking";
  if (node.is_current_task || node.task_ids.length > 0) return "current_task";
  return "prerequisite";
}

/** 任务状态汇总 → 展示档位（全完成 / 有跳过 / 待学习）。 */
export function kcTaskProgress(node: PlanKcDependencyNode): "done" | "partial" | "pending" | "none" {
  const statuses = node.task_statuses ?? [];
  if (statuses.length === 0) return "none";
  if (statuses.every((item) => item.status === "done")) return "done";
  if (statuses.some((item) => item.status === "done" || item.status === "skipped")) return "partial";
  return "pending";
}
