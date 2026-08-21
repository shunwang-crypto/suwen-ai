"use client";

// 引用回原文抽屉（包A，自实现无组件库）：点击引用卡后从
// GET /api/kb/{kb_id}/chunks/{chunk_id}/context?window=1 拉取命中块上下文，
// 展示面包屑 + 页码 + 前后相邻子块 + 父块全文；命中块内引用片段 <mark> 高亮
// （quote indexOf 定位，找不到则整块淡高亮），全部文本经 <MathText> 渲染公式。

import { MathText } from "@/lib/katex";
import type { ChunkContextOut, CitationData } from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

/** 命中块正文：quote 在 chunk 文本中 indexOf 精确定位 <mark>；失败整块淡高亮。 */
function HighlightedChunk({ text, quote }: { text: string; quote?: string }) {
  const q = (quote ?? "").trim();
  const at = q ? text.indexOf(q) : -1;
  if (at === -1) {
    return (
      <div className="rounded-lg bg-amber-50 px-2.5 py-1.5 text-sm leading-relaxed text-ink">
        <MathText text={text} />
      </div>
    );
  }
  return (
    <div className="text-sm leading-relaxed text-ink">
      {at > 0 && <MathText text={text.slice(0, at)} />}
      <mark className="rounded bg-amber-200/80 px-0.5 py-0.5">
        <MathText text={text.slice(at, at + q.length)} />
      </mark>
      <MathText text={text.slice(at + q.length)} />
    </div>
  );
}

function NeighborBlock({ text }: { text: string }) {
  return (
    <div className="border-l-2 border-line pl-2.5 text-xs leading-relaxed text-ink-muted">
      <MathText text={text} />
    </div>
  );
}

export function ContextDrawer({
  kbId,
  citation,
  onClose,
}: {
  kbId: string;
  citation: CitationData;
  onClose: () => void;
}) {
  // 上下文加载（A5 收敛: useApiResource; 原 key 匹配过期防护由 request-epoch 承担）
  const { data, loading, error } = useApiResource<ChunkContextOut>(
    `/api/kb/${kbId}/chunks/${citation.chunk_id}/context?window=1`,
    { errorMessage: "原文加载失败，请重试" },
  );
  const page = data?.chunk.page_start ?? citation.page;

  return (
    <div className="fixed inset-0 z-50">
      <button
        type="button"
        aria-label="关闭原文抽屉"
        className="absolute inset-0 bg-ink/40"
        onClick={onClose}
      />
      <div className="absolute inset-y-0 right-0 flex w-[30rem] max-w-[92vw] flex-col bg-white shadow-2xl">
        {/* 头部：来源文档 + 引用序号 */}
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-line px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <span className="flex h-5 min-w-5 shrink-0 items-center justify-center rounded bg-indigo-600 px-1 text-xs font-semibold text-white">
              {citation.idx}
            </span>
            <h2 className="min-w-0 truncate text-sm font-semibold text-ink">
              {data?.doc_name ?? citation.doc_name ?? "引用原文"}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded-lg p-1 text-ink-muted hover:bg-surface-muted hover:text-ink-secondary"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>

        {/* 面包屑 + 页码 */}
        <div className="shrink-0 border-b border-line bg-surface-muted/60 px-4 py-2">
          <p className="break-words text-xs text-ink-muted">
            {(data?.chunk.heading_path || citation.heading_path) ?? ""}
            {page != null && (
              <span className="ml-1.5 rounded bg-surface-muted/70 px-1.5 py-0.5 text-body-sm text-ink-secondary">
                第 {page} 页
              </span>
            )}
          </p>
        </div>

        {/* 正文：前块 → 命中块（高亮）→ 后块 → 父块全文 */}
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
          {loading && (
            <p className="flex items-center gap-2 py-8 text-sm text-ink-muted">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-indigo-200 border-t-indigo-600" />
              原文加载中…
            </p>
          )}
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </div>
          )}
          {data && (
            <>
              {data.prev.map((c) => (
                <NeighborBlock key={c.id} text={c.text} />
              ))}
              <HighlightedChunk
                text={data.chunk.text}
                quote={citation.highlight}
              />
              {data.next.map((c) => (
                <NeighborBlock key={c.id} text={c.text} />
              ))}
              {data.parent_text && (
                <details className="rounded-xl border border-line bg-surface-muted/50 px-3 py-2">
                  <summary className="cursor-pointer select-none text-xs font-medium text-ink-muted hover:text-indigo-600">
                    查看所在父块全文
                    {data.parent_heading_path
                      ? `（${data.parent_heading_path}）`
                      : ""}
                  </summary>
                  <div className="mt-2 text-xs leading-relaxed text-ink-secondary">
                    <MathText text={data.parent_text} />
                  </div>
                </details>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
