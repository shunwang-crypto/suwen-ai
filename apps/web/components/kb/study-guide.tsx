"use client";

// 学习指南面板（对标 NotebookLM Source guide/Studio）：摄取完成后回答「下一步干嘛」。
// 大纲（可折叠 + KC 标签）/ 术语表 / 推荐问题 chips（点击跳对话页直接发问）。

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { ErrorBanner, SkeletonList, Spinner, fmtDateTime } from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type { StudyGuide, StudyGuideOut } from "@/lib/types";

const POLL_INTERVAL_MS = 3000;
const POLL_MAX_TICKS = 200; // 最长约 10 分钟，覆盖排队时间与较长资料

function generationActive(status?: string): boolean {
  return status === "queued" || status === "running";
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`h-4 w-4 shrink-0 text-ink-muted transition-transform ${
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

  const load = useCallback(
    async (silent = false): Promise<StudyGuide | null> => {
      try {
        const res = await apiJson<StudyGuideOut>(`/api/kb/${kbId}/study-guide`);
        setGuide(res.guide);
        setGenerating(generationActive(res.generation.status));
        if (res.generation.status === "failed") {
          setError(res.generation.message || "学习指南生成失败，请重试");
        } else {
          setError(null);
        }
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

  // 首屏加载（A5 收敛: 复用 load; 父组件用 key={kbId} 挂载本组件，切库即重挂载，无需在 effect 里重置状态）
  /* eslint-disable react-hooks/set-state-in-effect -- 首屏复用 load, 其 setState 均在 await 之后作为加载反馈 */
  useEffect(() => {
    void load();
  }, [load]);
  /* eslint-enable react-hooks/set-state-in-effect */

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
      void load(true);
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [generating, load]);

  const rebuild = async () => {
    setError(null);
    setGenerating(true);
    pollTicksRef.current = 0;
    try {
      const result = await apiJson<{ status: string }>(`/api/kb/${kbId}/study-guide/rebuild`, {
        method: "POST",
        body: {},
      });
      setGenerating(generationActive(result.status));
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
        <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-line bg-surface-muted px-4 py-6 text-center">
          <p className="text-sm text-ink-secondary">
            {generating
              ? "正在生成学习指南…（后台任务，完成后自动出现）"
              : "还没有学习指南"}
          </p>
          <p className="text-xs text-ink-muted">
            指南包含学习大纲、术语表与推荐提问，帮你决定从哪读起
          </p>
          {writable && (
            <button
              type="button"
              onClick={() => void rebuild()}
              disabled={generating}
              className="mt-1 flex items-center gap-1.5 rounded-lg bg-ink px-3 py-1.5 text-sm font-semibold text-white hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
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
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
              学习大纲
            </h3>
            <ul className="space-y-1.5">
              {guide.outline.map((item, idx) => {
                const open = openIdx === idx;
                return (
                  <li
                    key={`${item.title}-${idx}`}
                    className="overflow-hidden rounded-xl border border-line shadow-[0_2px_10px_-4px_rgba(79,70,229,0.10)] transition-all duration-200 hover:-translate-y-0.5 hover:border-indigo-200 hover:shadow-[0_6px_16px_-4px_rgba(79,70,229,0.18)] motion-reduce:hover:translate-y-0"
                  >
                    <button
                      type="button"
                      onClick={() => setOpenIdx(open ? -1 : idx)}
                      aria-expanded={open}
                      className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface-muted"
                    >
                      <ChevronIcon open={open} />
                      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-indigo-50 text-body-sm font-semibold text-indigo-600">
                        {idx + 1}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">
                        <MathText text={item.title} singleLine />
                      </span>
                    </button>
                    {open && (
                      <div className="space-y-2 border-t border-line bg-surface-muted/60 px-3 py-2.5">
                        {item.summary && (
                          <div className="text-xs leading-relaxed text-ink-secondary">
                            <MathText text={item.summary} />
                          </div>
                        )}
                        {item.kc_names.length > 0 && (
                          <div className="flex flex-wrap gap-1">
                            {item.kc_names.map((kc) => (
                              <span
                                key={kc}
                                className="rounded-full bg-white px-2 py-0.5 text-body-sm font-medium text-indigo-600 ring-1 ring-indigo-100"
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
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
                术语表
              </h3>
              <dl className="grid gap-2 sm:grid-cols-2">
                {guide.glossary.map((g) => (
                  <div
                    key={g.term}
                    className="rounded-xl border border-line bg-surface-muted/60 px-3 py-2 transition-all duration-200 hover:-translate-y-0.5 hover:border-indigo-200 hover:bg-white hover:shadow-[0_6px_16px_-4px_rgba(79,70,229,0.16)] motion-reduce:hover:translate-y-0"
                  >
                    <dt className="text-sm font-medium text-ink"><MathText text={g.term} /></dt>
                    <dd className="mt-0.5 text-xs leading-relaxed text-ink-muted">
                      <MathText text={g.definition} />
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          )}

          {/* 推荐问题 chips：点击跳对话页并自动发问（?kb=&q=） */}
          {guide.suggested_questions.length > 0 && (
            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
                从这些问题开始
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {guide.suggested_questions.map((q) => (
                  <Link
                    key={q}
                    href={`/chat?new=1&kb=${encodeURIComponent(kbId)}&q=${encodeURIComponent(q)}`}
                    className="rounded-full border border-indigo-200 bg-indigo-50 px-3 py-1.5 text-xs font-medium text-indigo-700 transition-colors hover:bg-indigo-100"
                    title={`在「${kbName}」中提问`}
                  >
                    <MathText text={q} />
                  </Link>
                ))}
              </div>
            </section>
          )}

          <div className="flex items-center justify-between gap-2 border-t border-line pt-3 text-body-sm text-ink-muted">
            <span>生成于 {fmtDateTime(guide.generated_at)} · 基于 {guide.doc_count} 篇文档</span>
            {writable && (
              <button
                type="button"
                onClick={() => void rebuild()}
                disabled={generating}
                className="flex items-center gap-1 rounded-lg border border-line px-2 py-1 font-medium text-ink-muted hover:bg-surface-muted disabled:opacity-50"
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
