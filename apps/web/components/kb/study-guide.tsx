"use client";

// 学习指南面板（对标 NotebookLM Source guide/Studio）：摄取完成后回答「下一步干嘛」。
// 大纲（可折叠 + KC 标签）/ 术语表 / 推荐问题 chips（点击跳对话页直接发问）。

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { ErrorBanner, SkeletonList, Spinner, fmtDateTime } from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import type { StudyGuide, StudyGuideOut } from "@/lib/types";

const POLL_INTERVAL_MS = 3000;
const POLL_MAX_TICKS = 40; // 最长约 2 分钟（standard 档一次生成通常 10-30s）

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${
        open ? "rotate-90" : ""
      }`}
      aria-hidden
    >
      <path d="m9 18 6-6-6-6" />
    </svg>
  );
}

export function StudyGuidePanel({
  kbId,
  kbName,
  writable,
}: {
  kbId: string;
  kbName: string;
  writable: boolean;
}) {
  const [guide, setGuide] = useState<StudyGuide | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [openIdx, setOpenIdx] = useState<number>(0);
  const pollTicksRef = useRef(0);
  // 重新生成时的基线时间戳：轮询到 generated_at 变化才算完成（否则会被旧指南骗停）
  const baselineRef = useRef<string | null>(null);

  const load = useCallback(
    async (silent = false): Promise<StudyGuide | null> => {
      try {
        const res = await apiJson<StudyGuideOut>(`/api/kb/${kbId}/study-guide`);
        setGuide(res.guide);
        setError(null);
        return res.guide;
      } catch (err) {
        if (!silent) {
          setError(err instanceof ApiError ? err.message : "学习指南加载失败");
        }
        return null;
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [kbId],
  );

  // 首屏加载（父组件用 key={kbId} 挂载本组件，切库即重挂载，无需在 effect 里重置状态）
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiJson<StudyGuideOut>(`/api/kb/${kbId}/study-guide`);
        if (!alive) return;
        setGuide(res.guide);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setError(err instanceof ApiError ? err.message : "学习指南加载失败");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [kbId]);

  // 生成中轮询：后台 ARQ 任务完成后指南出现即停
  useEffect(() => {
    if (!generating) return;
    const timer = setInterval(() => {
      pollTicksRef.current += 1;
      if (pollTicksRef.current > POLL_MAX_TICKS) {
        setGenerating(false);
        setError("生成超时，请稍后重试（后台任务可能仍在运行）");
        return;
      }
      void load(true).then((g) => {
        if (g && g.generated_at !== baselineRef.current) setGenerating(false);
      });
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [generating, load]);

  const rebuild = async () => {
    setError(null);
    setGenerating(true);
    pollTicksRef.current = 0;
    baselineRef.current = guide?.generated_at ?? null;
    try {
      await apiJson<{ status: string }>(`/api/kb/${kbId}/study-guide/rebuild`, {
        method: "POST",
        body: {},
      });
    } catch (err) {
      setGenerating(false);
      setError(err instanceof ApiError ? err.message : "生成学习指南失败");
    }
  };

  if (loading) return <SkeletonList rows={3} />;

  return (
    <div className="space-y-4">
      {error && <ErrorBanner message={error} onClose={() => setError(null)} />}

      {!guide ? (
        <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-slate-200 bg-slate-50 px-4 py-6 text-center">
          <p className="text-sm text-slate-600">
            {generating
              ? "正在生成学习指南…（后台任务，完成后自动出现）"
              : "还没有学习指南"}
          </p>
          <p className="text-xs text-slate-400">
            指南包含学习大纲、术语表与推荐提问，帮你决定从哪读起
          </p>
          {writable && (
            <button
              type="button"
              onClick={() => void rebuild()}
              disabled={generating}
              className="mt-1 flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {generating && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}
              生成学习指南
            </button>
          )}
        </div>
      ) : (
        <>
          {/* 大纲：可折叠，每项带 KC 标签 */}
          <section>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
              学习大纲
            </h3>
            <ul className="space-y-1.5">
              {guide.outline.map((item, idx) => {
                const open = openIdx === idx;
                return (
                  <li
                    key={`${item.title}-${idx}`}
                    className="overflow-hidden rounded-xl border border-slate-200 shadow-[0_2px_10px_-4px_rgba(79,70,229,0.10)] transition-all duration-200 hover:-translate-y-0.5 hover:border-indigo-200 hover:shadow-[0_6px_16px_-4px_rgba(79,70,229,0.18)] motion-reduce:hover:translate-y-0"
                  >
                    <button
                      type="button"
                      onClick={() => setOpenIdx(open ? -1 : idx)}
                      aria-expanded={open}
                      className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-slate-50"
                    >
                      <ChevronIcon open={open} />
                      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-indigo-50 text-[11px] font-semibold text-indigo-600">
                        {idx + 1}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-800">
                        {item.title}
                      </span>
                    </button>
                    {open && (
                      <div className="space-y-2 border-t border-slate-100 bg-slate-50/60 px-3 py-2.5">
                        {item.summary && (
                          <p className="text-xs leading-relaxed text-slate-600">
                            {item.summary}
                          </p>
                        )}
                        {item.kc_names.length > 0 && (
                          <div className="flex flex-wrap gap-1">
                            {item.kc_names.map((kc) => (
                              <span
                                key={kc}
                                className="rounded-full bg-white px-2 py-0.5 text-[11px] font-medium text-indigo-600 ring-1 ring-indigo-100"
                              >
                                {kc}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>

          {/* 术语表（两栏卡片化） */}
          {guide.glossary.length > 0 && (
            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
                术语表
              </h3>
              <dl className="grid gap-2 sm:grid-cols-2">
                {guide.glossary.map((g) => (
                  <div
                    key={g.term}
                    className="rounded-xl border border-slate-200 bg-slate-50/60 px-3 py-2 transition-all duration-200 hover:-translate-y-0.5 hover:border-indigo-200 hover:bg-white hover:shadow-[0_6px_16px_-4px_rgba(79,70,229,0.16)] motion-reduce:hover:translate-y-0"
                  >
                    <dt className="text-sm font-medium text-slate-800">{g.term}</dt>
                    <dd className="mt-0.5 text-xs leading-relaxed text-slate-500">
                      {g.definition}
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          )}

          {/* 推荐问题 chips：点击跳对话页并自动发问（?kb=&q=） */}
          {guide.suggested_questions.length > 0 && (
            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
                从这些问题开始
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {guide.suggested_questions.map((q) => (
                  <Link
                    key={q}
                    href={`/chat?kb=${encodeURIComponent(kbId)}&q=${encodeURIComponent(q)}`}
                    className="rounded-full border border-indigo-200 bg-indigo-50 px-3 py-1.5 text-xs font-medium text-indigo-700 transition-colors hover:bg-indigo-100"
                    title={`在「${kbName}」中提问`}
                  >
                    {q}
                  </Link>
                ))}
              </div>
            </section>
          )}

          <div className="flex items-center justify-between gap-2 border-t border-slate-100 pt-3 text-[11px] text-slate-400">
            <span>生成于 {fmtDateTime(guide.generated_at)} · 基于 {guide.doc_count} 篇文档</span>
            {writable && (
              <button
                type="button"
                onClick={() => void rebuild()}
                disabled={generating}
                className="flex items-center gap-1 rounded-lg border border-slate-200 px-2 py-1 font-medium text-slate-500 hover:bg-slate-50 disabled:opacity-50"
              >
                {generating && <Spinner className="h-3 w-3" />}
                重新生成
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
