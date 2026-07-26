"use client";

// 分块预览抽屉（F2）：heading_path / 页码 / 文本(<MathText>) / kc 数 / 向量化状态，分页浏览。

import { useEffect, useState } from "react";

import { EmptyState, ErrorBanner, LoadingState } from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type { ChunkOut, DocumentOut } from "@/lib/types";

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
  const [chunks, setChunks] = useState<ChunkOut[] | null>(null);
  const [page, setPage] = useState(0);
  const [retryKey, setRetryKey] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 分页加载：page/retryKey 变化触发；effect 内联 IIFE（同 chat 页模式）
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const rows = await apiJson<ChunkOut[]>(
          `/api/kb/${kbId}/documents/${doc.id}/chunks?offset=${page * PAGE_SIZE}&limit=${PAGE_SIZE}`,
        );
        if (!alive) return;
        setChunks(rows);
        setError(null);
      } catch (err) {
        if (alive) {
          setError(err instanceof ApiError ? err.message : "分块加载失败");
        }
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [kbId, doc.id, page, retryKey]);

  const goto = (p: number) => {
    setLoading(true);
    setError(null);
    if (p === page) setRetryKey((k) => k + 1);
    else setPage(p);
  };

  return (
    <div className="fixed inset-0 z-40">
      <button
        type="button"
        aria-label="关闭分块预览"
        className="absolute inset-0 bg-slate-900/40"
        onClick={onClose}
      />
      <div className="absolute inset-y-0 right-0 flex w-full max-w-xl flex-col bg-white shadow-xl">
        <header className="flex shrink-0 items-center justify-between border-b border-slate-100 px-4 py-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-slate-800">
              分块预览 · {doc.filename}
            </h2>
            <p className="text-xs text-slate-400">
              第 {page + 1} 页 · 每页 {PAGE_SIZE} 块
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            aria-label="关闭"
          >
            ✕
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {error && <ErrorBanner message={error} onRetry={() => goto(page)} />}
          {loading ? (
            <LoadingState text="分块加载中…" />
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
                  className="rounded-xl border border-slate-200 bg-slate-50/60 p-3"
                >
                  <div className="mb-1.5 flex flex-wrap items-center gap-1.5 text-xs">
                    <span className="rounded bg-slate-200/80 px-1.5 py-0.5 font-mono text-slate-500">
                      #{page * PAGE_SIZE + i + 1}
                    </span>
                    {c.heading_path && (
                      <span className="max-w-64 truncate rounded bg-indigo-50 px-1.5 py-0.5 text-indigo-600">
                        {c.heading_path}
                      </span>
                    )}
                    {c.page_start != null && (
                      <span className="text-slate-400">
                        P{c.page_start}
                        {c.page_end != null && c.page_end !== c.page_start
                          ? `-${c.page_end}`
                          : ""}
                      </span>
                    )}
                    <span className="text-slate-400">
                      {c.kc_ids.length} 个知识点
                    </span>
                    <span
                      className={
                        c.embedding_status === "failed"
                          ? "text-red-600"
                          : c.embedding_status === "pending"
                            ? "text-slate-400"
                            : "text-emerald-600"
                      }
                    >
                      {EMBED_LABELS[c.embedding_status] ?? c.embedding_status}
                    </span>
                  </div>
                  <MathText
                    text={c.text}
                    className="text-xs leading-relaxed text-slate-700"
                  />
                </li>
              ))}
            </ul>
          )}
        </div>

        <footer className="flex shrink-0 items-center justify-between border-t border-slate-100 px-4 py-2.5">
          <button
            type="button"
            disabled={loading || page === 0}
            onClick={() => goto(page - 1)}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            上一页
          </button>
          <button
            type="button"
            disabled={loading || (chunks !== null && chunks.length < PAGE_SIZE)}
            onClick={() => goto(page + 1)}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            下一页
          </button>
        </footer>
      </div>
    </div>
  );
}
