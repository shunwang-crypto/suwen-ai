"use client";

// 检索调试面板（F2，评委演示用）：query → POST debug/retrieve，
// dense/sparse 命中数、各 chunk 分数与 rerank 分、confident 标记全透明；
// 下方四阶段命中对比（dense/sparse 召回 → RRF 融合 → rerank 终排）带排名变化箭头，
// 直观展示 rerank 对排序的改变（Dify 式召回测试，管理端命中测试页共用）。

import { useState, type FormEvent } from "react";

import { EmptyState, ErrorBanner, Spinner } from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type {
  RetrieveDebugResult,
  RetrieveDebugStages,
  RetrieveStageEntry,
} from "@/lib/types";

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

/** 排名变化箭头：相对上一阶段的名次变动（↑2 / ↓1 / ＝ / 新）。 */
function RankChange({ prev, next }: { prev: number | null; next: number }) {
  if (prev === null) {
    return (
      <span className="rounded bg-slate-100 px-1 py-0.5 text-[10px] font-medium text-slate-500">
        新
      </span>
    );
  }
  const delta = prev - next;
  if (delta === 0) {
    return <span className="px-0.5 font-mono text-[10px] text-slate-400">＝</span>;
  }
  return (
    <span
      className={`rounded px-1 py-0.5 font-mono text-[10px] font-semibold ${
        delta > 0 ? "bg-emerald-50 text-emerald-600" : "bg-red-50 text-red-600"
      }`}
      title={delta > 0 ? `较上一阶段上升 ${delta} 名` : `较上一阶段下降 ${-delta} 名`}
    >
      {delta > 0 ? `↑${delta}` : `↓${-delta}`}
    </span>
  );
}

/** 单阶段召回列表：名次 + 排名变化箭头 + 分数 + 文本片段。 */
function StageColumn({
  title,
  scoreLabel,
  entries,
  stages,
  prevRank,
  disabledNote,
  accent,
}: {
  title: string;
  scoreLabel: string;
  entries: RetrieveStageEntry[];
  stages: RetrieveDebugStages;
  /** chunk_id → 上一阶段名次（1 起）；undefined 表示首阶段（不画箭头） */
  prevRank?: Map<string, number>;
  disabledNote?: string;
  accent: string;
}) {
  return (
    <div className="flex min-w-0 flex-col rounded-xl border border-slate-200 bg-white">
      <div className="border-b border-slate-100 px-3 py-2">
        <p className={`text-xs font-semibold ${accent}`}>{title}</p>
        <p className="text-[10px] text-slate-400">{scoreLabel}</p>
      </div>
      <div className="min-h-24 flex-1 space-y-1.5 p-2">
        {entries.length === 0 ? (
          <p className="py-4 text-center text-[11px] text-slate-400">
            {disabledNote ?? "无召回结果"}
          </p>
        ) : (
          entries.map((e, i) => {
            const meta = stages.chunk_meta[e.chunk_id];
            return (
              <div
                key={e.chunk_id}
                className="rounded-lg bg-slate-50 px-2 py-1.5"
                title={meta ? `${meta.heading_path}\n${meta.text}` : e.chunk_id}
              >
                <div className="flex items-center gap-1.5">
                  <span className="flex h-4.5 w-4.5 shrink-0 items-center justify-center rounded bg-slate-200/80 font-mono text-[10px] text-slate-600">
                    {i + 1}
                  </span>
                  {prevRank && (
                    <RankChange prev={prevRank.get(e.chunk_id) ?? null} next={i + 1} />
                  )}
                  <span className="ml-auto shrink-0 font-mono text-[10px] text-slate-500">
                    {e.score != null ? e.score.toFixed(4) : "—"}
                  </span>
                </div>
                <div className="mt-1 line-clamp-2 text-[11px] leading-snug text-slate-600">
                  <MathText text={meta?.text || e.chunk_id.slice(0, 8)} />
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

/** 四阶段命中对比：让 rerank 改变排序这件事一眼可见。 */
function StagesView({ stages }: { stages: RetrieveDebugStages }) {
  const rankOf = (entries: RetrieveStageEntry[]) =>
    new Map(entries.map((e, i) => [e.chunk_id, i + 1]));
  const denseRank = rankOf(stages.dense);
  const sparseRank = rankOf(stages.sparse);
  const fusedRank = rankOf(stages.fused);
  // 融合阶段的箭头基线：优先对比稠密名次，稠密未命中则对比稀疏名次
  const fusedBaseline = new Map<string, number>();
  for (const e of stages.fused) {
    const prev = denseRank.get(e.chunk_id) ?? sparseRank.get(e.chunk_id);
    if (prev != null) fusedBaseline.set(e.chunk_id, prev);
  }
  return (
    <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
      <StageColumn
        title="① 稠密召回"
        scoreLabel="向量余弦分"
        entries={stages.dense}
        stages={stages}
        accent="text-indigo-600"
      />
      <StageColumn
        title="② 稀疏召回"
        scoreLabel="BM25 词法分"
        entries={stages.sparse}
        stages={stages}
        disabledNote="稀疏通道未启用或无命中"
        accent="text-cyan-600"
      />
      <StageColumn
        title="③ RRF 融合"
        scoreLabel="倒数排名融合分 · 箭头对比召回名次"
        entries={stages.fused}
        stages={stages}
        prevRank={fusedBaseline}
        accent="text-violet-600"
      />
      <StageColumn
        title="④ rerank 终排"
        scoreLabel="交叉编码器精排分 · 箭头对比融合名次"
        entries={stages.reranked}
        stages={stages}
        prevRank={fusedRank}
        disabledNote="重排未启用"
        accent="text-emerald-600"
      />
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
          className="flex shrink-0 items-center gap-1.5 rounded-xl bg-ink px-4 py-2 text-sm font-semibold text-white hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
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

          {result.stages && result.stages.dense.length > 0 && (
            <div className="space-y-1.5">
              <p className="text-xs font-semibold text-slate-500">
                四阶段命中对比（↑↓ 为相对上一阶段的名次变化）
              </p>
              <StagesView stages={result.stages} />
            </div>
          )}

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
