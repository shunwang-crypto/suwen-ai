"use client";

// 检索调试面板（F2，评委演示用）：query → POST debug/retrieve，
// dense/sparse 命中数、各 chunk 分数与 rerank 分、confident 标记全透明。

import { useState, type FormEvent } from "react";

import { EmptyState, ErrorBanner, Spinner } from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type { RetrieveDebugResult } from "@/lib/types";

const SOURCE_LABELS: Record<string, string> = {
  dense: "稠密",
  sparse: "稀疏",
  fused: "融合",
};

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl bg-slate-50 px-3 py-2">
      <p className="text-xs text-slate-400">{label}</p>
      <p className="text-sm font-semibold text-slate-800">{value}</p>
    </div>
  );
}

export function RetrieveDebug({ kbId }: { kbId: string }) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<RetrieveDebugResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (e: FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (!q || loading) return;
    setLoading(true);
    setError(null);
    try {
      const res = await apiJson<RetrieveDebugResult>(
        `/api/kb/${kbId}/debug/retrieve`,
        { body: { query: q } },
      );
      setResult(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "检索调试失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-3">
      <form onSubmit={run} className="flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          maxLength={2000}
          placeholder="输入检索 query，观察 dense/sparse 召回与重排全过程"
          className="min-w-0 flex-1 rounded-xl border border-slate-300 px-3.5 py-2 text-sm text-slate-900 outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
        />
        <button
          type="submit"
          disabled={!query.trim() || loading}
          className="flex shrink-0 items-center gap-1.5 rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}
          检索
        </button>
      </form>

      {error && <ErrorBanner message={error} onClose={() => setError(null)} />}

      {result && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            <Stat label="稠密命中" value={String(result.dense_hits)} />
            <Stat label="稀疏命中" value={String(result.sparse_hits)} />
            <Stat label="稀疏通道" value={result.used_sparse ? "已启用" : "未启用"} />
            <Stat label="重排" value={result.used_rerank ? "已启用" : "未启用"} />
            <div
              className={`rounded-xl px-3 py-2 ${
                result.confident ? "bg-emerald-50" : "bg-amber-50"
              }`}
            >
              <p className="text-xs text-slate-400">置信度</p>
              <p
                className={`text-sm font-semibold ${
                  result.confident ? "text-emerald-700" : "text-amber-700"
                }`}
              >
                {result.confident ? "confident" : "不足"}
              </p>
            </div>
          </div>

          {result.chunks.length === 0 ? (
            <EmptyState
              title="未检索到结果"
              description="尝试换个 query，或确认文档已完成向量化"
            />
          ) : (
            <ul className="space-y-2">
              {result.chunks.map((c, i) => (
                <li
                  key={c.chunk_id}
                  className="rounded-xl border border-slate-200 bg-white p-3"
                >
                  <div className="mb-1.5 flex flex-wrap items-center gap-1.5 text-xs">
                    <span className="rounded bg-slate-200/80 px-1.5 py-0.5 font-mono text-slate-500">
                      {i + 1}
                    </span>
                    <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-indigo-600">
                      {SOURCE_LABELS[c.source] ?? c.source}
                    </span>
                    <span className="font-mono text-slate-500">
                      score {c.score.toFixed(4)}
                    </span>
                    {c.rerank_score != null && (
                      <span className="font-mono text-violet-600">
                        rerank {c.rerank_score.toFixed(4)}
                      </span>
                    )}
                    {c.heading_path && (
                      <span className="max-w-56 truncate text-slate-400">
                        {c.heading_path}
                      </span>
                    )}
                    {c.page_start != null && (
                      <span className="text-slate-400">P{c.page_start}</span>
                    )}
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
      )}
    </div>
  );
}
