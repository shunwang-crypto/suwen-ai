"use client";

/**
 * 课程知识图（Learning Map full 模式）：这门课全部知识点 + 全部先修边。
 *
 * 与计划里的「知识点学习路径」分工：那边回答"下一步学什么、被什么挡着"（计划任务
 * 闭包）；这里回答"我在整门课的什么位置"（开放学习者模型意义上的全局视图）。
 *
 * 三条纪律：
 * - 掌握度数值不出境：节点只显示证据档（未观测/估计中/已观测）与定性档位词，
 *   而定性档只在证据充分时才由后端给出；
 * - 按章展示，跨章直接前置一并带出并标注，跨章依赖不被章节切割隐藏；
 * - 分层是布局、不是依赖推断；成环残留如实报出（见 lib/kc-graph-layout.ts）。
 */

import { useMemo, useState } from "react";

import { ErrorBanner } from "@/components/common";
import { KcExplanationPanel } from "@/components/kb/kc-explanation-panel";
import { NODE_H, NODE_W } from "@/components/plans/kc-dependency-layout";
import {
  type KbKcGraphResponse,
  layoutKbKcGraph,
  visibleKcIds,
} from "@/lib/kc-graph-layout";
import { useApiResource } from "@/lib/use-api-resource";

const STATE_BOX: Record<string, string> = {
  observed: "border-brand-300 bg-white",
  estimated: "border-line-strong bg-white",
  unobserved: "border-dashed border-line-strong bg-surface-muted/60",
};

export function KcGraphPanel({ kbId }: { kbId: string }) {
  const { data, error, loading } = useApiResource<KbKcGraphResponse>(`/api/kb/${kbId}/kc-graph`);
  const [chapter, setChapter] = useState<string | null>(null);
  const [selectedKc, setSelectedKc] = useState<string | null>(null);
  const chapters = data?.chapters ?? [];
  const activeChapter = chapter ?? chapters[0]?.chapter ?? null;

  const layout = useMemo(() => {
    if (!data || !activeChapter) return null;
    const { ids, external } = visibleKcIds(activeChapter, data.nodes, data.edges);
    return layoutKbKcGraph(data.nodes, data.edges, ids, external);
  }, [data, activeChapter]);

  if (loading) return <p className="text-body-sm text-ink-muted">正在加载课程知识图…</p>;
  if (error) return <ErrorBanner message={error} />;
  if (!data || data.kc_count === 0) {
    return (
      <p className="text-body-sm text-ink-muted">
        这门课还没有知识点。上传资料并完成解析后，系统会从真实章节建立知识点与先修关系。
      </p>
    );
  }

  return (
    <section aria-label="课程知识图" className="min-w-0">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-body-sm font-semibold text-ink">课程知识图</h3>
          <p className="mt-1 text-caption text-ink-muted">
            全课 {data.kc_count} 个知识点 · {data.chapter_count} 章。按章查看；虚线框是尚未观测的知识点，
            带「跨章前置」标记的来自其他章节。
          </p>
        </div>
      </div>

      <div role="group" aria-label="章节切换" className="mt-3 flex flex-wrap gap-2">
        {chapters.map((item) => (
          <button
            key={item.chapter}
            type="button"
            onClick={() => setChapter(item.chapter)}
            aria-pressed={item.chapter === activeChapter}
            className={`rounded-full border px-3 py-1 text-caption font-medium transition-colors ${
              item.chapter === activeChapter
                ? "border-ink bg-ink text-white"
                : "border-line bg-white text-ink-secondary hover:bg-surface-muted"
            }`}
          >
            {item.chapter} · {item.kc_count}
          </button>
        ))}
      </div>

      {layout && layout.nodes.length > 0 && (
        <div className="mt-3 overflow-x-auto pb-2" data-testid="kb-kc-graph-scroll">
          <div className="relative min-w-max" style={{ width: layout.width, height: layout.height }}>
            <svg
              aria-hidden="true"
              className="pointer-events-none absolute inset-0"
              width={layout.width}
              height={layout.height}
            >
              <defs>
                <marker id="kb-kc-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
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
                  markerEnd="url(#kb-kc-arrow)"
                />
              ))}
            </svg>
            {layout.nodes.map(({ node, x, y, external }) => (
              <button
                key={node.kc_id}
                type="button"
                onClick={() => setSelectedKc(node.kc_id)}
                aria-pressed={selectedKc === node.kc_id}
                style={{ left: x, top: y, width: NODE_W, height: NODE_H }}
                aria-label={`查看讲解：${node.kc_name}，${node.evidence_state_label}${
                  node.band ? `，${node.band}` : ""
                }${external ? "，跨章前置" : ""}`}
                className={`absolute overflow-hidden rounded-xl border px-3 py-2 text-left transition-colors hover:border-brand-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 ${
                  STATE_BOX[node.evidence_state] ?? STATE_BOX.unobserved
                }${selectedKc === node.kc_id ? " ring-2 ring-brand-500 ring-offset-1" : ""}`}
              >
                <span className="block truncate text-body-sm font-semibold text-ink">{node.kc_name}</span>
                <span className="mt-1 block truncate text-caption text-ink-muted">
                  {node.band ?? node.evidence_state_label}
                </span>
                {external && (
                  <span className="mt-1 block truncate text-caption text-brand-700">跨章前置</span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      {selectedKc && (
        <div className="mt-4 border-t border-line pt-4">
          <KcExplanationPanel key={selectedKc} kbId={kbId} kcId={selectedKc} />
        </div>
      )}

      {layout && layout.unlayered > 0 && (
        <p className="mt-2 text-caption text-warning-700">
          有 {layout.unlayered} 个知识点的先修关系存在环，已单独排在最后一列 —— 图未替你猜测顺序。
        </p>
      )}
    </section>
  );
}
