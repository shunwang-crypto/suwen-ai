"use client";

/**
 * 知识点依赖图（active 模式）：把后端已经算好的先修闭包画成真图。
 *
 * 为什么值得画成图而不是列表：先修结构是"谁挡着谁"的关系，列表只能表达先后，
 * 表达不了分叉与汇聚（概念图/知识图的元分析证据见 brief-6）。原来的路径条只在
 * 列间画了一个通用箭头，`edges` 从未真正出现在界面上。
 *
 * 边界（与 kc-dependency-layout.ts 同一套契约）：
 * - 层与边都来自后端，前端不推断依赖；
 * - 只画两端节点都在渲染集合里的边；
 * - 只画当前计划的目标 KC + 其递归先修闭包（active 模式）。全库知识图（full 模式）
 *   需要新的后端端点并按章节聚合，不在本步范围。
 *
 * 无障碍与实现取舍：连线走一层 pointer-events-none 的 SVG，节点仍是原生
 * button/article 的 HTML 元素叠在上层 —— 保住键盘焦点与 aria 语义，
 * 不用 foreignObject。
 */

import type { PlanKcDependency, PlanKcDependencyNode } from "@/lib/types";

import {
  NODE_H,
  NODE_W,
  kcNodeKind,
  kcTaskProgress,
  layoutKcDependency,
} from "./kc-dependency-layout";

interface KcDependencyGraphProps {
  dependency?: PlanKcDependency;
  onTaskOpen?: (taskId: string) => void;
}

const KIND_BOX: Record<ReturnType<typeof kcNodeKind>, string> = {
  current_task: "border-brand-300 bg-white",
  blocking: "border-warning-300 bg-warning-50",
  prerequisite: "border-dashed border-line-strong bg-surface-muted/60",
};

const PROGRESS_LABEL: Record<ReturnType<typeof kcTaskProgress>, string> = {
  done: "已完成",
  partial: "部分完成",
  pending: "待学习",
  none: "尚未安排任务",
};

const PROGRESS_DOT: Record<ReturnType<typeof kcTaskProgress>, string> = {
  done: "bg-success-500",
  partial: "bg-warning-500",
  pending: "bg-brand-400",
  none: "bg-line-strong",
};

function nodeAriaLabel(node: PlanKcDependencyNode): string {
  const kind = kcNodeKind(node);
  const role =
    kind === "prerequisite" ? "递归先修知识点" : kind === "blocking" ? "阻塞后续任务的知识点" : "计划任务知识点";
  return `${role}：${node.kc_name}，${PROGRESS_LABEL[kcTaskProgress(node)]}`;
}

function NodeBody({ node }: { node: PlanKcDependencyNode }) {
  const progress = kcTaskProgress(node);
  return (
    <>
      <span className="flex items-center gap-1.5">
        <span aria-hidden="true" className={`h-1.5 w-1.5 shrink-0 rounded-full ${PROGRESS_DOT[progress]}`} />
        <span className="min-w-0 truncate text-body-sm font-semibold text-ink">{node.kc_name}</span>
      </span>
      <span className="mt-1 block truncate text-caption text-ink-muted">
        {PROGRESS_LABEL[progress]}
        {node.task_ids.length > 1 ? ` · ${node.task_ids.length} 个任务` : ""}
      </span>
      {node.is_blocking && (
        <span className="mt-1 block truncate text-caption font-medium text-warning-700">
          阻塞 {node.blocking_task_ids.length} 个后续任务
        </span>
      )}
    </>
  );
}

export function KcDependencyGraph({ dependency, onTaskOpen }: KcDependencyGraphProps) {
  const layout = layoutKcDependency(dependency);
  if (layout.nodes.length === 0) return null;

  return (
    <div className="overflow-x-auto pb-2" data-testid="kc-dependency-graph-scroll">
      <div className="relative min-w-max" style={{ width: layout.width, height: layout.height }}>
        <svg
          aria-hidden="true"
          className="pointer-events-none absolute inset-0"
          width={layout.width}
          height={layout.height}
        >
          <defs>
            <marker
              id="kc-dep-arrow"
              markerWidth="7"
              markerHeight="7"
              refX="6"
              refY="3.5"
              orient="auto"
            >
              <path d="M 0 0 L 7 3.5 L 0 7 z" className="fill-brand-400" />
            </marker>
          </defs>
          {layout.edges.map((edge) => (
            <path
              key={`${edge.from}->${edge.to}`}
              d={edge.d}
              fill="none"
              strokeWidth="1.5"
              className="stroke-brand-300"
              markerEnd="url(#kc-dep-arrow)"
            />
          ))}
        </svg>

        {layout.nodes.map(({ node, x, y }) => {
          const kind = kcNodeKind(node);
          const box = `absolute overflow-hidden rounded-xl border px-3 py-2 text-left ${KIND_BOX[kind]}`;
          const style = { left: x, top: y, width: NODE_W, height: NODE_H };
          const taskId = node.task_ids[0];
          if (taskId && onTaskOpen) {
            return (
              <button
                key={node.kc_id}
                type="button"
                style={style}
                onClick={() => onTaskOpen(taskId)}
                aria-label={`查看${nodeAriaLabel(node)}`}
                className={`${box} shadow-sm transition-colors hover:border-brand-500 hover:bg-brand-50/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2`}
              >
                <NodeBody node={node} />
              </button>
            );
          }
          return (
            <article key={node.kc_id} style={style} aria-label={nodeAriaLabel(node)} className={box}>
              <NodeBody node={node} />
            </article>
          );
        })}
      </div>
    </div>
  );
}
