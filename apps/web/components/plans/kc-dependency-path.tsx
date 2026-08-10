"use client";

import type {
  PlanKcDependency,
  PlanKcDependencyNode,
} from "@/lib/types";

interface KcDependencyPathProps {
  dependency?: PlanKcDependency;
  onTaskOpen?: (taskKey: string) => void;
}

function statusLabel(status: PlanKcDependencyNode["task_statuses"][number]["status"]): string {
  if (status === "done") return "已完成";
  if (status === "skipped") return "已跳过";
  return "待学习";
}

function KcNode({ node, onTaskOpen }: { node: PlanKcDependencyNode; onTaskOpen?: (taskKey: string) => void }) {
  const statusText = node.task_statuses.length > 0
    ? node.task_statuses.map((item) => `${statusLabel(item.status)}`).join("、")
    : "尚未安排任务";
  const taskKey = node.task_keys[0];
  const content = (
    <>
      <span className="block truncate text-body-sm font-semibold text-ink">{node.kc_name}</span>
      <span className="mt-1 block text-caption text-ink-muted">
        {node.is_current_task ? `计划任务 · ${statusText}` : "递归先修知识点"}
      </span>
      {node.task_keys.length > 1 && (
        <span className="mt-1 block text-caption text-ink-muted">关联 {node.task_keys.length} 个任务</span>
      )}
      {node.is_blocking && (
        <span className="mt-1 block text-caption font-medium text-warning-700">
          阻塞 {node.blocking_task_keys.length} 个后续任务
        </span>
      )}
    </>
  );

  if (taskKey && onTaskOpen) {
    return (
      <button
        type="button"
        onClick={() => onTaskOpen(taskKey)}
        aria-label={`查看知识点任务：${node.kc_name}`}
        className="w-full rounded-xl border border-line bg-white px-3 py-3 text-left shadow-sm transition hover:border-brand-300 hover:bg-brand-50/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2"
      >
        {content}
      </button>
    );
  }

  return (
    <article
      aria-label={`先修知识点：${node.kc_name}`}
      className="rounded-xl border border-dashed border-line-strong bg-surface-muted/50 px-3 py-3"
    >
      {content}
    </article>
  );
}

export function KcDependencyPath({ dependency, onTaskOpen }: KcDependencyPathProps) {
  const levels = dependency?.levels ?? [];
  if (levels.length === 0) return null;

  return (
    <section
      aria-label="知识点学习路径"
      className="mb-4 min-w-0 rounded-xl border border-brand-100 bg-brand-50/30 p-3 sm:p-4"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-body-sm font-semibold text-ink">知识点学习路径</h3>
          <p className="mt-1 text-caption text-ink-muted">左侧先修知识点逐层连接到当前计划任务；点击任务节点可查看具体安排。</p>
        </div>
        <span className="text-caption text-ink-muted">共 {levels.length} 层</span>
      </div>

      <div className="mt-3 overflow-x-auto pb-2" data-testid="kc-dependency-scroll">
        <div className="flex min-w-max items-stretch gap-2">
          {levels.map((level, levelIndex) => (
            <div key={level.index} className="flex w-64 shrink-0 items-center gap-2">
              <div className="min-w-0 flex-1">
                <h4 className="mb-2 text-caption font-medium text-brand-700">第 {level.index + 1} 层</h4>
                <div className="space-y-2">
                  {level.nodes.map((node) => (
                    <KcNode key={node.kc_id} node={node} onTaskOpen={onTaskOpen} />
                  ))}
                </div>
              </div>
              {levelIndex < levels.length - 1 && (
                <span aria-hidden="true" className="shrink-0 text-body-sm text-brand-400">→</span>
              )}
            </div>
          ))}
        </div>
      </div>

      {dependency?.edges.length ? (
        <p className="mt-2 text-caption text-ink-muted" aria-label="知识点依赖方向">
          学习方向：先修知识点 → 当前任务知识点
        </p>
      ) : null}
    </section>
  );
}
