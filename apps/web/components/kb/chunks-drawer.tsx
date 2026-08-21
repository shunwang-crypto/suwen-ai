"use client";

// 分块预览抽屉（F2）：heading_path / 页码 / 文本(<MathText>) / kc 数 / 向量化状态，分页浏览。

import { useState } from "react";

import { EmptyState, ErrorBanner, SkeletonList } from "@/components/common";
import { MathText } from "@/lib/katex";
import type { ChunkOut, DocumentOut } from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

const PAGE_SIZE = 20;

const EMBED_LABELS: Record<string, string> = {
  pending: "待向量化",
  embedded: "已向量化",
  done: "已向量化",
  failed: "向量化失败",
};

export function ChunksDrawer({
  kbId,
  doc,
  onClose,
}: {
  kbId: string;
  doc: DocumentOut;
  onClose: () => void;
}) {
  const [page, setPage] = useState(0);
  const [retryKey, setRetryKey] = useState(0);

  // 分页加载（A5 收敛: useApiResource; page 走路径, retryKey 走 deps）
  const { data: chunks, loading, error } = useApiResource<ChunkOut[]>(
    `/api/kb/${kbId}/documents/${doc.id}/chunks?offset=${page * PAGE_SIZE}&limit=${PAGE_SIZE}`,
    { errorMessage: "分块加载失败", deps: [retryKey] },
  );

  const goto = (p: number) => {
    if (p === page) setRetryKey((k) => k + 1);
    else setPage(p);
  };

  return (
    <div className="fixed inset-0 z-40">
      <button
        type="button"
        aria-label="关闭分块预览"
        className="absolute inset-0 bg-ink/40"
        onClick={onClose}
      />
      <div className="absolute inset-y-0 right-0 flex w-full max-w-xl flex-col bg-white shadow-xl">
        <header className="flex shrink-0 items-center justify-between border-b border-line px-4 py-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-ink">
              分块预览 · {doc.filename}
            </h2>
            <p className="text-xs text-ink-muted">
              第 {page + 1} 页 · 每页 {PAGE_SIZE} 块
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded-lg p-1.5 text-ink-muted hover:bg-surface-muted hover:text-ink-secondary"
            aria-label="关闭"
          >
            ✕
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {error && <ErrorBanner message={error} onRetry={() => goto(page)} />}
          {loading ? (
            <SkeletonList rows={5} />
          ) : chunks && chunks.length === 0 ? (
            <EmptyState
              title={page === 0 ? "该文档暂无分块" : "没有更多分块了"}
              description={
                page === 0 ? "文档可能仍在解析中，稍后再来看看" : undefined
              }
            />
          ) : (
            <ul className="space-y-3">
              {(chunks ?? []).map((c, i) => (
                <li
                  key={c.id}
                  className="rounded-xl border border-line bg-surface-muted/60 p-3"
                >
                  <div className="mb-1.5 flex flex-wrap items-center gap-1.5 text-xs">
                    <span className="rounded bg-surface-muted/80 px-1.5 py-0.5 font-mono text-ink-muted">
                      #{page * PAGE_SIZE + i + 1}
                    </span>
                    {c.heading_path && (
                      <span className="max-w-64 truncate rounded bg-indigo-50 px-1.5 py-0.5 text-indigo-600">
                        {c.heading_path}
                      </span>
                    )}
                    {c.page_start != null && (
                      <span className="text-ink-muted">
                        P{c.page_start}
                        {c.page_end != null && c.page_end !== c.page_start
                          ? `-${c.page_end}`
                          : ""}
                      </span>
                    )}
                    <span className="text-ink-muted">
                      {c.kc_ids.length} 个知识点
                    </span>
                    <span
                      className={
                        c.embedding_status === "failed"
                          ? "text-red-600"
                          : c.embedding_status === "pending"
                            ? "text-ink-muted"
                            : "text-emerald-600"
                      }
                    >
                      {EMBED_LABELS[c.embedding_status] ?? c.embedding_status}
                    </span>
                  </div>
                  <MathText
                    text={c.text}
                    className="text-xs leading-relaxed text-ink-secondary"
                  />
                </li>
              ))}
            </ul>
          )}
        </div>

        <footer className="flex shrink-0 items-center justify-between border-t border-line px-4 py-2.5">
          <button
            type="button"
            disabled={loading || page === 0}
            onClick={() => goto(page - 1)}
            className="rounded-lg border border-line-strong px-3 py-1.5 text-xs font-medium text-ink-secondary hover:bg-surface-muted disabled:cursor-not-allowed disabled:opacity-40"
          >
            上一页
          </button>
          <button
            type="button"
            disabled={loading || (chunks !== null && chunks.length < PAGE_SIZE)}
            onClick={() => goto(page + 1)}
            className="rounded-lg border border-line-strong px-3 py-1.5 text-xs font-medium text-ink-secondary hover:bg-surface-muted disabled:cursor-not-allowed disabled:opacity-40"
          >
            下一页
          </button>
        </footer>
      </div>
    </div>
  );
}
