"use client";

/**
 * 知识点讲解面板：把后端的结构化 block 渲染成一份可读文档。
 *
 * 为什么不是一坨 Markdown：分段 + 标题信号是有实证的教学设计，而结构化之后每一块
 * 都能强制带出处、能按块校验、能按掌握档换讲法。
 *
 * 三个界面纪律（与后端契约一一对应）：
 * - **出处可核对**：每块列出它引用的资料片段（文档名 + 章节 + 页码），不给出处的块
 *   在后端就被丢了，界面不会出现无出处内容；
 * - **stale 不清空**：教学契约或资料变了只提示"按旧版生成"并给重新生成入口，
 *   绝不把学生正在读的内容抹掉；
 * - **失败就说暂不可用**：生成失败不展示任何伪造内容（stub 网关下必然如此）。
 */

import { useCallback, useState } from "react";

import { ErrorBanner } from "@/components/common";
import { apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import { useApiResource } from "@/lib/use-api-resource";

interface ExplanationBlock {
  type: "concept" | "worked_example" | "misconception" | "diagram" | string;
  title: string;
  content: string;
  source_refs: string[];
  data?: { nodes?: Array<{ id: string; label: string }>; edges?: Array<{ source: string; target: string }> };
}

interface ExplanationSource {
  id: string;
  document: string;
  heading_path: string;
  page_start: number | null;
  text: string;
}

interface ExplanationResponse {
  available: boolean;
  kc_id: string;
  kc_name: string;
  band: string;
  blocks?: ExplanationBlock[];
  sources?: ExplanationSource[];
  stale?: boolean;
  stale_reason?: string | null;
  source_count?: number;
}

const BLOCK_LABELS: Record<string, string> = {
  concept: "概念",
  worked_example: "例题演练",
  misconception: "常见误解",
  diagram: "结构图示",
};

const BAND_LABELS: Record<string, string> = {
  unobserved: "尚无作答证据",
  weak: "较弱",
  reinforce: "待巩固",
  near: "接近掌握",
  mastered: "已掌握",
};

const STALE_LABELS: Record<string, string> = {
  teaching_contract_changed: "讲解按旧版教学契约生成",
  material_changed: "资料已更新，这份讲解按旧资料生成",
};

function sourceLabel(source: ExplanationSource): string {
  const parts = [`出处：《${source.document}》`];
  if (source.heading_path) parts.push(source.heading_path);
  if (source.page_start) parts.push(`第 ${source.page_start} 页`);
  return parts.join(" · ");
}

function SourceList({ refs, sources }: { refs: string[]; sources: ExplanationSource[] }) {
  const cited = sources.filter((source) => refs.includes(source.id));
  if (cited.length === 0) return null;
  return (
    <ul className="mt-2 space-y-1 border-t border-line pt-2">
      {cited.map((source) => (
        // 拼成单一文本节点：屏幕阅读器与测试都按一句话读，不被 JSX 拆碎
        <li key={source.id} className="text-caption text-ink-muted">
          {sourceLabel(source)}
        </li>
      ))}
    </ul>
  );
}

function DiagramBlock({ data }: { data: NonNullable<ExplanationBlock["data"]> }) {
  const nodes = data.nodes ?? [];
  const edges = data.edges ?? [];
  const labelById = new Map(nodes.map((node) => [node.id, node.label]));
  return (
    <div className="mt-2 space-y-1">
      <ul className="flex flex-wrap gap-2">
        {nodes.map((node) => (
          <li
            key={node.id}
            className="rounded-[7px] border border-line bg-white px-2 py-1 text-caption text-ink"
          >
            {node.label}
          </li>
        ))}
      </ul>
      <ul className="space-y-0.5">
        {edges.map((edge) => (
          <li key={`${edge.source}->${edge.target}`} className="text-caption text-ink-secondary">
            {labelById.get(edge.source) ?? edge.source} → {labelById.get(edge.target) ?? edge.target}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function KcExplanationPanel({ kbId, kcId }: { kbId: string; kcId: string }) {
  const path = `/api/kb/${kbId}/kc/${kcId}/explanation`;
  // 读走仓内统一的 useApiResource（自带 request-epoch 过期防护，迟到响应不写状态）；
  // 生成（POST）的结果作为覆盖值，避免在 effect 里直接 setState。
  const resource = useApiResource<ExplanationResponse>(path, {
    errorMessage: "讲解暂不可用，请稍后重试",
  });
  const [generated, setGenerated] = useState<{ path: string; body: ExplanationResponse } | null>(
    null,
  );
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);

  const data = generated?.path === path ? generated.body : resource.data;
  const error = generateError ?? resource.error;

  const generate = useCallback(async () => {
    setGenerating(true);
    setGenerateError(null);
    try {
      const body = await apiJson<ExplanationResponse>(path, { method: "POST" });
      setGenerated({ path, body });
    } catch {
      // 生成失败不动已有内容：只提示暂不可用，绝不展示伪造讲解
      setGenerateError("这次没能生成可用的讲解，暂不可用；已保留原有内容");
    } finally {
      setGenerating(false);
    }
  }, [path]);

  if (resource.loading && !data) {
    return <p className="text-body-sm text-ink-muted">正在读取讲解…</p>;
  }

  const blocks = data?.blocks ?? [];
  const sources = data?.sources ?? [];

  return (
    <section aria-label="知识点讲解" className="min-w-0">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h4 className="text-body-sm font-semibold text-ink">{data?.kc_name ?? "知识点"} · 讲解</h4>
          <p className="mt-1 text-caption text-ink-muted">
            按你的资料生成，每块都给出出处；讲法随当前学情（{BAND_LABELS[data?.band ?? "unobserved"] ?? "尚无作答证据"}）调整。
          </p>
        </div>
        <button
          type="button"
          onClick={generate}
          disabled={generating}
          className="rounded-[7px] border border-line bg-white px-3 py-1.5 text-caption font-medium text-ink-secondary transition-colors hover:bg-surface-muted disabled:opacity-60"
        >
          {generating ? "生成中…" : data?.available ? "重新生成" : "生成讲解"}
        </button>
      </div>

      {error && (
        <div className="mt-2">
          <ErrorBanner message={error} />
        </div>
      )}

      {data?.stale && (
        <p className="mt-2 rounded-[7px] border border-warning-200 bg-warning-50 px-3 py-2 text-caption text-warning-700">
          {STALE_LABELS[data.stale_reason ?? ""] ?? "这份讲解已过期"}
          ，内容仍可阅读；需要最新版本请点「重新生成」。
        </p>
      )}

      {!data?.available && !error && (
        <p className="mt-2 text-body-sm text-ink-muted">
          还没有这个知识点的讲解。
          {data?.source_count === 0
            ? "该知识点在资料里没有对应片段，先上传或关联相关资料才能生成有出处的讲解。"
            : "点「生成讲解」按你的资料生成。"}
        </p>
      )}

      {blocks.length > 0 && (
        <div className="mt-3 space-y-3">
          {blocks.map((block) => (
            <article
              key={`${block.type}-${block.title}`}
              className="rounded-xl border border-line bg-white px-4 py-3"
            >
              <span className="inline-flex rounded-full bg-brand-50 px-2 py-0.5 text-caption font-medium text-brand-700">
                {BLOCK_LABELS[block.type] ?? block.type}
              </span>
              <h5 className="mt-2 text-body-sm font-semibold text-ink">{block.title}</h5>
              {block.content && (
                <div className="mt-1 text-body-sm leading-6 text-ink-secondary">
                  <MathText text={block.content} />
                </div>
              )}
              {block.type === "diagram" && block.data && <DiagramBlock data={block.data} />}
              <SourceList refs={block.source_refs} sources={sources} />
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
