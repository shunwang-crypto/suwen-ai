"use client";

// 引用回原文抽屉（包A，自实现无组件库）：点击引用卡后从
// GET /api/kb/{kb_id}/chunks/{chunk_id}/context?window=1 拉取命中块上下文，
// 展示面包屑 + 页码 + 前后相邻子块 + 父块全文；命中块内引用片段 <mark> 高亮
// （quote indexOf 定位，找不到则整块淡高亮），全部文本经 <MathText> 渲染公式。

import { useEffect, useState } from "react";

import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type { ChunkContextOut, CitationData } from "@/lib/types";

interface FetchResult {
  key: string;
  data: ChunkContextOut | null;
  error: string | null;
}

/** 命中块正文：quote 在 chunk 文本中 indexOf 精确定位 <mark>；失败整块淡高亮。 */
function HighlightedChunk({ text, quote }: { text: string; quote?: string }) {
  const q = (quote ?? "").trim();
  const at = q ? text.indexOf(q) : -1;
  if (at === -1) {
    return (
      <div className="rounded-lg bg-amber-50 px-2.5 py-1.5 text-sm leading-relaxed text-slate-800">
        <MathText text={text} />
      </div>
    );
  }
  return (
    <div className="text-sm leading-relaxed text-slate-800">
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
    <div className="border-l-2 border-slate-200 pl-2.5 text-xs leading-relaxed text-slate-400">
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
  const key = `${kbId}/${citation.chunk_id}`;
  const [result, setResult] = useState<FetchResult | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await apiJson<ChunkContextOut>(
          `/api/kb/${kbId}/chunks/${citation.chunk_id}/context?window=1`,
        );
        if (alive) setResult({ key, data, error: null });
      } catch (err) {
        if (alive) {
          setResult({
            key,
            data: null,
            error: err instanceof ApiError ? err.message : "原文加载失败，请重试",
          });
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [key, kbId, citation.chunk_id]);

  const loading = result?.key !== key;
  const data = loading ? null : result?.data ?? null;
  const error = loading ? null : result?.error ?? null;
  const page = data?.chunk.page_start ?? citation.page;

  return (
    <div className="fixed inset-0 z-50">
      <button
        type="button"
        aria-label="关闭原文抽屉"
        className="absolute inset-0 bg-slate-900/40"
        onClick={onClose}
      />
      <div className="absolute inset-y-0 right-0 flex w-[30rem] max-w-[92vw] flex-col bg-white shadow-2xl">
        {/* 头部：来源文档 + 引用序号 */}
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-slate-100 px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <span className="flex h-5 min-w-5 shrink-0 items-center justify-center rounded bg-indigo-600 px-1 text-xs font-semibold text-white">
              {citation.idx}
            </span>
            <h2 className="min-w-0 truncate text-sm font-semibold text-slate-800">
              {data?.doc_name ?? citation.doc_name ?? "引用原文"}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>

        {/* 面包屑 + 页码 */}
        <div className="shrink-0 border-b border-slate-100 bg-slate-50/60 px-4 py-2">
          <p className="break-words text-xs text-slate-500">
            {(data?.chunk.heading_path || citation.heading_path) ?? ""}
            {page != null && (
              <span className="ml-1.5 rounded bg-slate-200/70 px-1.5 py-0.5 text-[11px] text-slate-600">
                第 {page} 页
              </span>
            )}
          </p>
        </div>

        {/* 正文：前块 → 命中块（高亮）→ 后块 → 父块全文 */}
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
          {loading && (
            <p className="flex items-center gap-2 py-8 text-sm text-slate-400">
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
                <details className="rounded-xl border border-slate-200 bg-slate-50/50 px-3 py-2">
                  <summary className="cursor-pointer select-none text-xs font-medium text-slate-500 hover:text-indigo-600">
                    查看所在父块全文
                    {data.parent_heading_path
                      ? `（${data.parent_heading_path}）`
                      : ""}
                  </summary>
                  <div className="mt-2 text-xs leading-relaxed text-slate-600">
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
